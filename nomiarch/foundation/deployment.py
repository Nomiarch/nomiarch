"""Plan once, explicitly approve, then execute exactly that private saved plan."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import time

from nomiarch.bootstrap import bundle, controller
from nomiarch.bootstrap.config import files_preflight, require, validate
from nomiarch.bootstrap.providers import get_provider
from nomiarch.common import NomiarchError, Runner, atomic_write, canonical, digest, file_lock, private_dir, read_json, write_json
from . import infrastructure, network, scaffold


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
                or p.name in {'.terraform.lock.hcl', 'ssh-host', 'ssh-host.pub', 'ssh-client', 'ssh-client.pub', 'known_hosts'}):
            files[rel.as_posix()] = digest(p)
    return files


def admit_private_file(source, destination, mode=0o600):
    """Copy user-supplied private material into the run directory; never into Git."""
    source = Path(source).expanduser()
    require(source.is_file(), 'Missing file: ' + str(source))
    shutil.copyfile(source, destination)
    destination.chmod(mode)
    return str(destination.resolve())


def foundation_lock(state_dir, foundation_id):
    """One planning/retirement operation per foundation at a time, across controller processes."""
    require(isinstance(foundation_id, str) and len(foundation_id) == 32 and all(c in '0123456789abcdef' for c in foundation_id), 'Invalid foundation identity')
    return file_lock(Path(state_dir) / 'foundations' / (foundation_id + '.lock'), blocking=False)


def audit(state_dir, foundation_id, event):
    """Append-only, per-foundation controller audit log (private controller state, not Git)."""
    path = Path(state_dir) / 'foundations' / (foundation_id + '.audit.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(dict(event, time=time.time()), sort_keys=True) + '\n')


def prepare_infrastructure(project_dir, state_dir, source, paths, value, snapshot, *, factory=get_provider):
    """Plan infrastructure only. No Core bundle, model or inference artifact is read or required.

    The foundation lock makes the duplicate check and run creation atomic: two setup
    windows cannot both create an executable plan for the same foundation.
    """
    with foundation_lock(state_dir, value['id']):
        result = _prepare_infrastructure(project_dir, state_dir, source, paths, value, snapshot, factory=factory)
        audit(state_dir, value['id'], {'event': 'plan-prepared', 'run_id': result['plan']['run_id'], 'digest': result['plan']['digest']})
        return result


def _prepare_infrastructure(project_dir, state_dir, source, paths, value, snapshot, *, factory=get_provider):
    config = copy.deepcopy(value['configuration'])
    paths = dict(paths or {})
    for forbidden in ('bundle', 'key'):
        require(forbidden not in paths, 'Infrastructure-only planning does not accept Core release files')
    duplicates = infrastructure.existing_runs(state_dir, value['id'])
    if duplicates:
        raise NomiarchError('This foundation already has a retained run (' + str(duplicates[0]['phase']) + ') at ' + duplicates[0]['run_directory']
                            + '. Reopen it to review and approve, retire it if it is unused and expired or invalid, or use Manage to inspect, recover or remove it. '
                            'Planning again would create duplicate resources.')
    if config['target'] == 'local':
        require(paths.get('image'), 'Select the admitted Ubuntu 24.04 image before planning')
        require(config['local']['image_sha256'] == scaffold.PINS[config['architecture']]['image'][1], 'Use the matching desktop recipe for this Ubuntu image')
        config['local']['image'] = str(Path(paths['image']).resolve())
    elif config['target'] == 'existing':
        require(paths.get('ssh_key') and paths.get('known_hosts'), 'Select the SSH private key and the out-of-band enrolled known_hosts file for the adopted host')
        config['existing']['ssh_key'], config['existing']['known_hosts'] = str(Path(paths['ssh_key']).resolve()), str(Path(paths['known_hosts']).resolve())
    else:
        require(bool(config['azure'].get('subnet_id')), 'Azure setup needs a private administration route and an existing subnet. The scaffold can also be exported for your cloud administrator.')
    validate(config)
    directory, run = controller.new_run(config, state_dir, 'infrastructure')
    try:
        artifacts = {}
        if config['target'] == 'local':
            frozen = freeze({'image': paths['image']}, {'image': config['local']['image_sha256']}, directory / 'admitted')
            config['local']['image'] = frozen['image']
            artifacts['image'] = {'path': frozen['image'], 'sha256': config['local']['image_sha256']}
        elif config['target'] == 'existing':
            config['existing']['ssh_key'] = admit_private_file(paths['ssh_key'], directory / 'ssh-client')
            config['existing']['known_hosts'] = admit_private_file(paths['known_hosts'], directory / 'known_hosts')
            require(any(line.split() and config['existing']['host'] in line.split()[0].split(',') for line in Path(config['existing']['known_hosts']).read_text().splitlines()
                        if line.strip() and not line.startswith('#') and not line.startswith('|')),
                    'known_hosts does not contain an enrolled key for ' + config['existing']['host'] + '. Hashed entries are not accepted; enroll the host key out of band.')
        else:
            from .ssh_keys import generate
            key = generate(directory / 'ssh-client')
            config['azure']['ssh_key'], config['azure']['ssh_public_key'] = str(key), str(key) + '.pub'
        files_preflight(config)
        run['foundation'] = {'id': value['id'], 'project_directory': str(Path(project_dir).resolve()),
                             'snapshot': snapshot, 'isolation': value['isolation'], 'scope': 'infrastructure'}
        run['application'] = {'status': 'not-installed', 'note': 'Infrastructure-only bootstrap; application installation is a separate later operation'}
        provider = factory(config, run, directory, Runner(log_dir=directory / 'logs'), source)
        provider.prepare()  # Read-only checks and a saved plan; no resource is created or changed.
        summary = infrastructure.summary(config, value, run['prefix'])
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
        elif config['target'] == 'existing':
            summary['preflight'] = run.get('preflight')
        record = {'schema': 1, 'scope': 'infrastructure', 'run_id': run['id'], 'configuration': config, 'snapshot': snapshot,
                  'project_directory': str(Path(project_dir).resolve()), 'inputs': inputs(directory), 'artifacts': artifacts,
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


def prepare(project_dir, state_dir, source, paths, *, factory=get_provider):
    value, snapshot = scaffold.load_supported(project_dir, source)
    require(value['repository']['mode'] != 'offline',
            'This configuration uses export-only review. Create a configuration with a supported repository integration before deployment.')
    if scaffold.is_infrastructure(value):
        return prepare_infrastructure(project_dir, state_dir, source, paths, value, snapshot, factory=factory)
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
    require(not (directory / 'retired.json').exists(), 'This plan was retired and can never be executed. Prepare and review a new plan.')
    run = read_json(directory / 'run.json')
    require(run['id'] == directory.name == record['run_id'], 'Plan/run identity mismatch')
    require(run['phase'] != 'retired', 'This plan was retired and can never be executed. Prepare and review a new plan.')
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
        if value['repository']['mode'] in scaffold.REVIEW_MODES:
            require(repository_check is not None, 'A current human-reviewed repository change is required')
            evidence['repository'] = repository_check(value, record['snapshot'])
        require(value['usage'] != 'organisation' or 'repository' in evidence, 'Organisation approval cannot be bypassed by a local confirmation')
        write_json(directory / 'approval.json', evidence)
        if 'repository' in evidence: run['foundation']['repository_commit'] = evidence['repository']['merge']
        provider = factory(run['config'], run, directory, Runner(deadline=time.time() + 7200, log_dir=directory / 'logs'), source)
        if record.get('scope') == 'infrastructure':
            return apply_infrastructure(directory, run, record, provider)
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


def apply_infrastructure(directory, run, record, provider):
    """Execute the approved infrastructure plan exactly once. Nothing is installed on the host."""
    require(run['scope'] == 'infrastructure' and run['foundation'].get('scope') == 'infrastructure', 'Run scope does not match the approved plan')
    run['phase'], run['creation_started'] = 'creating', True
    controller.save(directory, run)
    try:
        run['inventory'] = provider.provision()  # Never re-plan after approval.
        run['phase'] = 'provisioned'
        controller.save(directory, run)
        checks = infrastructure.verify(provider, run['config'], run['foundation']['isolation'])
        run['validation'] = {'status': 'passed', 'scope': 'infrastructure', 'checks': checks}
        run['application'] = {'status': 'not-installed', 'note': 'Infrastructure ready. Core, models, agents and containers are installed by a later reviewed operation.'}
        run['phase'] = 'infrastructure-ready'
    except BaseException as e:
        run['validation'] = {'status': 'failed', 'scope': 'infrastructure', 'error': str(e)}
        run['phase'] = 'failed'
        run['recovery'] = {'retained': ['run.json', 'plan.json', 'approval.json', 'logs/', 'infra/ (state, if any)'],
                           'instruction': 'Nothing was deleted. Inspect logs and inventory, then use Manage to retry verification or prepare a separately approved removal. '
                                          'Adopted hosts are never deleted. Planning a new run is refused while this run is retained.'}
    finally:
        controller.save(directory, run)
        write_json(directory / 'report.json', run)
    return {'run_directory': str(directory), 'validation': run['validation'], 'cleanup': run['cleanup'],
            'inventory': run['inventory'], 'application': run['application'], 'phase': run['phase']}


def verify_infrastructure(directory, source, *, factory=get_provider):
    """Re-run the infrastructure checks against the recorded host. Read-only; nothing is installed or changed."""
    directory = Path(directory).resolve()
    with file_lock(directory / 'controller.lock', blocking=False):
        run = read_json(directory / 'run.json')
        require(run.get('scope') == 'infrastructure' and run.get('inventory') and run['cleanup'].get('status') != 'deleted', 'This run has no retained infrastructure to verify')
        _, snapshot = scaffold.load_supported(run['foundation']['project_directory'], source)
        require(snapshot == run['foundation']['snapshot'], 'The reviewed configuration changed outside the review workflow')
        provider = factory(run['config'], run, directory, Runner(log_dir=directory / 'logs'), source)
        checks = infrastructure.verify(provider, run['config'], run['foundation']['isolation'], configure_network=False)
        result = {'validation': {'status': 'passed', 'scope': 'infrastructure', 'checks': checks}, 'application': run.get('application'),
                  'run_directory': str(directory), 'time': time.time()}
        write_json(directory / 'verification.json', result)
        return result


def unused(directory, run):
    """True only for a plan that provably never started: no approval, no creation, no inventory."""
    return (run.get('phase') == 'awaiting-approval' and run.get('creation_started') is False and not run.get('inventory')
            and run.get('cleanup', {}).get('status') == 'not-requested' and not (Path(directory) / 'approval.json').exists()
            and not (Path(directory) / 'started.json').exists() and not (Path(directory) / 'report.json').exists())


def saved_plan(state_dir, foundation_id, source):
    """Reopen the foundation's pending plan after setup was closed. Returns None if there is none.

    The full apply-time checks decide validity; the caller shows the plan again and still
    needs the human's explicit approval and the repository review check to apply it.
    """
    for entry in infrastructure.existing_runs(state_dir, foundation_id):
        if entry['phase'] != 'awaiting-approval':
            continue
        directory = Path(entry['run_directory'])
        run = read_json(directory / 'run.json')
        record = read_json(directory / 'plan.json')
        try:
            validate_plan(directory, source)
            status, reason = 'valid', None
        except NomiarchError as error:
            status, reason = ('expired' if time.time() >= record.get('expires', 0) else 'invalid'), str(error)
        return {'run_directory': str(directory), 'plan': record, 'status': status, 'reason': reason, 'unused': unused(directory, run)}
    return None


def retire_plan(directory, source, reason):
    """Permanently retire an unused plan that is expired or no longer valid.

    The plan and run records are kept; a retirement record explains why. The run and
    foundation locks prevent racing an apply or a concurrent replacement plan.
    """
    directory = Path(directory).resolve()
    require(isinstance(reason, str) and 3 <= len(reason.strip()) <= 500, 'Record why this plan is being retired')
    state_dir = directory.parent.parent
    run = read_json(directory / 'run.json')
    foundation = (run.get('foundation') or {}).get('id')
    require(foundation and run.get('scope') == 'infrastructure', 'Only infrastructure-only plans use this retirement path')
    with foundation_lock(state_dir, foundation), file_lock(directory / 'controller.lock', blocking=False):
        run = read_json(directory / 'run.json')  # refresh under both locks
        require(not (directory / 'retired.json').exists(), 'This plan is already retired')
        require(unused(directory, run), 'Only an unused plan can be retired. Provisioned, partially provisioned or uncertain runs '
                                        'are kept for recovery; use Manage to inspect them or prepare a reviewed removal.')
        record = read_json(directory / 'plan.json')
        try:
            validate_plan(directory, source)
            valid = True
        except NomiarchError as error:
            valid, invalid_reason = False, str(error)
        require(not valid, 'This plan is still valid. Reopen it to review and approve it, or let it expire before replacing it.')
        evidence = {'schema': 1, 'run_id': run['id'], 'foundation': foundation, 'plan_digest': record.get('digest'),
                    'expired': time.time() >= record.get('expires', 0), 'invalid_because': invalid_reason,
                    'reason': reason.strip(), 'time': time.time(), 'controller': socket.gethostname(),
                    'effect': 'This plan can never be executed. Plan and run records are retained.'}
        write_json(directory / 'retired.json', evidence)
        run['phase'] = 'retired'
        run['retired'] = {'time': evidence['time'], 'reason': evidence['reason']}
        controller.save(directory, run)
        audit(state_dir, foundation, {'event': 'plan-retired', 'run_id': run['id'], 'digest': record.get('digest'), 'reason': evidence['reason'],
                                      'invalid_because': invalid_reason})
        return evidence
