"""Fetch exact test tool versions on connected Linux/amd64 CI runners."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import urllib.request

root = Path(__file__).resolve().parent.parent
destination = Path(sys.argv[1]).resolve()
destination.mkdir(parents=True, exist_ok=True)
components = json.loads((root / "packages/components.json").read_text())["architectures"]["amd64"]
items = dict(components)
items = {name: items[name] for name in ("opa", "age.tar.gz")}
items["tofu.tar.gz"] = {"url": "https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.tar.gz",
                        "sha256": "50a6106fa4de523d09c87af85f3db1dd47535fc005727fdca6852146476b88ec"}
for name, spec in items.items():
    path = destination / name
    with urllib.request.urlopen(spec["url"], timeout=60) as response, path.open("wb") as f:
        shutil.copyfileobj(response, f)
    with path.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != spec["sha256"]:
            raise SystemExit("CI tool checksum mismatch: " + name)
    if name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            entries = ["tofu"] if name.startswith("tofu") else ["age/age", "age/age-keygen"]
            for entry in entries:
                binary = destination / Path(entry).name
                binary.write_bytes(archive.extractfile(entry).read())
                binary.chmod(0o755)
    else:
        path.chmod(0o755)
print("Verified OpenTofu, OPA, and age test tools")
