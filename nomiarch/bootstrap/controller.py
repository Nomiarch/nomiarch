import json
import os
from pathlib import Path
import re
import socket
import time
import uuid

from nomiarch.bootstrap import bundle
from nomiarch.bootstrap.config import files_preflight, load, validate
from nomiarch.bootstrap.providers import get_provider
from nomiarch.common import NomiarchError, Runner, digest, file_lock, private_dir, read_json, write_json


def new_run(config, state_dir, scope):
    run_id = uuid.uuid4().hex
    directory = private_dir(Path(state_dir) / "runs" / run_id)
    now = time.time()
    run = {"schema": 1, "id": run_id, "prefix": "nomiarch-" + run_id[:16], "config": config,
           "created": now, "expires_at": now + config["lifecycle"]["max_runtime_minutes"] * 60,
           "scope": scope, "destroy_after": config["lifecycle"]["destroy_after"], "phase": "recorded",
           "creation_started": False, "controller": socket.gethostname(),
           "validation": {"status": "pending"}, "cleanup": {"status": "not-requested"},
           "inventory": None}
    write_json(directory / "run.json", run)
    return directory, run


def save(directory, run):
    write_json(Path(directory) / "run.json", run)


def verify_worker(state_dir, config):
    worker = config["lifecycle"].get("cleanup_worker_id")
    if not worker:
        raise NomiarchError("Unattended tests require lifecycle.cleanup_worker_id and an independent cleanup controller")
    heartbeat = read_json(Path(state_dir) / "workers" / (worker + ".json"))
    age = time.time() - heartbeat["time"]
    if not (0 <= age <= 60) or heartbeat["host"] == socket.gethostname():
        raise NomiarchError("Cleanup worker heartbeat is stale or is on the initiating host")
    # Operator-declared host separation is a prerequisite, not a fault-domain proof.
    return {"worker": worker, "host": heartbeat["host"], "heartbeat": heartbeat["time"]}


def cleanup(directory, run, repo, *, factory=get_provider):
    if run["config"]["target"] == "existing":
        raise NomiarchError("Existing hosts are outside the destruction scope")
    run["cleanup"] = {"status": "running", "started": time.time()}
    # Evidence persistence failure must not strand already-created test resources.
    try:
        save(directory, run)
    except OSError:
        pass
    provider = factory(run["config"], run, directory, Runner(deadline=time.time() + 2100, log_dir=Path(directory) / "logs"), repo)
    try:
        run["cleanup"] = provider.destroy()
        run["phase"] = "destroyed"
    except Exception as e:
        run["cleanup"] = {"status": "failed", "error": str(e), "retry": "nomiarch bootstrap destroy --run " + str(directory)}
    save(directory, run)
    return run


def install_remote(provider, directory, run, archive, key, action="install", recipient=None):
    with bundle.verified(archive, key) as (release, manifest):
        if manifest["architecture"] != run["config"]["architecture"]:
            raise NomiarchError("Bundle and environment architectures differ")
        temp = provider.remote(["mktemp", "-d", "/tmp/nomiarch-XXXXXXXXXX"]).strip()
        if not re.fullmatch(r"/tmp/nomiarch-[A-Za-z0-9]{10}", temp):
            raise NomiarchError("Guest did not return a valid private staging path")
        backup = f"/var/backups/nomiarch/{run['id']}-{time.time_ns()}.zip.age" if action == "upgrade" else None
        try:
            for source, name in [(archive, "release.tar"), (key, "trusted.pem"), (release / "installer.pyz", "installer.pyz")]:
                provider.transfer(source, temp + "/" + name)
            args = ["sudo", "-n", "python3", temp + "/installer.pyz", "host", action,
                    "--bundle", temp + "/release.tar", "--trusted-key", temp + "/trusted.pem"]
            if run["config"]["target"] == "azure" and action == "install":
                args += ["--data-device", run["inventory"]["data_device"]]
            if action == "upgrade":
                args += ["--recipient", recipient, "--output", backup]
            value = json.loads(provider.remote(args, timeout=2400))
            return value
        finally:
            if backup:
                # Retain the encrypted pre-upgrade snapshot on the controller even
                # when the upgrade itself fails. Failure here remains visible.
                try:
                    provider.remote(["sudo", "-n", "cp", backup, temp + "/recovery.zip.age"])
                    provider.remote(["sudo", "-n", "chmod", "0644", temp + "/recovery.zip.age"])
                    provider.fetch(temp + "/recovery.zip.age", Path(directory) / (Path(backup).name))
                    write_json(Path(directory) / (Path(backup).name + ".receipt.json"), {
                        "sha256": digest(Path(directory) / Path(backup).name), "file": Path(backup).name})
                except Exception as e:
                    run["backup_export_error"] = str(e)
                    save(directory, run)
            try:
                provider.remote(["rm", "-rf", "--", temp], timeout=30)
            except Exception:
                pass  # No billable resource may be stranded by staging cleanup.


def apply(config, state_dir, scope, archive=None, key=None, unattended=False, repo=".", factory=get_provider):
    validate(config)
    files_preflight(config)
    if scope == "core":
        if config["target"] == "azure" and not config["azure"].get("subnet_id"):
            raise NomiarchError("Full Azure installation requires an existing privately routed azure.subnet_id. Use --scope foundation to test an isolated new VNet.")
        if not archive or not key:
            raise NomiarchError("Core installation requires --bundle and --trusted-key before provisioning")
        manifest = bundle.inspect(archive, key)
        if manifest["architecture"] != config["architecture"]:
            raise NomiarchError("Bundle architecture does not match the environment")
    worker = None
    if unattended and config["lifecycle"]["destroy_after"]:
        if config["target"] != "azure":
            raise NomiarchError("Unattended destroy-after is supported only with an independent Azure cleanup controller")
        worker = verify_worker(state_dir, config)
    directory, run = new_run(config, state_dir, scope)
    run["cleanup_worker"] = worker
    save(directory, run)
    print("Run directory: " + str(directory), flush=True)
    if config["target"] == "azure":
        print("Azure resources can incur charges until deletion completes. Keep this controller running for attended tests.", flush=True)
    with file_lock(directory / "controller.lock", blocking=False):
        provider = factory(config, run, directory, Runner(deadline=run["expires_at"], log_dir=directory / "logs"), repo)
        try:
            provider.prepare()
            run["phase"] = "creating"
            run["creation_started"] = True
            save(directory, run)
            run["inventory"] = provider.provision()
            run["phase"] = "provisioned"
            save(directory, run)
            checks = {"foundation": provider.verify()}
            if scope == "core":
                checks["core"] = install_remote(provider, directory, run, archive, key)
            run["validation"] = {"status": "passed", "scope": scope, "checks": checks}
            run["phase"] = "ready"
        except BaseException as e:
            run["validation"] = {"status": "failed", "scope": scope, "error": type(e).__name__ + ": " + str(e)}
            run["phase"] = "failed"
        finally:
            try:
                save(directory, run)
            finally:
                if run["destroy_after"] and run["creation_started"]:
                    cleanup(directory, run, repo, factory=factory)
            write_json(directory / "report.json", run)
    return {"run_directory": str(directory), "validation": run["validation"], "cleanup": run["cleanup"], "inventory": run["inventory"]}


def plan(config, state_dir, repo, provider_plan=False):
    value = {"target": config["target"], "architecture": config["architecture"],
             "foundation": "dedicated Ubuntu 24.04 VM" if config["target"] != "existing" else "adopt dedicated Ubuntu 24.04 host",
             "runtime": "same signed K3s/core/policy/model bundle on every target", "lifecycle": config["lifecycle"],
             "creates_resources": False, "network": "private Azure administration route required for core installation" if config["target"] == "azure" else "site/host boundary must be validated"}
    if provider_plan:
        files_preflight(config)
        directory, run = new_run(config, state_dir, "plan")
        # A plan never authorizes a future automatic deletion.
        run["destroy_after"] = False
        run["phase"] = "plan-only"
        save(directory, run)
        provider = get_provider(config, run, directory, Runner(log_dir=directory / "logs"), repo)
        provider.prepare()
        value["provider_plan_directory"] = str(directory)
        value["provider_checks"] = "passed; saved plan and private logs retained"
    return value


def reap(state_dir, worker_id, watch, repo):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", worker_id):
        raise NomiarchError("Invalid cleanup worker ID")
    state_dir = private_dir(state_dir)
    while True:
        write_json(state_dir / "workers" / (worker_id + ".json"), {"worker": worker_id, "host": socket.gethostname(), "time": time.time()})
        for path in sorted((state_dir / "runs").glob("*/run.json")):
            run = read_json(path)
            if (not run["destroy_after"] or not run["creation_started"] or run["expires_at"] > time.time()
                    or run["cleanup"].get("status") == "deleted" or run["config"]["target"] != "azure"):
                continue
            expected = run["config"]["lifecycle"].get("cleanup_worker_id")
            if expected and expected != worker_id:
                continue
            try:
                with file_lock(path.parent / "controller.lock", blocking=False):
                    # Refresh after acquiring the cross-controller operation lock.
                    run = read_json(path)
                    result = cleanup(path.parent, run, repo)
                    print(json.dumps({"run": run["id"], "cleanup": result["cleanup"]}), flush=True)
            except NomiarchError as e:
                print(str(e), flush=True)
        if not watch:
            return {"worker": worker_id, "scan": "completed"}
        time.sleep(15)


def dispatch(args):
    if args.action in {"plan", "apply"}:
        config = load(args.config)
        if args.target and args.target != config["target"]:
            raise NomiarchError("--target and configuration disagree")
        if args.action == "plan":
            return plan(config, args.state_dir, args.repo, args.provider_plan)
        if args.destroy_after:
            config["lifecycle"]["destroy_after"] = True
        return apply(config, args.state_dir, args.scope, args.bundle, args.trusted_key, args.unattended, args.repo)
    if args.action == "reap":
        return reap(args.state_dir, args.worker_id, args.watch, args.repo)
    directory = Path(args.run).expanduser().resolve()
    run = read_json(directory / "run.json")
    if directory.name != run["id"] or run["prefix"] != "nomiarch-" + run["id"][:16]:
        raise NomiarchError("Run identity mismatch")
    validate(run["config"])
    if args.action == "status":
        return run
    with file_lock(directory / "controller.lock", blocking=False):
        run = read_json(directory / "run.json")
        if args.action == "destroy":
            if not run["creation_started"]:
                raise NomiarchError("This run never started resource creation")
            result = cleanup(directory, run, args.repo)
            return {"validation": result["validation"], "cleanup": result["cleanup"]}
        if run["cleanup"].get("status") == "deleted" or not run.get("inventory"):
            raise NomiarchError("This run has no retained provisioned host")
        provider = get_provider(run["config"], run, directory, Runner(log_dir=directory / "logs"), args.repo)
        if args.action == "backup":
            return backup_remote(provider, directory, run, args.backup_recipient)
        if args.action == "upgrade":
            # A controller copy must exist before changing the installed release.
            run["pre_upgrade_backup"] = backup_remote(provider, directory, run, args.backup_recipient)
            save(directory, run)
        result = install_remote(provider, directory, run, args.bundle, args.trusted_key, args.action, getattr(args, "backup_recipient", None))
        run["phase"] = "ready"
        run["validation"] = {"status": "passed", "scope": "core", "checks": result}
        save(directory, run)
        return {"run_directory": str(directory), "result": result, "backup_export_error": run.get("backup_export_error")}


def backup_remote(provider, directory, run, recipient):
    # Use the currently running core image for the stdlib snapshot code. The host
    # installer zipapp is retained by extracting it from the active signed release.
    script = "from pathlib import Path; import json,tarfile; r=Path('/var/lib/nomiarch'); s=json.loads((r/'installation.json').read_text()); " \
             "t=tarfile.open(r/'releases'/(s['active_release']+'.tar')); Path('/run/nomiarch-backup.pyz').write_bytes(t.extractfile('installer.pyz').read())"
    provider.remote(["sudo", "-n", "python3", "-c", script])
    output = f"/var/backups/nomiarch/{run['id']}-{time.time_ns()}.zip.age"
    receipt = json.loads(provider.remote(["sudo", "-n", "python3", "/run/nomiarch-backup.pyz", "host", "backup",
                                         "--recipient", recipient, "--output", output], timeout=1800))
    temp = provider.remote(["mktemp", "-d", "/tmp/nomiarch-XXXXXXXXXX"]).strip()
    if not re.fullmatch(r"/tmp/nomiarch-[A-Za-z0-9]{10}", temp):
        raise NomiarchError("Invalid backup transfer staging path")
    try:
        provider.remote(["sudo", "-n", "cp", output, temp + "/backup.age"])
        provider.remote(["sudo", "-n", "chmod", "0644", temp + "/backup.age"])
        local = directory / Path(output).name
        provider.fetch(temp + "/backup.age", local)
        if digest(local) != receipt["sha256"]:
            raise NomiarchError("Transferred backup checksum differs from the guest receipt")
        receipt["file"] = str(local)
        write_json(local.with_name(local.name + ".receipt.json"), receipt)
        return receipt
    finally:
        provider.remote(["rm", "-rf", "--", temp])
