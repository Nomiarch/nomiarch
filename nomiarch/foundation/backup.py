"""Practical local backup of an infrastructure foundation: repository export, controller
records, inventory, trust configuration and logs. Private keys stay out and are listed
for separate protection. This is distinct from Core database backups, which cover
application state only and never recover infrastructure records."""
import os
from pathlib import Path
import shutil
import tempfile
import time
import zipfile

from nomiarch.bootstrap.config import require
from nomiarch.common import Runner, canonical, digest, read_json
from . import scaffold

PRIVATE = {'ssh-client', 'ssh-host', 'approval.key', 'controller.lock'}
PRIVATE_PATTERNS = ('.auto.tfvars.json',)
STATE_PRIVATE = ('terraform.tfstate',)  # may embed generated host identity; exported only when encrypted


def classify(relative):
    name = Path(relative).name
    if name in PRIVATE or any(name.endswith(p) for p in PRIVATE_PATTERNS):
        return 'private'
    if any(name.startswith(p) for p in STATE_PRIVATE) or 'logs' in Path(relative).parts:
        return 'state'  # command logs can echo infrastructure state
    if '.terraform' in Path(relative).parts or name.endswith('.tfplan'):
        return 'skip'
    if 'admitted' in Path(relative).parts:
        return 'artifact'  # large admitted files are re-admitted from the kit, not duplicated here
    return 'record'


def export(run_directory, output, source, recipient=None, age='age'):
    run_dir = Path(run_directory).resolve()
    output = Path(output).resolve()
    require(not output.exists() and not output.is_relative_to(run_dir), 'Choose a new backup file outside the controller directory')
    run = read_json(run_dir / 'run.json')
    require(run.get('foundation'), 'Only customer foundations have a repository export and controller record to back up')
    project = Path(run['foundation']['project_directory'])
    value, snapshot = scaffold.load_supported(project, source)
    require(snapshot == run['foundation']['snapshot'], 'The configuration folder changed outside the review workflow; back up the reviewed folder')
    files, private, deferred, artifacts = {}, [], [], []
    for path in sorted(project.rglob('*')):
        if path.is_file() and '.git' not in path.relative_to(project).parts:
            files['repository/' + path.relative_to(project).as_posix()] = path
    for path in sorted(run_dir.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_dir).as_posix()
        kind = classify(relative)
        if kind == 'private':
            private.append(relative)
        elif kind == 'state':
            if recipient:
                files['controller/' + relative] = path
            else:
                deferred.append(relative)
        elif kind == 'artifact':
            artifacts.append({'file': relative, 'sha256': digest(path)})
        elif kind == 'record':
            files['controller/' + relative] = path
    for name in ('approval.key',):
        if (run_dir.parent.parent / name).is_file():
            private.append('../../' + name)
    manifest = {'schema': 1, 'created': time.time(), 'foundation': value['id'], 'run_id': run['id'], 'phase': run['phase'],
                'scope': run.get('scope'), 'inventory': run.get('inventory'), 'application': run.get('application'),
                'contents': 'repository export, controller records, inventory, verification, trust configuration' + (', encrypted infrastructure state and logs' if recipient else ''),
                'kept_separately': sorted(set(private)), 'exported_only_when_encrypted': sorted(set(deferred)),
                're_admit_from_kit': artifacts,
                'encrypted': bool(recipient),
                'recovery': 'Restore the repository export into the customer Git service, re-enrol trust roots and reviewer accounts, place the controller records under a new private state directory, and restore private keys from their separate protected copy. Verify with Manage → Verify before any new plan. A Core database backup does not restore these records.',
                'files': {name: {'sha256': digest(path), 'size': path.stat().st_size} for name, path in files.items()}}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='nomiarch-foundation-backup-') as tmp:
        archive = Path(tmp) / 'foundation-backup.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for name, path in files.items():
                z.write(path, name)
            z.writestr('recovery.json', canonical(manifest))
        if recipient:
            require(recipient.startswith('age1'), 'Use a dedicated age recipient public key')
            require(shutil.which(age), 'The age tool is required for an encrypted foundation backup')
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
            Runner().run([age, '--encrypt', '--recipient', recipient, '--output', output, archive], timeout=1800)
        else:
            shutil.copyfile(archive, output)
            output.chmod(0o600)
    return {'file': str(output), 'sha256': digest(output), 'manifest': manifest}
