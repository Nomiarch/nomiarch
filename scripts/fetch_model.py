"""Connected staging helper; never used by a guest or runtime service."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.request

spec = json.loads((Path(__file__).resolve().parent.parent / "packages/reference-model.json").read_text())
output = Path(sys.argv[1]).expanduser().resolve()
if output.exists():
    with output.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() == spec["sha256"]:
            print("Reference model already verified: " + str(output))
            raise SystemExit(0)
    raise SystemExit("Output exists with different content; refusing overwrite")
output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
pending = output.with_name(output.name + ".partial")
with pending.open("xb") as f:
    os.chmod(pending, 0o600)
    try:
        with urllib.request.urlopen(spec["url"], timeout=120) as response:
            shutil.copyfileobj(response, f)
    except BaseException:
        pending.unlink()
        raise
with pending.open("rb") as f:
    actual = hashlib.file_digest(f, "sha256").hexdigest()
if actual != spec["sha256"] or pending.stat().st_size != spec["size"]:
    pending.unlink()
    raise SystemExit("Model checksum/size mismatch; bytes were not admitted")
os.replace(pending, output)
print("Verified reference model: " + str(output) + "\nSHA-256: " + actual)
