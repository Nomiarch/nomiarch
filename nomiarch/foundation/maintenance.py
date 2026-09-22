"""Operation-specific human approval for release maintenance and scoped removal."""
import copy
import hmac
from pathlib import Path
import re
import time
import uuid

from nomiarch.bootstrap import bundle, controller
from nomiarch.bootstrap.config import require
from nomiarch.bootstrap.providers import get_provider
from nomiarch.common import NomiarchError, Runner, digest, file_lock, private_dir, read_json, write_json
from . import deployment, network, scaffold


def version(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?', value)
    require(match is not None, 'Unsupported release version')
    return (*map(int, match.group(1, 2, 3)), 0 if match[4] is not None else 1, int(match[4] or 0))


def admit(value, archive, key):
    runtime = value['runtime']
    require(digest(archive) == runtime['bundle_sha256'] and digest(key) == runtime['release_key_sha256'], 'Selected release files differ from the reviewed runtime pins')
    manifest = bundle.inspect(archive, key)
    require(manifest['architecture'] == runtime['architecture'] and manifest['version'] == runtime['core_version'], 'Release metadata differs from the reviewed configuration')
    return manifest


def propose(directory, source, action, archive=None, key=None):
    require(action in {'upgrade', 'repair', 'destroy'}, 'Unsupported operation')
    run = read_json(Path(directory) / 'run.json')
    value, snapshot = scaffold.load_supported(run['foundation']['project_directory'], source)
    require(snapshot == run['foundation']['snapshot'], 'Installed configuration changed outside the review workflow')
    value.pop('reconciliation', None)
    if action == 'upgrade':
        manifest = bundle.inspect(archive, key)
        require(manifest['architecture'] == value['configuration']['architecture'], 'Release architecture differs from this installation')
        require(version(manifest['version']) >= version(value['runtime']['core_version']), 'Release downgrades are not supported by this recipe')
        value['runtime'] = {'core_version': manifest['version'], 'architecture': manifest['architecture'],
                            'bundle_sha256': digest(archive), 'release_key_sha256': digest(key)}
        require(value['runtime'] == scaffold.admitted_runtime(manifest['architecture']), 'Download the desktop version that admits this Core release before upgrading')
    elif action == 'repair': admit(value, archive, key)
    value['maintenance'] = {'action': action, 'id': uuid.uuid4().hex, 'configuration_sha256': snapshot['sha256']}
    return scaffold.validate_project(value)


def compatible(previous, candidate, snapshot):
    action = candidate.get('maintenance', {}).get('action')
    require(action in {'upgrade', 'repair', 'destroy'}, 'Select an operation-specific configuration request')
    require(candidate['maintenance']['configuration_sha256'] == snapshot['sha256'], 'Operation request refers to a stale configuration')
    a, b = copy.deepcopy(previous), copy.deepcopy(candidate)
    for item in (a, b):
        item.pop('maintenance', None); item.pop('reconciliation', None)
    if action == 'upgrade':
        require(version(b['runtime']['core_version']) >= version(a['runtime']['core_version']), 'Release downgrades are prohibited')
        a.pop('runtime'); b.pop('runtime')
    require(a == b, 'An operation request cannot also change infrastructure, repository ownership or approval policy')
    return action


def prepare(directory, project_directory, source, archive=None, key=None, recipient=None, *, factory=get_provider):
    directory = Path(directory).resolve()
    with file_lock(directory / 'controller.lock', blocking=False):
        run = read_json(directory / 'run.json')
        require(run.get('foundation') and run.get('creation_started') and run['cleanup'].get('status') != 'deleted', 'This foundation has no retained installation to maintain')
        previous, baseline = scaffold.load_supported(run['foundation']['project_directory'], source)
        require(baseline == run['foundation']['snapshot'], 'Installed configuration changed outside the review workflow')
        candidate, snapshot = scaffold.load_supported(project_directory, source)
        action = compatible(previous, candidate, baseline)
        if action == 'upgrade': require(candidate['runtime'] == scaffold.admitted_runtime(candidate['configuration']['architecture']), 'Upgrade release is not admitted by this desktop catalog')
        artifacts = {}
        change_dir = private_dir(directory / 'operations' / uuid.uuid4().hex)
        if action in {'upgrade', 'repair'}:
            require(run.get('inventory'), 'The provisioned VM was not recorded; inspect partial resources before maintenance')
            admit(candidate, archive, key)
            paths = deployment.freeze({'bundle': archive, 'key': key}, {'bundle': candidate['runtime']['bundle_sha256'], 'key': candidate['runtime']['release_key_sha256']}, change_dir / 'admitted')
            artifacts = {role: {'path': path, 'sha256': digest(path)} for role, path in paths.items()}
        if action == 'upgrade': require(isinstance(recipient, str) and re.fullmatch(r'age1[0-9a-z]{58}', recipient), 'Save a new private recovery key before planning the upgrade')
        provider = factory(run['config'], run, directory, Runner(), source)
        if action == 'destroy':
            if run['config']['target'] == 'azure' and provider.group_exists():
                group = run['prefix'] + '-rg'
                provider.assert_cleanup_scope(provider.az(['group', 'show', '--name', group]), provider.az(['resource', 'list', '--resource-group', group]))
        else: provider.verify()
        summary = {'action': action, 'system': run['config']['name'], 'target': run['config']['target'], 'vm': run['prefix'],
                   'from_release': previous['runtime']['core_version'], 'to_release': candidate['runtime']['core_version'],
                   'downtime': action in {'upgrade', 'repair'}, 'deletes_vm_and_disks': action == 'destroy',
                   'encrypted_backup_before_change': action == 'upgrade', 'recipe': candidate['recipe_version']}
        record = {'schema': 1, 'run_id': run['id'], 'prefix': run['prefix'], 'operation_id': change_dir.name,
                  'configuration': run['config'], 'inventory': run.get('inventory'), 'baseline': baseline, 'snapshot': snapshot,
                  'project_directory': str(Path(project_directory).resolve()), 'artifacts': artifacts, 'recipient': recipient,
                  'summary': summary, 'created': time.time(), 'expires': time.time() + 3600}
        record['digest'] = deployment.fingerprint(record)
        record['seal'] = deployment.sign(directory.parent.parent, record)
        write_json(change_dir / 'plan.json', record)
        return {'operation_directory': str(change_dir), 'plan': record}


def apply(operation_directory, source, expected_digest, *, repository_check=None, factory=get_provider):
    operation_dir = Path(operation_directory).resolve()
    directory = operation_dir.parent.parent
    with file_lock(directory / 'controller.lock', blocking=False):
        record = read_json(operation_dir / 'plan.json')
        sealed = dict(record); signature = sealed.pop('seal')
        require(hmac.compare_digest(signature, deployment.sign(directory.parent.parent, sealed)), 'Operation plan integrity failed')
        require(record['digest'] == expected_digest and record['operation_id'] == operation_dir.name, 'The displayed operation plan differs')
        require(time.time() < record['expires'] and not (operation_dir / 'started.json').exists(), 'Operation plan expired or was already used')
        run = read_json(directory / 'run.json')
        require(run['id'] == directory.name == record['run_id'] and run['prefix'] == record['prefix'] == 'nomiarch-' + run['id'][:16], 'Installation identity changed')
        require(run['config'] == record['configuration'] and run.get('inventory') == record['inventory'] and run['cleanup'].get('status') != 'deleted', 'Installation changed since planning')
        previous, baseline = scaffold.load_supported(run['foundation']['project_directory'], source)
        require(baseline == run['foundation']['snapshot'] == record['baseline'], 'Installed configuration changed since planning')
        candidate, snapshot = scaffold.load_supported(record['project_directory'], source)
        require(snapshot == record['snapshot'], 'Operation request changed since planning')
        action = compatible(previous, candidate, baseline)
        require(action == record['summary']['action'], 'Operation action changed')
        if action in {'upgrade', 'repair'}:
            for artifact in record['artifacts'].values(): require(digest(artifact['path']) == artifact['sha256'], 'A release file changed after planning')
            admit(candidate, record['artifacts']['bundle']['path'], record['artifacts']['key']['path'])
        evidence = {'mode': 'local-human', 'time': time.time(), 'digest': expected_digest}
        if candidate['repository']['mode'] in scaffold.REVIEW_MODES:
            require(repository_check is not None, 'A current human-reviewed operation PR is required')
            evidence['repository'] = repository_check(candidate, snapshot)
        require(candidate['usage'] != 'organisation' or 'repository' in evidence, 'Organisation maintenance needs its own independent human approval')
        write_json(operation_dir / 'approval.json', evidence)
        write_json(operation_dir / 'started.json', {'time': time.time()})
        provider = factory(run['config'], run, directory, Runner(log_dir=operation_dir / 'logs'), source)
        run['phase'] = action + '-running'; controller.save(directory, run)
        try:
            if action == 'destroy':
                controller.cleanup(directory, run, source, factory=factory)
                require(run['cleanup'].get('status') == 'deleted', 'Removal did not complete: ' + run['cleanup'].get('error', 'inspect the run record'))
                result = {'status': 'passed', 'cleanup': run['cleanup']}
            else:
                if action == 'upgrade':
                    run['pre_upgrade_backup'] = controller.backup_remote(provider, directory, run, record['recipient'])
                    controller.save(directory, run)
                result = controller.install_remote(provider, directory, run, record['artifacts']['bundle']['path'], record['artifacts']['key']['path'], action, record['recipient'])
                require(not run.get('backup_export_error'), 'The guest backup was not exported; inspect the retained recovery files')
                if run['config']['target'] == 'local': network.configure(provider)
                run['foundation']['project_directory'] = record['project_directory']
                run['foundation']['snapshot'] = snapshot
                if 'repository' in evidence: run['foundation']['repository_commit'] = evidence['repository']['merge']
                run['validation'] = {'status': 'passed', 'scope': 'core', 'checks': result}
                run['phase'] = 'ready'
                result = {'status': 'passed', 'result': result}
            write_json(operation_dir / 'result.json', result)
            return result
        except BaseException as e:
            run['phase'] = action + '-failed'
            write_json(operation_dir / 'result.json', {'status': 'failed', 'error': str(e)})
            raise
        finally:
            controller.save(directory, run)
