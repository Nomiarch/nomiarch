"""Plan once, explicitly approve, then execute exactly that private saved plan."""
import copy
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import time

from nomiarch.bootstrap import bundle, controller
from nomiarch.bootstrap.config import files_preflight, require, validate
from nomiarch.bootstrap.providers import get_provider
from nomiarch.common import NomiarchError, Runner, atomic_write, canonical, digest, file_lock, private_dir, read_json, write_json
from . import network, scaffold


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def public_plan(value, sensitive=False):
    if sensitive is True: return '[sensitive value omitted]'
    if isinstance(value, dict):
        return {k: public_plan(v, True if k in {'custom_data', 'admin_password', 'password', 'private_key', 'token', 'secret'} else
                              sensitive.get(k, False) if isinstance(sensitive, dict) else False) for k, v in value.items()}
    if isinstance(value, list):
        return [public_plan(v, sensitive[i] if isinstance(sensitive, list) and i < len(sensitive) else False) for i, v in enumerate(value)]
    return value


def freeze(paths, expected, directory):
    directory = private_dir(directory)
    result = {}
    for role, sha in expected.items():
        name = {'bundle': 'release.tar', 'key': 'release-public.pem', 'image': 'ubuntu.img'}[role]
        path = directory / name
        shutil.copyfile(paths[role], path)
        path.chmod(0o600)
        require(digest(path) == sha, 'A release file changed while it was being admitted')
        result[role] = str(path.resolve())
    return result


def seal_key(state_dir):
    path = Path(state_dir) / 'approval.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, 'wb') as stream:
        key = secrets.token_bytes(32)
        stream.write(key)
    return key


def sign(state_dir, value):
    return hmac.new(seal_key(state_dir), canonical(value), hashlib.sha256).hexdigest()


def inputs(directory):
    directory = Path(directory)
    # Exclude output/state files, which the provider writes during application.
    files = {}
    for p in sorted(directory.rglob('*')):
        rel = p.relative_to(directory)
        if p.is_symlink():
            if '.terraform' in rel.parts: continue  # init's module symlinks are not customer inputs
            raise NomiarchError('Symlinks are not accepted in deployment inputs')
        if not p.is_file() or any(x in rel.parts for x in ('logs', '.terraform')): continue
        if (p.suffix in {'.tf', '.tfplan'} or p.name.endswith('.tfvars.json')
                or p.name in {'.terraform.lock.hcl', 'ssh-host', 'ssh-host.pub', 'ssh-client', 'ssh-client.pub'}):
            files[rel.as_posix()] = digest(p)
    return files


def prepare(project_dir, state_dir, source, paths, *, factory=get_provider):
    value, snapshot = scaffold.load_supported(project_dir, source)
    require(value['repository']['mode'] != 'offline',
            'Offline organisation scaffolds can be exported. Connect an approved internal review integration before deployment; this preview cannot verify internal approvals.')
    config = copy.deepcopy(value['configuration'])
    require(value['runtime'] == scaffold.admitted_runtime(config['architecture']), 'Use the matching desktop release for these Core pins, or generate a configuration for this app’s admitted release')
    if config['target'] == 'local': config['local']['image'] = str(Path(paths['image']).resolve())
    validate(config)
    for role in ('bundle', 'key'):
        field = 'bundle_sha256' if role == 'bundle' else 'release_key_sha256'
        require(digest(paths[role]) == value['runtime'][field], 'Release files do not match the scaffold')
    manifest = bundle.inspect(paths['bundle'], paths['key'])
    require(manifest['architecture'] == config['architecture'], 'Release architecture mismatch')
    require(manifest['version'] == value['runtime']['core_version'], 'Release version does not match the customer configuration')
    if config['target'] == 'azure':
        require(bool(config['azure'].get('subnet_id')),
                'Azure Core setup needs a private administration route and an existing subnet. The scaffold can also be exported for your cloud administrator.')
    directory, run = controller.new_run(config, state_dir, 'core')
    try:
        expected = {'bundle': value['runtime']['bundle_sha256'], 'key': value['runtime']['release_key_sha256']}
        if config['target'] == 'local':
            require(config['local']['image_sha256'] == scaffold.PINS[config['architecture']]['image'][1], 'Use the matching desktop recipe for this Ubuntu image')
            expected['image'] = config['local']['image_sha256']
        paths = freeze(paths, expected, directory / 'admitted')
        if config['target'] == 'local': config['local']['image'] = paths['image']
        if config['target'] == 'azure':
            from .ssh_keys import generate
            key = generate(directory / 'ssh-client')
            config['azure']['ssh_key'], config['azure']['ssh_public_key'] = str(key), str(key) + '.pub'
        files_preflight(config)
        run['foundation'] = {'id': value['id'], 'project_directory': str(Path(project_dir).resolve()),
                             'snapshot': snapshot, 'isolation': value['isolation']}
        provider = factory(config, run, directory, Runner(log_dir=directory / 'logs'), source)
        provider.prepare()
        summary = {'system': config['name'], 'destination': config['target'], 'capacity': config['capacity'],
                   'organisation': value['organisation'], 'environment': value['environment'],
                   'isolation': value['isolation'], 'core_version': value['runtime']['core_version'],
                   'action': 'Create one retained VM and install Nomiarch',
                   'network': 'Guest outbound deny; host administration remains' if config['target'] == 'local' else 'Private VM; Azure platform dependencies remain',
                   'charges': 'Azure resources incur charges until explicitly removed' if config['target'] == 'azure' else 'Uses this computer’s resources'}
        if config['target'] == 'azure':
            raw = provider.command(['tofu', 'show', '-json', 'apply.tfplan'], cwd=directory / 'infra', timeout=120)
            import json
            tf = json.loads(raw)
            summary['azure'] = {k: v for k, v in config['azure'].items() if k not in {'ssh_key', 'ssh_public_key'}}
            summary['resource_group'] = run['prefix'] + '-rg'
            summary['resources'] = [{'address': r['address'], 'actions': r['change']['actions'],
                                     'planned': public_plan(r['change'].get('after'), r['change'].get('after_sensitive', False))}
                                    for r in tf.get('resource_changes', [])]
            require(all(r['actions'] in (['create'], ['no-op'], ['read']) for r in summary['resources']), 'Initial plan unexpectedly changes existing resources')
        record = {'schema': 1, 'run_id': run['id'], 'configuration': config, 'snapshot': snapshot,
                  'project_directory': str(Path(project_dir).resolve()), 'inputs': inputs(directory),
                  'artifacts': {role: {'path': str(Path(paths[role]).resolve()), 'sha256': digest(paths[role])} for role in ('bundle', 'key')},
                  'created': time.time(), 'expires': time.time() + 3600, 'summary': summary}
        record['digest'] = fingerprint(record)
        record['seal'] = sign(state_dir, record)
        write_json(directory / 'plan.json', record)
        run['phase'] = 'awaiting-approval'
        controller.save(directory, run)
        return {'run_directory': str(directory), 'plan': record}
    except BaseException:
        run['phase'] = 'planning-failed'
        controller.save(directory, run)
        raise


def validate_plan(directory, source):
    directory = Path(directory).resolve()
    record = read_json(directory / 'plan.json')
    sealed = dict(record); supplied = sealed.pop('seal')
    require(hmac.compare_digest(supplied, sign(directory.parent.parent, sealed)), 'Plan integrity check failed')
    unsigned = dict(sealed); supplied_digest = unsigned.pop('digest')
    require(fingerprint(unsigned) == supplied_digest, 'Plan digest mismatch')
    run = read_json(directory / 'run.json')
    require(run['id'] == directory.name == record['run_id'], 'Plan/run identity mismatch')
    require(run['prefix'] == 'nomiarch-' + run['id'][:16], 'VM ownership prefix changed after planning')
    require(run['phase'] == 'awaiting-approval' and not run['creation_started'], 'This plan was already used or cannot be applied')
    require(run['config'] == record['configuration'], 'Configuration changed after planning')
    require(time.time() < record['expires'], 'This plan expired. Prepare and review a new plan.')
    value, snapshot = scaffold.load_supported(record['project_directory'], source)
    require(snapshot == record['snapshot'], 'Customer configuration changed. Prepare and review a new plan.')
    require(run['foundation']['id'] == value['id'] and run['foundation']['snapshot'] == snapshot
            and run['foundation']['project_directory'] == record['project_directory'], 'Foundation binding changed after planning')
    require(inputs(directory) == record['inputs'], 'Deployment inputs changed. Prepare a new plan.')
    for artifact in record['artifacts'].values():
        require(digest(artifact['path']) == artifact['sha256'], 'An admitted release file changed')
    files_preflight(run['config'])
    return run, record, value


def apply(directory, source, expected_digest, *, repository_check=None, factory=get_provider):
    """Called by the desktop's explicit Approve and create action, never by Core."""
    directory = Path(directory).resolve()
    with file_lock(directory / 'controller.lock', blocking=False):
        run, record, value = validate_plan(directory, source)
        require(hmac.compare_digest(record['digest'], expected_digest), 'Displayed approval does not match the saved plan')
        evidence = {'mode': 'local-human', 'time': time.time(), 'digest': expected_digest}
        if value['repository']['mode'] == 'github':
            require(repository_check is not None, 'A current human-reviewed repository change is required')
            evidence['repository'] = repository_check(value, record['snapshot'])
        require(value['usage'] != 'organisation' or 'repository' in evidence, 'Organisation approval cannot be bypassed by a local confirmation')
        write_json(directory / 'approval.json', evidence)
        if 'repository' in evidence: run['foundation']['repository_commit'] = evidence['repository']['merge']
        provider = factory(run['config'], run, directory, Runner(deadline=time.time() + 7200, log_dir=directory / 'logs'), source)
        run['phase'], run['creation_started'] = 'creating', True
        controller.save(directory, run)
        try:
            run['inventory'] = provider.provision()  # Never re-plan after approval.
            run['phase'] = 'provisioned'
            controller.save(directory, run)
            checks = {'foundation': provider.verify()}
            if run['config']['target'] == 'local': checks['network'] = network.configure(provider)
            checks['core'] = controller.install_remote(provider, directory, run, record['artifacts']['bundle']['path'], record['artifacts']['key']['path'])
            if run['config']['target'] == 'local':
                # K3s can insert its own chains during installation. Reassert our
                # boundary first, then test both runtime and host egress.
                provider.remote(['sudo', '-n', '/usr/local/sbin/nomiarch-egress', 'install'])
                checks['network'] = network.verify(provider)
            run['validation'] = {'status': 'passed', 'scope': 'core', 'checks': checks}
            run['phase'] = 'ready'
        except BaseException as e:
            run['validation'] = {'status': 'failed', 'scope': 'core', 'error': str(e)}
            run['phase'] = 'failed'
        finally:
            controller.save(directory, run)
            write_json(directory / 'report.json', run)
        return {'run_directory': str(directory), 'validation': run['validation'], 'cleanup': run['cleanup'], 'inventory': run['inventory']}
