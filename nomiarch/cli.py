import argparse
import json
import os
from pathlib import Path
import signal
import sys
from urllib.parse import urlsplit

from nomiarch import __version__
from nomiarch.common import NomiarchError, digest, write_json


def wizard(target, output):
    def ask(label, default=None):
        result = input(label + (f" [{default}]" if default else "") + ": ").strip()
        if not result and default is None:
            raise NomiarchError(label + " is required")
        return result or default
    if Path(output).exists():
        raise NomiarchError("Configuration output already exists")
    config = {"api_version": "nomiarch.io/v1alpha1", "name": ask("Environment name", "nomiarch-lab"),
              "target": target, "architecture": "amd64", "capacity": {"cpus": 2, "memory_gib": 8, "disk_gib": 40},
              "lifecycle": {"destroy_after": False, "max_runtime_minutes": 30}}
    if target == "local":
        config["architecture"] = ask("Guest architecture (Apple Silicon: arm64)", "amd64")
        image = Path(ask("Path to admitted Ubuntu 24.04 cloud image")).expanduser().resolve()
        config["local"] = {"image": str(image), "image_sha256": digest(image)}
        print("Image SHA-256: " + config["local"]["image_sha256"] + ". Verify against your trusted publisher receipt.")
    elif target == "azure":
        config["azure"] = {"subscription_id": ask("Azure subscription UUID"), "location": ask("Azure region", "canadacentral"),
                           "vm_size": ask("VM size", "Standard_B2ms"), "admin_cidr": ask("Private administration CIDR (VPN/management subnet)"),
                           "ssh_user": ask("VM administrator", "nomiarch"),
                           "ssh_key": str(Path(ask("SSH private key file", "~/.ssh/id_ed25519")).expanduser().resolve()),
                           "ssh_public_key": str(Path(ask("SSH public key file", "~/.ssh/id_ed25519.pub")).expanduser().resolve()),
                           "image": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server",
                                     "version": ask("Exact Ubuntu Azure image version (see docs/testing.md)")}}
        subnet = input("Existing privately reachable subnet resource ID (blank creates an isolated VNet): ").strip()
        if subnet:
            config["azure"]["subnet_id"] = subnet
        config["lifecycle"]["destroy_after"] = ask("Destroy after test? yes/no", "yes").lower() == "yes"
    else:
        config["architecture"] = ask("Guest architecture", "amd64")
        config["existing"] = {"host": ask("Dedicated Linux host address"), "ssh_user": ask("Administrator", "ubuntu"),
                              "ssh_key": str(Path(ask("SSH private key file")).expanduser().resolve()),
                              "known_hosts": str(Path(ask("Trusted known_hosts file")).expanduser().resolve())}
    from nomiarch.bootstrap.config import validate
    validate(config)
    write_json(output, config)
    print("Configuration written: " + str(Path(output).resolve()))


def parser():
    root = argparse.ArgumentParser(prog="nomiarch", description="Portable core and independent lifecycle controller")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="group", required=True)
    dev = sub.add_parser("dev", help="Local development harness; no cloud resources")
    dsub = dev.add_subparsers(dest="action", required=True)
    for name in ("demo", "up"):
        p = dsub.add_parser(name)
        p.add_argument("--data-dir", default=".nomiarch/dev")
        p.add_argument("--port", type=int, default=8787)
        p.add_argument("--model-url", help="Optional real local llama.cpp endpoint")
    bundle = sub.add_parser("bundle").add_subparsers(dest="action", required=True)
    p = bundle.add_parser("keygen")
    p.add_argument("--private", required=True)
    p.add_argument("--public", required=True)
    p = bundle.add_parser("prepare")
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    p.add_argument("--output", required=True)
    p.add_argument("--signing-key", required=True)
    p.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--model-sha256", required=True)
    p.add_argument("--python-image", default="python:3.12-slim-bookworm")
    p.add_argument("--model-image", default="ghcr.io/ggml-org/llama.cpp:server")
    p = bundle.add_parser("verify")
    p.add_argument("--bundle", required=True)
    p.add_argument("--trusted-key", required=True)
    boot = sub.add_parser("bootstrap").add_subparsers(dest="action", required=True)
    p = boot.add_parser("wizard", help="Guided installation, upgrade, repair, verification and backup")
    p.add_argument("--action", choices=["install", "upgrade", "repair", "verify", "backup", "status"])
    p.add_argument("--output", default="env.local.json")
    p.add_argument("--state-dir", default=".nomiarch")
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    p = boot.add_parser("init", help="Ask for environment variables and write configuration")
    p.add_argument("--target", choices=["local", "azure", "existing"], required=True)
    p.add_argument("--output", required=True)
    for name in ("plan", "apply"):
        p = boot.add_parser(name)
        p.add_argument("--config", "-f", required=True)
        p.add_argument("--target", choices=["local", "azure", "existing"], help="Optional consistency check against configuration")
        p.add_argument("--state-dir", default=".nomiarch")
        p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
        if name == "plan":
            p.add_argument("--provider-plan", action="store_true", help="Also run read-only provider checks and a saved OpenTofu plan")
        else:
            p.add_argument("--scope", choices=["foundation", "core"], default="core")
            p.add_argument("--bundle")
            p.add_argument("--trusted-key")
            p.add_argument("--destroy-after", action="store_true", default=None)
            p.add_argument("--unattended", action="store_true", help="Requires a fresh heartbeat from an independent cleanup controller")
    for name in ("status", "destroy", "install", "repair", "upgrade", "backup", "verify"):
        p = boot.add_parser(name)
        p.add_argument("--run", required=True, help="Exact run directory printed by apply")
        p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
        if name in {"install", "repair", "upgrade"}:
            p.add_argument("--bundle", required=True)
            p.add_argument("--trusted-key", required=True)
        if name in {"upgrade", "backup"}:
            p.add_argument("--backup-recipient", required=True, help="age recipient; retain private recovery key separately")
    p = boot.add_parser("reap")
    p.add_argument("--state-dir", default=".nomiarch")
    p.add_argument("--worker-id", required=True)
    p.add_argument("--watch", action="store_true")
    p.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    recovery = sub.add_parser("backup").add_subparsers(dest="action", required=True)
    p = recovery.add_parser("export")
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--recipient", required=True)
    p = recovery.add_parser("restore")
    p.add_argument("--archive", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--identity", required=True)
    p.add_argument("--sha256", required=True, help="Checksum from the separately retained backup receipt")
    host = sub.add_parser("host", help="Dedicated Ubuntu guest lifecycle; requires root").add_subparsers(dest="action", required=True)
    for name in ("install", "repair", "upgrade", "verify", "backup"):
        p = host.add_parser(name)
        if name in {"install", "repair", "upgrade"}:
            p.add_argument("--bundle", required=True)
            p.add_argument("--trusted-key", required=True)
            p.add_argument("--data-device", help="New Azure managed data disk only")
        if name in {"upgrade", "backup"}:
            p.add_argument("--recipient", required=True)
            p.add_argument("--output", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    # A private umask protects IaC state, plan files, host keys, and all run logs.
    os.umask(0o077)
    def terminate(signum, frame):
        raise KeyboardInterrupt("Termination requested")
    signal.signal(signal.SIGTERM, terminate)
    try:
        value = None
        if args.group == "dev":
            from nomiarch import dev
            if args.model_url and urlsplit(args.model_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise NomiarchError("Development inference must use a local loopback endpoint")
            dev.run(args.data_dir, demo=args.action == "demo", port=args.port, model_url=args.model_url)
        elif args.group == "bundle":
            from nomiarch.bootstrap import bundle
            if args.action == "keygen":
                bundle.keygen(args.private, args.public)
                value = {"public_key": str(Path(args.public).resolve()), "next": "Enroll this public key through a trusted channel before importing releases"}
            elif args.action == "verify":
                value = bundle.inspect(args.bundle, args.trusted_key)
            else:
                bundle.prepare(args.repo, args.output, args.signing_key, args.arch, args.model, args.model_sha256,
                               args.python_image, args.model_image)
        elif args.group == "backup":
            from nomiarch.bootstrap import recovery
            if args.action == "export":
                value = recovery.export(args.source, args.output, args.recipient)
            else:
                value = recovery.restore(args.archive, args.destination, args.identity, args.sha256)
        elif args.group == "bootstrap":
            if args.action == "wizard":
                from nomiarch.bootstrap.wizard import run
                value = run(args.output, args.state_dir, args.repo, args.action)
            elif args.action == "init":
                wizard(args.target, args.output)
            else:
                from nomiarch.bootstrap.controller import dispatch
                value = dispatch(args)
        else:
            from nomiarch.bootstrap.host import dispatch
            value = dispatch(args)
        if value is not None:
            print(json.dumps(value, indent=2))
        if isinstance(value, dict) and (value.get("validation", {}).get("status") == "failed" or value.get("cleanup", {}).get("status") == "failed"):
            return 1
        if isinstance(value, dict) and value.get("backup_export_error"):
            return 1
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Interrupted. Inspect the retained run report and cleanup status.", file=sys.stderr)
        return 130
    except (NomiarchError, OSError, ValueError, KeyError) as e:
        print("Error: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
