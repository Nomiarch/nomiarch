"""Controller observation bridge. Core sees measurements, never cloud/Git tokens."""
import copy
import json
from pathlib import Path
import re
import time

from nomiarch.bootstrap.providers import get_provider
from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError, Runner, read_json, write_json
from . import network, scaffold


def quantity(value):
    value = str(value).strip()
    if re.fullmatch(r'[0-9]+', value): return int(value) / 1024**3
    match = re.fullmatch(r'([0-9.]+)\s*(Gi?B?|Mi?B?|Ki?B?)', value)
    require(match is not None, 'Unknown VM memory/disk quantity')
    return float(match[1]) * {'G': 1, 'M': 1/1024, 'K': 1/1024**2}[match[2][0]]


def local(provider, value):
    name = provider.run['prefix']
    observed = {}
    for key, setting in [('cpus', 'cpus'), ('memory_gib', 'memory'), ('disk_gib', 'disk')]:
        raw = provider.command(['multipass', 'get', 'local.' + name + '.' + setting]).strip()
        observed[key] = int(raw) if key == 'cpus' else quantity(raw)
    try:
        network.verify(provider)
        observed['outbound_blocked'] = True
    except NomiarchError:
        # Confirm the VM remains reachable; a failed observation is not evidence
        # of drift and must not manufacture an automatic remediation proposal.
        provider.remote(['true'], timeout=20)
        observed['outbound_blocked'] = False
    return observed


def azure(provider, value):
    config = value['configuration']
    vm = provider.az(['vm', 'show', '--ids', provider.run['inventory']['vm_id']])
    sizes = provider.az(['vm', 'list-sizes', '--location', config['azure']['location']])
    size = next((x for x in sizes if x['name'] == vm['hardwareProfile']['vmSize']), None)
    require(size is not None, 'Could not observe the current Azure VM size')
    disk = provider.az(['disk', 'show', '--ids', provider.run['inventory']['data_disk_id']])
    group = provider.run['prefix'] + '-rg'
    nsg = provider.az(['network', 'nsg', 'show', '--resource-group', group, '--name', provider.run['prefix'] + '-nsg'])
    nic = provider.az(['network', 'nic', 'show', '--resource-group', group, '--name', provider.run['prefix'] + '-nic'])
    expected = [
        {'name': 'azure-platform-agent', 'priority': 100, 'direction': 'Outbound', 'access': 'Allow', 'protocol': 'Tcp',
         'sourcePortRange': '*', 'destinationPortRanges': ['80', '32526'], 'sourceAddressPrefix': '*', 'destinationAddressPrefix': '168.63.129.16/32'},
        {'name': 'deny-other-outbound', 'priority': 200, 'direction': 'Outbound', 'access': 'Deny', 'protocol': '*',
         'sourcePortRange': '*', 'destinationPortRange': '*', 'sourceAddressPrefix': '*', 'destinationAddressPrefix': '*'}]
    actual = [x for x in nsg.get('securityRules', []) if x['direction'] == 'Outbound']
    # Exact required rule values plus no earlier exceptions and the actual NIC
    # association; merely finding an NSG named as expected is insufficient.
    blocked = len(actual) == 2 and all(any(all(rule.get(k) == v for k, v in e.items()) for rule in actual) for e in expected)
    blocked = blocked and (nic.get('networkSecurityGroup') or {}).get('id', '').lower() == nsg['id'].lower()
    return {'cpus': size['numberOfCores'], 'memory_gib': size['memoryInMB']/1024,
            'disk_gib': disk['diskSizeGb'], 'outbound_blocked': bool(blocked)}


CORE_REQUEST = '''import json,os,sys,time
from nomiarch.common import request
from nomiarch.core.foundation import evaluate
from nomiarch import __version__
config=json.load(sys.stdin)
config['observed']['core_version']=__version__
evaluate(config)
url='http://127.0.0.1:8787'; token=os.environ['NOMIARCH_OPERATOR_TOKEN']
task=request(url+'/v1/tasks',token=token,data={'config':config})['id']
until=time.monotonic()+160
while time.monotonic()<until:
    result=request(url+'/v1/tasks/'+task,token=token)
    if result['status'] in ('failed','completed'):
        result['observation']=config
        print(json.dumps(result)); break
    time.sleep(1)
else: raise RuntimeError('Core observation evaluation timed out')
'''


def inspect(directory, source, *, repository_head=None, factory=get_provider):
    directory = Path(directory)
    run = read_json(directory / 'run.json')
    require(run.get('foundation') and run.get('inventory') and run['phase'] == 'ready', 'Select a ready installation created from a customer scaffold')
    value, snapshot = scaffold.load_supported(run['foundation']['project_directory'], source)
    require(snapshot == run['foundation']['snapshot'], 'Customer configuration has changed since deployment; review the change before observing against a new baseline')
    provider = factory(run['config'], run, directory, Runner(), source)
    provider.verify()
    observed = {'local': local, 'azure': azure}[run['config']['target']](provider, value)
    desired = dict(value['configuration']['capacity'], outbound_blocked=True, core_version=value['runtime']['core_version'])
    observed['core_version'] = value['runtime']['core_version']  # Replaced with the actual version inside Core.
    checked_repository = False
    if value['repository']['mode'] == 'github' and repository_head is not None:
        require(run['foundation'].get('repository_commit'), 'No approved repository revision was recorded')
        desired['repository_head'] = run['foundation']['repository_commit']
        observed['repository_head'] = repository_head(value)
        checked_repository = True
    request = {'kind': 'foundation', 'name': value['id'], 'configuration_sha256': snapshot['sha256'], 'desired': desired, 'observed': observed}
    command = ['sudo', '-n', '/usr/local/bin/k3s', 'kubectl', '-n', 'nomiarch', 'exec', '-i', 'deployment/core', '--', 'python3', '-c', CORE_REQUEST]
    result = json.loads(provider.remote(command, input=json.dumps(request).encode(), timeout=180))
    require(result['status'] == 'completed' and result['result'].get('kind') == 'foundation', 'Core could not evaluate this foundation observation; use a release with the foundation evaluator')
    report = {'observed_at': time.time(), 'request': result.pop('observation'), 'core': result, 'repository_checked': checked_repository}
    write_json(directory / 'observation.json', report)
    return report


def proposed_project(directory, source, report):
    run = read_json(Path(directory) / 'run.json')
    value, snapshot = scaffold.load_supported(run['foundation']['project_directory'], source)
    require(report['request']['configuration_sha256'] == snapshot['sha256'], 'Observation belongs to another configuration')
    result = report['core']['result']
    require(result.get('kind') == 'foundation' and result.get('authority') == 'proposal-only', 'Unrecognised Core proposal')
    failed = [f['check'] for f in result['findings'] if f['status'] == 'fail']
    require(not set(failed) - {'cpus', 'memory_gib', 'disk_gib', 'outbound_blocked'}, 'Review the changed repository or release before proposing infrastructure repairs')
    require(failed, 'The observed settings match the approved configuration; there is no remediation to propose')
    proposal = copy.deepcopy(value)
    proposal.pop('maintenance', None)
    proposal['reconciliation'] = {'observation_sha256': result['observation_sha256'], 'configuration_sha256': snapshot['sha256'],
                                  'checks': failed}
    return scaffold.validate_project(proposal)
