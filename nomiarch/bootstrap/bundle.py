"""Signed complete releases; no downloads occur in verification or installation."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipapp

from nomiarch import __version__
from nomiarch.common import NomiarchError, Runner, atomic_write, canonical, digest, private_dir, read_json, write_json

MAX_BUNDLE_BYTES = 100 * 1024**3
REQUIRED = {"k3s", "install-k3s.sh", "k3s-images.tar.zst", "workload-images.tar", "model.gguf", "installer.pyz", "tools.rego", "components.lock.json", "age.tar.gz"}


def keygen(private, public):
    private, public = Path(private).expanduser(), Path(public).expanduser()
    if private.exists() or public.exists():
        raise NomiarchError("Refusing to overwrite an existing release key")
    private.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    public.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner = Runner()
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    runner.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", private])
    runner.run(["openssl", "pkey", "-in", private, "-pubout", "-out", public])


def seal(directory, output, signing_key, metadata):
    directory, output = Path(directory), Path(output)
    if output.exists():
        raise NomiarchError("Bundle output already exists")
    files = {}
    for p in sorted(directory.rglob("*")):
        if p.is_symlink():
            raise NomiarchError("Release inputs cannot contain symlinks")
        if p.is_file():
            files[p.relative_to(directory).as_posix()] = {"sha256": digest(p), "size": p.stat().st_size}
    manifest = {"schema": 1, "version": __version__, **metadata, "files": files}
    atomic_write(directory / "manifest.json", canonical(manifest))
    Runner().run(["openssl", "dgst", "-sha256", "-sign", signing_key, "-out",
                  directory / "manifest.sig", directory / "manifest.json"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w") as archive:
        for name in ["manifest.json", "manifest.sig", *files]:
            archive.add(directory / name, arcname=name, recursive=False)
    return manifest


def inspect(archive_path, public_key, extract_to=None):
    """Validate signature, entire inventory, sizes, and paths before extraction."""
    with tarfile.open(archive_path, "r:") as archive:
        index = {}
        total = 0
        for m in archive:
            path = PurePosixPath(m.name)
            if (not m.isfile() or path.is_absolute() or ".." in path.parts or
                    "\\" in m.name or str(path) != m.name or m.name in index or m.size < 0):
                raise NomiarchError("Unsafe or duplicate bundle entry: " + m.name)
            total += m.size
            if total > MAX_BUNDLE_BYTES:
                raise NomiarchError("Bundle exceeds 100 GiB admission limit")
            index[m.name] = m
            if len(index) > 1000:
                raise NomiarchError("Too many bundle entries")
        if not {"manifest.json", "manifest.sig"} <= index.keys():
            raise NomiarchError("Missing signed manifest")
        if index["manifest.json"].size > 1024 * 1024 or index["manifest.sig"].size > 8192:
            raise NomiarchError("Oversized signature metadata")
        raw = archive.extractfile(index["manifest.json"]).read()
        sig = archive.extractfile(index["manifest.sig"]).read()
        with tempfile.TemporaryDirectory(prefix="nomiarch-verify-") as tmp:
            p = Path(tmp)
            (p / "manifest").write_bytes(raw)
            (p / "sig").write_bytes(sig)
            Runner().run(["openssl", "dgst", "-sha256", "-verify", public_key, "-signature", p / "sig", p / "manifest"])
        manifest = json.loads(raw)
        if (manifest.get("schema") != 1 or manifest.get("architecture") not in {"amd64", "arm64"}
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.a-z0-9-]+)?", manifest.get("version", ""))):
            raise NomiarchError("Unsupported release manifest")
        files = manifest.get("files", {})
        if not REQUIRED <= files.keys() or set(index) != set(files) | {"manifest.json", "manifest.sig"}:
            raise NomiarchError("Bundle is incomplete or contains undeclared files")
        for name, spec in files.items():
            if index[name].size != spec["size"]:
                raise NomiarchError("Artifact size mismatch: " + name)
            h = hashlib.sha256()
            with archive.extractfile(index[name]) as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != spec["sha256"]:
                raise NomiarchError("Artifact checksum mismatch: " + name)
        for name in ("core_image", "model_image"):
            if not re.fullmatch(r"nomiarch/[a-z]+:[0-9a-f]{64}", manifest.get(name, "")):
                raise NomiarchError("Release image reference must use a content-derived local tag")
        if extract_to:
            destination = Path(extract_to)
            if destination.exists() and any(destination.iterdir()):
                raise NomiarchError("Extraction requires a new empty private directory")
            private_dir(destination)
            for name, m in index.items():
                p = destination / name
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(m) as source, p.open("xb") as out:
                    os.chmod(p, 0o600)
                    shutil.copyfileobj(source, out)
                if name in files and digest(p) != files[name]["sha256"]:
                    raise NomiarchError("Artifact changed during extraction: " + name)
        manifest["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        return manifest


@contextmanager
def verified(archive_path, public_key):
    with tempfile.TemporaryDirectory(prefix="nomiarch-release-") as tmp:
        path = Path(tmp) / "release"
        manifest = inspect(archive_path, public_key, path)
        yield path, manifest


def prepare(repo, output, signing_key, architecture, model, model_sha256,
            python_image="python:3.12-slim-bookworm", model_image="ghcr.io/ggml-org/llama.cpp:server"):
    repo, model = Path(repo), Path(model).expanduser().resolve()
    if architecture not in {"amd64", "arm64"}:
        raise NomiarchError("architecture must be amd64 or arm64")
    if not model.is_file() or digest(model) != model_sha256:
        raise NomiarchError("Admitted model file is missing or its SHA-256 does not match")
    with model.open("rb") as f:
        if f.read(4) != b"GGUF":
            raise NomiarchError("Model must be a GGUF file")
    if Path(signing_key).stat().st_mode & 0o077:
        raise NomiarchError("Signing key permissions must be 0600 or stricter")
    components = read_json(repo / "packages/components.json")
    runner = Runner()
    with tempfile.TemporaryDirectory(prefix="nomiarch-build-") as tmp:
        stage = Path(tmp) / "release"
        stage.mkdir()
        for name, artifact in components["architectures"][architecture].items():
            print("Fetching admitted component: " + name, flush=True)
            with urllib.request.urlopen(artifact["url"], timeout=60) as src, (stage / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if digest(stage / name) != artifact["sha256"]:
                raise NomiarchError("Upstream artifact checksum mismatch: " + name)
        resolved = {}
        for role, source in [("python", python_image), ("model", model_image)]:
            print("Resolving " + role + " image for linux/" + architecture, flush=True)
            runner.run(["docker", "pull", "--platform", "linux/" + architecture, source], timeout=1800)
            record = json.loads(runner.run(["docker", "image", "inspect", source]))[0]
            if record["Architecture"] != architecture or not record.get("RepoDigests"):
                raise NomiarchError("Image architecture or registry digest is unavailable: " + source)
            resolved[role] = {"source": source, "digest": record["RepoDigests"][0], "id": record["Id"]}
        context = Path(tmp) / "image"
        context.mkdir()
        shutil.copytree(repo / "nomiarch", context / "nomiarch", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(stage / "opa", context / "opa")
        (context / "opa").chmod(0o755)
        shutil.copyfile(repo / "packages/Containerfile", context / "Dockerfile")
        image_iid = Path(tmp) / "image-id"
        runner.run(["docker", "build", "--platform", "linux/" + architecture, "--build-arg",
                    "PYTHON_IMAGE=" + resolved["python"]["digest"], "--iidfile", image_iid, context], timeout=1800)
        core_id = image_iid.read_text().strip()
        model_id = resolved["model"]["id"]
        core_ref, model_ref = "nomiarch/core:" + core_id.split(":")[1], "nomiarch/model:" + model_id.split(":")[1]
        runner.run(["docker", "tag", core_id, core_ref])
        runner.run(["docker", "tag", model_id, model_ref])
        runner.run(["docker", "save", "-o", stage / "workload-images.tar", core_ref, model_ref], timeout=1800)
        (stage / "opa").unlink()  # The admitted binary is inside the application image.
        shutil.copyfile(model, stage / "model.gguf")
        shutil.copyfile(repo / "policies/tools.rego", stage / "tools.rego")
        source = Path(tmp) / "zipapp"
        source.mkdir()
        shutil.copytree(context / "nomiarch", source / "nomiarch")
        zipapp.create_archive(source, stage / "installer.pyz", main="nomiarch.cli:main")
        write_json(stage / "components.lock.json", {"components": components, "images": resolved,
                                                   "core_image_id": core_id, "model_sha256": model_sha256})
        manifest = seal(stage, output, signing_key, {"architecture": architecture,
                        "k3s_version": components["k3s_version"], "core_image": core_ref,
                        "model_image": model_ref, "guest": "ubuntu-24.04", "minimum_memory_gib": 4})
    print("Signed offline bundle: " + str(Path(output).resolve()))
    return manifest
