"""Human-approved reconciliation of an existing foundation, with one-use plans."""
import copy
import hmac
import json
from pathlib import Path
import shutil
import time
import uuid

from nomiarch.bootstrap import controller
from nomiarch.bootstrap.config import require
from nomiarch.bootstrap.providers import get_provider
from nomiarch.common import NomiarchError, Runner, file_lock, private_dir, read_json, write_json
from . import deployment, network, observe, scaffold


def prepare(directory, project_directory, source, *, factory=get_provider):
    directory = Path(directory).resolve()
    with file_lock(directory / 'controller.lock', blocking=False):
        run = read_json(directory / 'run.json')
        require(run.get('foundation') and run['phase'] == 'ready', 'Select a ready customer foundation')
        previous, old_snapshot = scaffold.load_supported(run['foundation']['project_directory'], source)
        candidate, snapshot = scaffold.load_supported(project_directory, source)
        require(old_snapshot == run['foundation']['snapshot'], 'The original configuration changed outside the approved workflow')
        require(candidate['id'] == previous['id'], 'The proposal belongs to another foundation')
        # This first change recipe restores drift. Capacity changes and recipe
        # upgrades require their own reviewed recipes rather than hidden edits.
        a, b = copy.deepcopy(previous), copy.deepcopy(candidate)
        a.pop('reconciliation', None); b.pop('reconciliation', None)
        a.pop('maintenance', None); b.pop('maintenance', None)
        require(a == b, 'This reconciliation recipe restores the existing approved configuration only')
        require(candidate.get('reconciliation'), 'Select the configuration proposed by Core')
        require(candidate['reconciliation']['configuration_sha256'] == old_snapshot['sha256'], 'Reconciliation refers to a stale baseline')
        change_dir = private_dir(directory / 'changes' / uuid.uuid4().hex)
        provider = factory(run['config'], run, directory, Runner(), source)
        provider.verify()
        target = run['config']['target']
        current = {'local': observe.local, 'azure': observe.azure}[target](provider, previous)
        desired = dict(previous['configuration']['capacity'], outbound_blocked=True)
        require(current['disk_gib'] <= desired['disk_gib'] or 'disk_gib' not in candidate['reconciliation']['checks'], 'Disk shrinking is prohibited')
        if target == 'azure':
            shutil.copytree(directory / 'infra', change_dir / 'infra', ignore=shutil.ignore_patterns('.terraform', '*.tfplan', '*.lock.info'))
            shutil.copytree(directory / 'modules', change_dir / 'modules')
            provider.command(['tofu', 'init', '-input=false', '-lockfile=readonly'], cwd=change_dir / 'infra', timeout=600)
            provider.command(['tofu', 'plan', '-input=false', '-out=apply.tfplan'], cwd=change_dir / 'infra', timeout=600)
            tf = json.loads(provider.command(['tofu', 'show', '-json', 'apply.tfplan'], cwd=change_dir / 'infra'))
            actions = [{'address': r['address'], 'actions': r['change']['actions']} for r in tf.get('resource_changes', [])]
            require(all(r['actions'] in (['update'], ['no-op'], ['read']) for r in actions), 'Reconciliation would create, replace or delete resources; a separate administrator-reviewed recovery is required')
        else:
            actions = [{'setting': k, 'from': current[k], 'to': desired[k]} for k in desired if current[k] != desired[k] and (k != 'disk_gib' or current[k] < desired[k])]
        value = {'schema': 1, 'run_id': run['id'], 'change_id': change_dir.name, 'configuration': run['config'],
                 'baseline': old_snapshot, 'snapshot': snapshot, 'project_directory': str(Path(project_directory).resolve()),
                 'observed': current, 'desired': desired, 'actions': actions, 'inputs': deployment.inputs(change_dir),
                 'state_sha256': deployment.fingerprint(read_json(directory / 'infra/terraform.tfstate')) if target == 'azure' else None,
                 'created': time.time(), 'expires': time.time() + 3600,
                 'downtime': target == 'local' and any(current[k] != desired[k] and (k != 'disk_gib' or current[k] < desired[k]) for k in ('cpus', 'memory_gib', 'disk_gib'))}
        value['digest'] = deployment.fingerprint(value)
        value['seal'] = deployment.sign(directory.parent.parent, value)
        write_json(change_dir / 'plan.json', value)
        return {'change_directory': str(change_dir), 'plan': value}


def apply(change_directory, source, expected_digest, *, repository_check=None, factory=get_provider):
    change_dir = Path(change_directory).resolve()
    directory = change_dir.parent.parent
    with file_lock(directory / 'controller.lock', blocking=False):
        record = read_json(change_dir / 'plan.json')
        signed = dict(record); seal = signed.pop('seal')
        require(hmac.compare_digest(seal, deployment.sign(directory.parent.parent, signed)), 'Change plan integrity check failed')
        require(expected_digest == record['digest'], 'Displayed change plan does not match')
        require(time.time() < record['expires'] and not (change_dir / 'started.json').exists(), 'Change plan expired or was already used')
        run = read_json(directory / 'run.json')
        require(run['id'] == record['run_id'] == directory.name and run['config'] == record['configuration'] and run['phase'] == 'ready', 'Installation changed after planning')
        _, baseline = scaffold.load_supported(run['foundation']['project_directory'], source)
        require(baseline == run['foundation']['snapshot'] == record['baseline'], 'Approved baseline changed')
        value, snapshot = scaffold.load_supported(record['project_directory'], source)
        require(snapshot == record['snapshot'] and deployment.inputs(change_dir) == record['inputs'], 'Proposed configuration or plan changed')
        proof = {'digest': expected_digest, 'time': time.time(), 'mode': 'local-human'}
        if value['repository']['mode'] == 'github':
            require(repository_check is not None, 'Current human repository approval is required')
            proof['repository'] = repository_check(value, snapshot)
        require(value['usage'] != 'organisation' or 'repository' in proof, 'Organisation changes need independent human review')
        provider = factory(run['config'], run, directory, Runner(), source)
        target = run['config']['target']
        current = {'local': observe.local, 'azure': observe.azure}[target](provider, value)
        require(current == record['observed'], 'Environment changed since planning. Prepare a new plan before applying.')
        if target == 'azure':
            require(deployment.fingerprint(read_json(directory / 'infra/terraform.tfstate')) == record['state_sha256'], 'Terraform state changed after planning')
        write_json(change_dir / 'approval.json', proof)
        write_json(change_dir / 'started.json', {'time': time.time()})
        run['phase'] = 'reconciling'; controller.save(directory, run)
        try:
            if target == 'local':
                if record['downtime']:
                    provider.command(['multipass', 'stop', run['prefix']], timeout=180)
                    for key, setting in [('cpus', 'cpus'), ('memory_gib', 'memory'), ('disk_gib', 'disk')]:
                        desired = record['desired'][key]
                        if current[key] != desired and (key != 'disk_gib' or current[key] < desired):
                            provider.command(['multipass', 'set', 'local.' + run['prefix'] + '.' + setting + '=' + str(desired) + ('' if key == 'cpus' else 'G')])
                    provider.command(['multipass', 'start', run['prefix']], timeout=300)
                network.configure(provider)
            else:
                try:
                    provider.command(['tofu', 'apply', '-input=false', 'apply.tfplan'], cwd=change_dir / 'infra', timeout=1800)
                finally:
                    # Persist even partial state; a failed apply must be recoverable.
                    state = change_dir / 'infra/terraform.tfstate'
                    if state.exists():
                        from nomiarch.common import atomic_write
                        atomic_write(directory / 'infra/terraform.tfstate', state.read_bytes())
            actual = {'local': observe.local, 'azure': observe.azure}[target](provider, value)
            require(all(actual[k] == v if k != 'disk_gib' else actual[k] >= v for k, v in record['desired'].items()), 'Post-change observation still differs from the approved configuration')
            run['foundation']['project_directory'] = record['project_directory']
            run['foundation']['snapshot'] = snapshot
            if 'repository' in proof: run['foundation']['repository_commit'] = proof['repository']['merge']
            run['phase'] = 'ready'
            result = {'status': 'passed', 'observed': actual, 'change_directory': str(change_dir)}
            write_json(change_dir / 'result.json', result)
            return result
        except BaseException as e:
            run['phase'] = 'reconciliation-failed'
            write_json(change_dir / 'result.json', {'status': 'failed', 'error': str(e), 'recovery': 'Private state and the exact change plan are retained'})
            raise
        finally:
            controller.save(directory, run)
