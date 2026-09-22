"""Interactive offline installation; delegates lifecycle to the existing controller."""
import json
from pathlib import Path
import platform
import re
import shutil
from types import SimpleNamespace

from nomiarch.common import NomiarchError, digest, read_json, write_json
from nomiarch.bootstrap import bundle, controller
from nomiarch.bootstrap.config import validate


def ask(label, default=None):
    while True:
        value = input(label + (f" [{default}]" if default is not None else "") + ": ").strip()
        if value or default is not None:
            return value or str(default)
        print("Please enter a value.")


def choice(label, options, default):
    while True:
        value = ask(label, default).lower()
        if value in options:
            return value
        print("Choose " + ", ".join(options))


def number(label, default, minimum, maximum):
    while True:
        value = ask(label, default)
        if value.isdigit() and minimum <= int(value) <= maximum:
            return int(value)
        print(f"Enter a whole number from {minimum} to {maximum}.")


def file_path(label):
    while True:
        path = Path(ask(label)).expanduser().resolve()
        if path.is_file():
            return str(path)
        print("File not found. Supply a local file; this wizard does not download dependencies.")


def run(output, state_dir, repo, action=None):
    print("Nomiarch setup and maintenance")
    action = action or choice("Action: install / upgrade / repair / verify / backup / status",
                              ("install", "upgrade", "repair", "verify", "backup", "status"), "install")
    if action != "install":
        return maintain(action, state_dir, repo)
    return install(output, state_dir, repo)


def maintain(action, state_dir, repo):
    candidates = []
    for path in sorted((Path(state_dir).expanduser() / "runs").glob("*/run.json")):
        try:
            record = read_json(path)
            if record.get("inventory") and record.get("cleanup", {}).get("status") != "deleted":
                candidates.append((path.parent.resolve(), record))
        except NomiarchError:
            continue
    for i, (path, record) in enumerate(candidates, 1):
        print(f"{i}. {record['config']['name']} — {record['config']['target']} — {record['phase']} ({path})")
    selected = ask("Installation number or full run directory", "1" if len(candidates) == 1 else None)
    if selected.isdigit() and 1 <= int(selected) <= len(candidates):
        directory = candidates[int(selected) - 1][0]
    else:
        directory = Path(selected).expanduser().resolve()
    args = SimpleNamespace(action="status", run=str(directory), repo=repo)
    record = controller.dispatch(args)  # Validate identity before offering a mutation.
    if action == "status":
        return record
    if not record.get("inventory") or record.get("cleanup", {}).get("status") == "deleted":
        raise NomiarchError("No retained VM exists for this run")
    args.action = action
    if action in {"upgrade", "repair"}:
        args.bundle = file_path("Signed release bundle (.tar)")
        args.trusted_key = file_path("Previously trusted public key for that release (.pem)")
        candidate = bundle.inspect(args.bundle, args.trusted_key)
        if candidate["architecture"] != record["config"]["architecture"]:
            raise NomiarchError("Bundle architecture does not match this installation")
        print(f"Candidate: {candidate['version']} / {candidate['manifest_sha256']}")
        print("The guest checks runtime compatibility. Repair requires the currently installed bundle.")
    if action in {"upgrade", "backup"}:
        args.backup_recipient = ask("Recovery public recipient (age1…); retain the private recovery key separately")
        if not re.fullmatch(r"age1[0-9a-z]{58}", args.backup_recipient):
            raise NomiarchError("Expected an age public recipient from age-keygen -y; never enter your private key")
        print("An encrypted backup is saved to this controller. Keep its private recovery key elsewhere.")
    if action == "upgrade":
        print("Upgrade includes downtime and a backup before changes. There is no automatic rollback.")
    if action == "verify":
        print("Verification submits a sample task using the local model and appends audit evidence.")
    print(f"Selected {record['config']['name']} ({record['config']['target']}), run {directory.name}.")
    if choice(f"Run {action} on this installation? yes/no", ("yes", "no"), "no") != "yes":
        print("Cancelled; installation unchanged.")
        return None
    return controller.dispatch(args)


def install(output, state_dir, repo):
    print("Nomiarch — offline local VM installation")
    print("Have Multipass, OpenSSL, a signed release bundle, a separately trusted public key,")
    print("and an approved Ubuntu 24.04 cloud image ready on this machine.")
    print("This installs from local files. Physical air-gap isolation is managed by your site;")
    print("a VM on a connected host is not an air gap. No Azure resources are created.")
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise NomiarchError("Configuration already exists; choose another --output or use bootstrap apply -f " + str(output))
    for command in ("multipass", "openssl"):
        if not shutil.which(command):
            raise NomiarchError(command + " must be installed before starting the offline wizard")

    print("\n1/4 — Verify release")
    archive = file_path("Signed release bundle (.tar)")
    key = file_path("Previously trusted release public key (.pem)")
    print("Verifying signature and all bundled files…", flush=True)
    manifest = bundle.inspect(archive, key)
    architecture = manifest["architecture"]
    native = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    if native != architecture:
        raise NomiarchError("Local host architecture must match bundle architecture: " + architecture)
    print(f"Verified release {manifest['version']} ({architecture}).")

    print("\n2/4 — VM image and capacity")
    image = file_path(f"Approved Ubuntu 24.04 {architecture} cloud image")
    expected = ask("Image SHA-256 from your trusted publisher receipt").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(image) != expected:
        raise NomiarchError("Image checksum does not match the trusted receipt")
    while True:
        name = ask("Environment name", "nomiarch-local")
        if re.fullmatch(r"[a-z][a-z0-9-]{1,19}", name):
            break
        print("Use 2–20 lowercase letters, digits or hyphens; start with a letter.")
    config = {
        "api_version": "nomiarch.io/v1alpha1", "name": name,
        "target": "local", "architecture": architecture,
        "local": {"image": image, "image_sha256": expected},
        "capacity": {
            "cpus": number("VM CPUs", 2, 2, 64),
            "memory_gib": number("VM memory (GiB)", 8, 4, 512),
            "disk_gib": number("VM disk (GiB)", 40, 30, 4096),
        },
        "lifecycle": {
            "destroy_after": choice("Delete this VM after the installation test? yes/no", {"yes", "no"}, "no") == "yes",
            "max_runtime_minutes": number("Installation deadline (minutes)", 60, 5, 240),
        },
    }
    validate(config)
    print("\n3/4 — Review")
    print(json.dumps(config, indent=2))
    print("The VM will be " + ("deleted after the test, including its data." if config["lifecycle"]["destroy_after"] else "kept for further evaluation."))
    print("Core, policy enforcement and the bundled local model will be installed and checked.")
    if choice("Create VM and install now? yes/no", {"yes", "no"}, "no") != "yes":
        print("Cancelled; no VM created and no configuration written.")
        return None
    write_json(output, config)
    print("Configuration saved: " + str(output))
    print("\n4/4 — Install and verify", flush=True)
    result = controller.apply(config, state_dir, "core", archive=archive, key=key, repo=repo)
    print("Retain the run directory for status, repair, backup and removal.")
    return result
