"""Build the small, source-auditable customer controller ZIP (no signing keys)."""
from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from nomiarch import __version__

output = Path(sys.argv[1])
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for folder in ('nomiarch', 'infra', 'packages', 'policies', 'docs'):
        for path in sorted((root / folder).rglob('*')):
            if not path.is_file() or any(part in {'__pycache__', '.terraform'} for part in path.parts) or path.suffix == '.pyc':
                continue
            archive.write(path, 'nomiarch/' + str(path.relative_to(root)))
    archive.write(root / 'README.md', 'nomiarch/README.md')
    archive.writestr('nomiarch/START-HERE.txt', f'''Nomiarch {__version__} — engineering preview
Install Python 3.11+, Multipass and OpenSSL before moving into the isolated environment.
Open a terminal in this folder and run:
    python3 -m nomiarch bootstrap wizard
No pip packages or Docker are needed on the installation controller.
Keep this folder and its .nomiarch state for upgrades, verification and removal.
Get the matching signed bundle, public key and trusted image before installation.
Read docs/customer-guide.md for the complete offline and upgrade steps.
''')
print(output)
