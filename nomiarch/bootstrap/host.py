import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import tarfile
import time

from nomiarch.bootstrap import bundle, recovery
from nomiarch.bootstrap.manifests import dns_config, render
from nomiarch.common import NomiarchError, Runner, atomic_write, digest, file_lock, private_dir, read_json, write_json

ROOT = Path("/var/lib/nomiarch")
K3S = "/usr/local/bin/k3s"
AGE = "/usr/local/bin/nomiarch-age"


def kubectl(runner, *args, **kwargs):
    return runner.run([K3S, "kubectl", "--kubeconfig=/etc/rancher/k3s/k3s.yaml", *args], **kwargs)


def wait_for_cluster(runner, timeout=300):
    deadline = time.monotonic() + timeout
    last = "node has not registered"
    while time.monotonic() < deadline:
        try:
            node = json.loads(kubectl(runner, "get", "node", "nomiarch", "-o", "json", timeout=20))
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", []))
            if ready:
                # Wait until the packaged DNS addon has created its resources
                # before replacing its default forwarding configuration.
                kubectl(runner, "-n", "kube-system", "get", "deployment", "coredns", "-o", "name", timeout=20)
                kubectl(runner, "-n", "kube-system", "get", "configmap", "coredns", "-o", "name", timeout=20)
                return
        except NomiarchError as e:
            last = str(e)
        time.sleep(2)
    raise NomiarchError("K3s node/DNS registration did not become ready: " + last)


def preflight(manifest):
    if os.geteuid() != 0:
        raise NomiarchError("Host lifecycle requires root on a dedicated supported guest")
    os_release = platform.freedesktop_os_release()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "24.04" or arch != manifest["architecture"]:
        raise NomiarchError("Bundle requires Ubuntu 24.04 with matching CPU architecture")
    for command in ["systemctl", "ip", "openssl", "mount", "findmnt", "tar", "sh"]:
        if not shutil.which(command):
            raise NomiarchError("Missing prepositioned guest dependency: " + command)
    if not Path("/run/systemd/system").exists():
        raise NomiarchError("The reference guest must run systemd")
    memory_kib = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:")))
    if memory_kib < 3.5 * 1024**2:
        raise NomiarchError("At least a 4 GiB guest is required; 8 GiB is recommended")
    if Path(K3S).exists() and not (ROOT / "owner.json").exists():
        raise NomiarchError("An existing non-Nomiarch K3s installation cannot be adopted")


def data_disk(device, runner):
    if device != "/dev/disk/azure/scsi1/lun0":
        raise NomiarchError("Only the new Azure reference data disk at LUN 0 can be initialized")
    actual = Path(device).resolve(strict=True)
    current = runner.run(["findmnt", "--json", "--mountpoint", ROOT], check=False)
    if current.strip():
        mounted = json.loads(current)["filesystems"][0]["source"]
        if Path(mounted).resolve() != actual:
            raise NomiarchError("Data root is already mounted from a different device")
        return
    if ROOT.exists() and any(ROOT.iterdir()):
        raise NomiarchError("Refusing to mount over an existing Nomiarch data directory")
    devices = json.loads(runner.run(["lsblk", "--json", "--output", "TYPE,MOUNTPOINT", actual]))["blockdevices"]
    if len(devices) != 1 or devices[0]["type"] != "disk" or devices[0].get("children") or devices[0].get("mountpoint"):
        raise NomiarchError("Data disk is partitioned, mounted, or not a whole disk")
    signatures = json.loads(runner.run(["wipefs", "--no-act", "--json", actual]))
    if signatures.get("signatures"):
        raise NomiarchError("Disk contains an existing signature; refusing formatting. Mount it explicitly for recovery.")
    runner.run(["mkfs.ext4", "-m", "0", actual], timeout=300)
    ROOT.mkdir(parents=True, exist_ok=True)
    runner.run(["mount", actual, ROOT])
    uuid = runner.run(["blkid", "-s", "UUID", "-o", "value", actual]).strip()
    if not uuid or any(c not in "0123456789abcdefABCDEF-" for c in uuid):
        raise NomiarchError("Cannot establish data disk filesystem UUID")
    fstab = Path("/etc/fstab")
    atomic_write(fstab, fstab.read_text().rstrip() + f"\nUUID={uuid} {ROOT} ext4 defaults 0 2\n", 0o644)


def install(bundle_path, trusted_key, action="install", device=None, recipient=None, backup_output=None):
    runner = Runner()
    archive = Path(bundle_path).resolve()
    trusted_key = Path(trusted_key).resolve()
    # Signature and every payload byte are checked before executing bundled code.
    with bundle.verified(archive, trusted_key) as (release, manifest):
        preflight(manifest)
        with file_lock("/run/nomiarch-install.lock", blocking=False):
            if device:
                data_disk(device, runner)
            private_dir(ROOT)
            current = read_json(ROOT / "installation.json") if (ROOT / "installation.json").exists() else None
            if current and current["k3s_version"] != manifest["k3s_version"]:
                raise NomiarchError("Automatic K3s version migration is not admitted in this preview; restore/reprovision using the documented procedure")
            if current and action == "install" and current["active_release"] != manifest["manifest_sha256"]:
                raise NomiarchError("Use upgrade with a recovery recipient to change an existing release")
            if action == "upgrade" and not current:
                raise NomiarchError("There is no installed release to upgrade")
            if action == "repair" and current and current["active_release"] != manifest["manifest_sha256"]:
                raise NomiarchError("Repair requires the currently installed signed release")
            required = sum(f["size"] for f in manifest["files"].values()) * 3
            if shutil.disk_usage(ROOT).free < required:
                raise NomiarchError("Insufficient free disk for release staging, images, and model")
            write_json(ROOT / "journal.json", {"phase": "verified", "candidate": manifest["manifest_sha256"], "started": time.time()})
            if action == "upgrade":
                if not recipient or not backup_output:
                    raise NomiarchError("Upgrade requires an encrypted backup output and recipient")
                # Stop writers before snapshotting. A failed upgrade remains recoverable.
                kubectl(runner, "-n", "nomiarch", "scale", "deployment/worker", "deployment/core", "--replicas=0")
                kubectl(runner, "-n", "nomiarch", "wait", "--for=delete", "pod", "-l", "app=core", "--timeout=120s")
                recovery.export(ROOT, backup_output, recipient, AGE)
            if not (ROOT / "owner.json").exists():
                write_json(ROOT / "owner.json", {"product": "nomiarch", "scope": "dedicated single-node guest", "created": time.time()})
            identities_path = ROOT / "identities.json"
            if not identities_path.exists():
                write_json(identities_path, {k: secrets.token_urlsafe(32) for k in ("operator", "worker", "broker")})
            identities = read_json(identities_path)
            for name in ("core", "model"):
                directory = ROOT / name
                directory.mkdir(exist_ok=True, mode=0o700)
                os.chown(directory, 10001, 10001)
                for p in directory.glob("*"):
                    if p.is_file():
                        os.chown(p, 10001, 10001)
            candidate_model = ROOT / "model/model.gguf.new"
            shutil.copyfile(release / "model.gguf", candidate_model)
            os.chown(candidate_model, 10001, 10001)
            os.chmod(candidate_model, 0o400)
            os.replace(candidate_model, ROOT / "model/model.gguf")
            # Replace executable inode atomically; truncating a running K3s
            # binary would fail with ETXTBSY during repair/upgrade.
            shutil.copyfile(release / "k3s", K3S + ".new")
            os.chmod(K3S + ".new", 0o755)
            os.replace(K3S + ".new", K3S)
            with tarfile.open(release / "age.tar.gz") as age_archive:
                entry = age_archive.getmember("age/age")
                if not entry.isfile() or entry.size > 100 * 1024**2:
                    raise NomiarchError("Invalid admitted age archive")
                atomic_write(AGE, age_archive.extractfile(entry).read(), 0o755)
            images = ROOT / "k3s/agent/images"
            images.mkdir(parents=True, exist_ok=True)
            for source, dest in [("k3s-images.tar.zst", "k3s.tar.zst"), ("workload-images.tar", "nomiarch.tar")]:
                shutil.copyfile(release / source, images / dest)
            config_dir = Path("/etc/rancher/k3s")
            config_dir.mkdir(parents=True, exist_ok=True)
            private_dir("/etc/nomiarch")
            # Packaged DNS must never inherit a connected host's upstream resolver,
            # including during reboot before the local DNS ConfigMap is restored.
            atomic_write("/etc/nomiarch/resolv.conf", "nameserver 127.0.0.1\n")
            atomic_write(config_dir / "config.yaml", f"data-dir: {ROOT}/k3s\nnode-name: nomiarch\nwrite-kubeconfig-mode: '0600'\n"
                         "secrets-encryption: true\ndisable-default-registry-endpoint: true\n"
                         "resolv-conf: /etc/nomiarch/resolv.conf\n"
                         "disable:\n  - traefik\n  - servicelb\n  - local-storage\n  - metrics-server\n")
            runner.run(["sh", release / "install-k3s.sh"], env={"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                       "INSTALL_K3S_SKIP_DOWNLOAD": "true", "INSTALL_K3S_SKIP_SELINUX_RPM": "true", "INSTALL_K3S_EXEC": "server"}, timeout=600)
            # A same-version reinstall still needs image import and model refresh.
            runner.run(["systemctl", "restart", "k3s"], timeout=300)
            wait_for_cluster(runner)
            write_json(ROOT / "dns.json", dns_config())
            kubectl(runner, "apply", "-f", ROOT / "dns.json")
            manifest["policy"] = (release / "tools.rego").read_text()
            write_json(ROOT / "runtime.json", render(manifest, identities))
            kubectl(runner, "apply", "-f", ROOT / "runtime.json")
            kubectl(runner, "-n", "kube-system", "rollout", "restart", "deployment/coredns")
            kubectl(runner, "-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=180s", timeout=210)
            kubectl(runner, "-n", "nomiarch", "rollout", "restart", "deployment/core", "deployment/broker", "deployment/model", "deployment/worker")
            for name in ("core", "broker", "model", "worker"):
                kubectl(runner, "-n", "nomiarch", "rollout", "status", "deployment/" + name, "--timeout=600s", timeout=630)
            install_units(runner)
            report = verify(runner)
            releases = private_dir(ROOT / "releases")
            retained = releases / (manifest["manifest_sha256"] + ".tar")
            if archive != retained.resolve():
                shutil.copyfile(archive, retained)
            state = {"active_release": manifest["manifest_sha256"], "version": manifest["version"], "k3s_version": manifest["k3s_version"],
                     "architecture": manifest["architecture"], "installed": time.time(), "release_key_sha256": digest(trusted_key), "schema": 1}
            write_json(ROOT / "installation.json", state)
            write_json(ROOT / "verification.json", report)
            write_json(ROOT / "journal.json", {"phase": "ready", "release": state["active_release"]})
            return {"installation": state, "verification": report, "backup": backup_output}


def install_units(runner):
    atomic_write("/etc/systemd/system/nomiarch-dns.service", """[Unit]
Description=Nomiarch internal DNS configuration
After=k3s.service
Requires=k3s.service
PartOf=k3s.service
RequiresMountsFor=/var/lib/nomiarch
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml apply -f /var/lib/nomiarch/dns.json
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
""", 0o644)
    atomic_write("/etc/systemd/system/nomiarch-api.service", """[Unit]
Description=Nomiarch operator API on guest loopback
After=k3s.service nomiarch-dns.service
Requires=k3s.service
RequiresMountsFor=/var/lib/nomiarch
[Service]
ExecStart=/usr/local/bin/k3s kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml -n nomiarch port-forward --address=127.0.0.1 service/core 8787:8787
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
""", 0o644)
    directory = Path("/etc/systemd/system/k3s.service.d")
    directory.mkdir(exist_ok=True)
    atomic_write(directory / "nomiarch-storage.conf", "[Unit]\nRequiresMountsFor=/var/lib/nomiarch\n", 0o644)
    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", "--now", "nomiarch-dns", "nomiarch-api"])


def verify(runner=None):
    runner = runner or Runner()
    core = json.loads(kubectl(runner, "-n", "nomiarch", "exec", "deployment/core", "--",
                              "python3", "-m", "nomiarch.smoke", timeout=180))
    boundary = json.loads(kubectl(runner, "-n", "nomiarch", "exec", "deployment/worker", "--",
                                  "python3", "-m", "nomiarch.smoke", "--boundary", timeout=60))
    return {"core": core, "worker_boundary": boundary}


def dispatch(args):
    if os.geteuid() != 0:
        raise NomiarchError("Host operations require root")
    if args.action == "verify":
        return verify()
    if args.action == "backup":
        return recovery.export(ROOT, args.output, args.recipient, AGE)
    return install(args.bundle, args.trusted_key, args.action, args.data_device,
                   getattr(args, "recipient", None), getattr(args, "output", None))
