"""Integration drill for a disposable CI guest with an installed Nomiarch core.

Requires root. Uses only the dedicated /var/lib/nomiarch test installation.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nomiarch.bootstrap import bundle, host, recovery
from nomiarch.common import Runner, read_json

if os.geteuid() != 0:
    raise SystemExit("Run this integration drill as root on the disposable test guest")
repo = Path(__file__).resolve().parent.parent
work = repo / ".nomiarch"
root = Path("/var/lib/nomiarch")
before = read_json(root / "installation.json")
identities = read_json(root / "identities.json")
with sqlite3.connect(root / "core/core.db") as db:
    task_ids = {r[0] for r in db.execute("SELECT id FROM tasks")}

with bundle.verified(work / "release.tar", work / "keys/public.pem") as (release, manifest):
    # A changed, signed policy artifact exercises a real release transition
    # while retaining the admitted application/database/K3s versions.
    policy = release / "tools.rego"
    policy.write_text(policy.read_text() + "\n# Admitted recovery-drill release generation 2\n")
    (release / "manifest.json").unlink()
    (release / "manifest.sig").unlink()
    metadata = {k: v for k, v in manifest.items() if k not in {"files", "manifest_sha256", "schema", "version"}}
    bundle.seal(release, work / "upgrade.tar", work / "keys/private.pem", metadata)
    with tarfile.open(release / "age.tar.gz") as archive:
        age_keygen = work / "age-keygen"
        age_keygen.write_bytes(archive.extractfile("age/age-keygen").read())
        age_keygen.chmod(0o700)

identity = work / "recovery-identity.txt"
runner = Runner()
runner.run([age_keygen, "-o", identity])
recipient = runner.run([age_keygen, "-y", identity]).strip()
backup = work / "pre-upgrade.zip.age"
result = host.install(work / "upgrade.tar", work / "keys/public.pem", "upgrade", recipient=recipient, backup_output=str(backup))
after = read_json(root / "installation.json")
assert after["active_release"] != before["active_release"], "Release did not change"
assert read_json(root / "identities.json") == identities, "Upgrade changed service identities"
with sqlite3.connect(root / "core/core.db") as db:
    assert task_ids <= {r[0] for r in db.execute("SELECT id FROM tasks")}, "Upgrade lost durable tasks"

receipt = read_json(backup.with_name(backup.name + ".receipt.json"))
destination = work / "restored-appliance"
recovery.restore(backup, destination, identity, receipt["sha256"], host.AGE)
assert read_json(destination / "identities.json") == identities
assert read_json(destination / "installation.json")["active_release"] == before["active_release"]
with sqlite3.connect(destination / "core/core.db") as db:
    assert task_ids <= {r[0] for r in db.execute("SELECT id FROM tasks")}
restored_release = destination / "releases" / (before["active_release"] + ".tar")
bundle.inspect(restored_release, work / "keys/public.pem")
print(json.dumps({"upgrade": "passed", "identities": "preserved", "existing_tasks": "preserved",
                  "encrypted_appliance_payload_restore": "passed", "restored_release_signature": "verified",
                  "scope": "Disposable Ubuntu guest; clean-hardware restoration remains a separate release gate"}, indent=2))
