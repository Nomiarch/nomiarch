"""Consistent SQLite snapshots in encrypted ZIP archives."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile

from nomiarch.common import NomiarchError, Runner, canonical, digest, private_dir, read_json, write_json


def export(source, output, recipient, age="age"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source):
        raise NomiarchError("Backup output must be a new file outside the source directory")
    if not recipient.startswith("age1"):
        raise NomiarchError("Use a dedicated age recipient public key")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = "appliance" if (source / "installation.json").exists() else "development"
    selected = ["tokens.json"] if profile == "development" else ["identities.json", "installation.json", "owner.json"]
    if profile == "appliance":
        state = read_json(source / "installation.json")
        selected.append("releases/" + state["active_release"] + ".tar")
    db_relative = "data/core.db" if profile == "development" else "core/core.db"
    if not (source / db_relative).is_file():
        raise NomiarchError("Core database is missing; no complete core backup can be made")
    with tempfile.TemporaryDirectory(prefix="nomiarch-backup-") as tmp:
        tmp = Path(tmp)
        snapshot = tmp / "core.db"
        with sqlite3.connect("file:" + str(source / db_relative) + "?mode=ro", uri=True) as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise NomiarchError("Database snapshot integrity check failed")
        files = {db_relative: snapshot}
        for name in selected:
            p = source / name
            if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(source):
                raise NomiarchError("Required recovery input is missing or unsafe: " + name)
            files[name] = p
        manifest = {"schema": 1, "created": time.time(), "profile": profile,
                    "scope": "core database, local service identities, active signed release; excludes provider state and host/K3s state",
                    "files": {name: {"sha256": digest(p), "size": p.stat().st_size} for name, p in files.items()}}
        archive = tmp / "recovery.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for name, p in files.items():
                z.write(p, name)
            z.writestr("recovery.json", canonical(manifest))
        pending = output.with_name(output.name + ".partial")
        if pending.exists():
            raise NomiarchError("A previous incomplete backup exists at " + str(pending))
        try:
            fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            Runner().run([age, "--encrypt", "--recipient", recipient, "--output", pending, archive], timeout=1800)
            os.replace(pending, output)
        finally:
            if pending.exists():
                pending.unlink()
    receipt = {"file": str(output), "sha256": digest(output), "profile": profile, "created": manifest["created"],
               "scope": manifest["scope"]}
    write_json(output.with_name(output.name + ".receipt.json"), receipt)
    return receipt


def restore(archive, destination, identity, expected_sha256, age="age"):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if digest(archive) != expected_sha256:
        raise NomiarchError("Recovery archive does not match the separately retained checksum")
    if destination.exists():
        raise NomiarchError("Restore requires a new destination; existing data is never overwritten")
    with tempfile.TemporaryDirectory(prefix="nomiarch-restore-", dir=destination.parent) as tmp:
        tmp = Path(tmp)
        decrypted = tmp / "recovery.zip"
        Runner().run([age, "--decrypt", "--identity", identity, "--output", decrypted, archive], timeout=1800)
        staging = private_dir(tmp / "restored")
        with zipfile.ZipFile(decrypted) as z:
            names = set()
            total = 0
            for item in z.infolist():
                p = PurePosixPath(item.filename)
                total += item.file_size
                if (item.is_dir() or p.is_absolute() or ".." in p.parts or "\\" in item.filename or str(p) != item.filename
                    or item.filename in names or stat.S_ISLNK(item.external_attr >> 16) or total > 100 * 1024**3):
                    raise NomiarchError("Unsafe recovery archive entry")
                names.add(item.filename)
            if "recovery.json" not in names or z.getinfo("recovery.json").file_size > 1024 * 1024:
                raise NomiarchError("Missing/oversized recovery manifest")
            manifest = json.loads(z.read("recovery.json"))
            if manifest.get("schema") != 1 or names != set(manifest["files"]) | {"recovery.json"}:
                raise NomiarchError("Recovery inventory mismatch")
            for name, spec in manifest["files"].items():
                p = staging / name
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with z.open(name) as src, p.open("xb") as dst:
                    os.chmod(p, 0o600)
                    shutil.copyfileobj(src, dst)
                if p.stat().st_size != spec["size"] or digest(p) != spec["sha256"]:
                    raise NomiarchError("Recovery artifact integrity failure: " + name)
        # Data stays staged until every file has been authenticated and checked.
        os.replace(staging, destination)
    return {"status": "restored", "destination": str(destination), "profile": manifest["profile"],
            "next": "For an appliance, install the included release using an independently trusted release public key."}
