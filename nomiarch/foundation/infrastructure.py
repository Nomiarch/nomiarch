"""Infrastructure-only verification and run bookkeeping. No application is installed here."""
import json
from pathlib import Path

from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError, read_json
from . import network

FACTS = ("import json,os,platform,shutil,subprocess,sys\n"
         "mem=[int(l.split()[1]) for l in open('/proc/meminfo') if l.startswith('MemTotal')][0]\n"
         "st=shutil.disk_usage('/')\n"
         "rel=dict(l.strip().split('=',1) for l in open('/etc/os-release') if '=' in l)\n"
         "devices=[]\n"
         "try:\n"
         "    out=subprocess.run(['lsblk','-J','-b','-o','NAME,KNAME,SIZE,TYPE,HCTL,MOUNTPOINT'],capture_output=True,text=True,timeout=30)\n"
         "    devices=[d for d in json.loads(out.stdout).get('blockdevices',[]) if d.get('type')=='disk']\n"
         "except Exception as e: devices=[{'error':str(e)}]\n"
         "expected=sys.argv[1] if len(sys.argv)>1 else ''\n"
         "resolved=os.path.realpath(expected) if expected and os.path.exists(expected) else None\n"
         "print(json.dumps({'cpus':os.cpu_count(),'memory_kib':mem,'root_bytes':st.total,'machine':platform.machine(),"
         "'os_id':rel.get('ID','').strip('\"'),'os_version':rel.get('VERSION_ID','').strip('\"'),'hostname':platform.node(),"
         "'disks':devices,'expected_device':expected,'expected_device_resolved':resolved}))")
PROBE = ("import json,socket\n"
         "out={}\n"
         "for name,(family,host) in {'ipv4':(socket.AF_INET,'1.1.1.1'),'ipv6':(socket.AF_INET6,'2606:4700:4700::1111')}.items():\n"
         "    try:\n"
         "        s=socket.socket(family,socket.SOCK_STREAM); s.settimeout(3)\n"
         "        out[name]='reachable' if s.connect_ex((host,443))==0 else 'blocked'\n"
         "        s.close()\n"
         "    except OSError: out[name]='blocked'\n"
         "try:\n"
         "    socket.getaddrinfo('github.com',443); out['public_dns']='resolves'\n"
         "except OSError: out['public_dns']='unresolvable'\n"
         "print(json.dumps(out))")
MACHINES = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}


def observe(provider, expected_device=None):
    """Read-only facts: CPU, memory, OS, root filesystem and attached block devices.

    Nothing is formatted, mounted or written. `expected_device` is the data disk path
    recorded in the Azure inventory; it is resolved to its kernel device for identity.
    """
    args = ['python3', '-c', FACTS] + ([expected_device] if expected_device else [])
    raw = provider.remote(args, timeout=60)
    try:
        facts = json.loads(raw)
    except ValueError:
        raise NomiarchError('The host did not return readable system facts') from None
    require(isinstance(facts, dict) and all(k in facts for k in ('cpus', 'memory_kib', 'machine', 'os_id', 'os_version')),
            'Incomplete system facts from the host')
    root_bytes = facts.get('root_bytes', facts.get('disk_bytes', 0))
    disks = []
    for item in facts.get('disks') or []:
        if not isinstance(item, dict) or 'error' in item: continue
        disks.append({'name': str(item.get('kname') or item.get('name') or ''), 'size_gib': round(int(item.get('size') or 0) / 1024 ** 3, 2),
                      'hctl': str(item.get('hctl') or ''), 'mountpoint': item.get('mountpoint')})
    return {'cpus': int(facts['cpus']), 'memory_gib': round(int(facts['memory_kib']) / 1024 ** 2, 2),
            'root_filesystem_gib': round(int(root_bytes) / 1024 ** 3, 2), 'architecture': MACHINES.get(str(facts['machine']), str(facts['machine'])),
            'os': str(facts['os_id']) + ' ' + str(facts['os_version']), 'hostname': str(facts.get('hostname', '')),
            'disks': disks, 'expected_device': facts.get('expected_device') or None,
            'expected_device_resolved': facts.get('expected_device_resolved')}


def verify_azure_data_disk(provider, config, observed):
    """The declared data disk must be the recorded managed disk, attached at LUN 0 and large enough.

    Capacity is read from the block device and from the Azure control plane; the disk is
    never formatted or mounted here. Application installation decides its use later.
    """
    inventory = provider.run.get('inventory') or {}
    device, disk_id, vm_id = inventory.get('data_device'), inventory.get('data_disk_id'), inventory.get('vm_id')
    require(device and disk_id and vm_id, 'The Azure inventory does not record the data disk; refusing to verify an unknown disk')
    declared = config['capacity']['disk_gib']
    resolved = observed.get('expected_device_resolved')
    require(resolved, 'The recorded data disk ' + str(device) + ' is not attached to the VM')
    kernel = Path(resolved).name
    attached = next((d for d in observed['disks'] if d['name'] == kernel), None)
    require(attached is not None, 'The recorded data disk resolves to ' + resolved + ', which is not a whole disk on the VM')
    lun = attached['hctl'].split(':')[-1] if attached['hctl'] else ''
    require(lun == device.rsplit('lun', 1)[-1], 'The data disk is attached at an unexpected LUN; refusing to trust a different disk')
    require(attached['size_gib'] >= declared * 0.99, f"The attached data disk is {attached['size_gib']} GiB; the approved configuration requires {declared} GiB")
    result = {'device': device, 'kernel_device': resolved, 'lun': lun, 'attached_size_gib': attached['size_gib'], 'declared_gib': declared,
              'mounted': bool(attached['mountpoint']), 'inspection': 'read-only; not formatted or mounted by verification'}
    if hasattr(provider, 'az'):
        disk = provider.az(['disk', 'show', '--ids', disk_id])
        require(str(disk.get('id', '')).lower() == str(disk_id).lower(), 'Azure returned a different managed disk than the recorded one')
        require(str(disk.get('managedBy') or '').lower() == str(vm_id).lower(), 'The recorded data disk is not attached to the approved VM')
        require(int(disk.get('diskSizeGb') or 0) >= declared, f"Azure reports the data disk as {disk.get('diskSizeGb')} GiB; the approved configuration requires {declared} GiB")
        result['control_plane'] = {'managed_disk': disk_id, 'managed_by': vm_id, 'disk_size_gb': int(disk['diskSizeGb'])}
    return result


def probe_egress(provider):
    """Software-only egress probe. It never establishes a physical air gap."""
    raw = provider.remote(['python3', '-c', PROBE], timeout=60)
    try:
        result = json.loads(raw)
    except ValueError:
        raise NomiarchError('The host did not return a readable network probe') from None
    result['meaning'] = 'software observation from inside the host; site qualification is separate'
    return result


def verify(provider, config, isolation, configure_network=True):
    """Compare the provisioned or adopted host with the reviewed declaration."""
    expected_device = (provider.run.get('inventory') or {}).get('data_device') if config['target'] == 'azure' else None
    observed = observe(provider, expected_device)
    declared = config['capacity']
    checks = {'observed': observed, 'declared_capacity': declared}
    require(observed['architecture'] == config['architecture'], f"Host architecture {observed['architecture']} differs from the declared {config['architecture']}")
    require(observed['os'].startswith('ubuntu 24.04'), 'The host must run Ubuntu 24.04; observed ' + observed['os'])
    require(observed['cpus'] >= declared['cpus'], f"Host has {observed['cpus']} CPUs; the configuration requires {declared['cpus']}")
    require(observed['memory_gib'] >= declared['memory_gib'] * 0.85, f"Host memory {observed['memory_gib']} GiB is below the declared {declared['memory_gib']} GiB")
    if config['target'] == 'azure':
        # Azure: a fixed 32 GiB OS disk plus the declared data disk. The declared
        # capacity is the data disk, not the root filesystem.
        require(observed['root_filesystem_gib'] >= 32 * 0.85, f"The Azure OS disk root filesystem is {observed['root_filesystem_gib']} GiB; expected the 32 GiB OS disk")
        checks['storage'] = {'measured': 'root filesystem of the 32 GiB OS disk and the attached data disk at the recorded LUN',
                             'os_root_filesystem_gib': observed['root_filesystem_gib'], 'data_disk': verify_azure_data_disk(provider, config, observed)}
    else:
        # Local VM and adopted host: the declared storage is the root filesystem.
        require(observed['root_filesystem_gib'] >= declared['disk_gib'] * 0.85, f"Root storage {observed['root_filesystem_gib']} GiB is below the declared {declared['disk_gib']} GiB")
        checks['storage'] = {'measured': 'root filesystem', 'root_filesystem_gib': observed['root_filesystem_gib'], 'declared_gib': declared['disk_gib']}
    checks['administration'] = provider.verify()
    if config['target'] == 'local':
        # First establishment installs the persistent egress boundary; later verification only checks it.
        checks['network'] = network.configure(provider) if configure_network else network.verify(provider)
    elif isolation == 'disconnected':
        probe = probe_egress(provider)
        require(probe.get('ipv4') == 'blocked' and probe.get('ipv6') == 'blocked', 'The adopted host still reaches the public internet; it is not inside a disconnected boundary')
        checks['network'] = probe
    else:
        checks['network'] = {'mode': isolation, 'meaning': 'policy declared; adopted-host and cloud boundaries are verified by the site/platform, not by this controller'}
    checks['capacity'] = 'meets declared minimum'
    checks['physical_air_gap'] = 'not established by software checks'
    return checks


def summary(config, value, run_prefix):
    target = config['target']
    capacity = config['capacity']
    location = value['destination']['location']
    base = {'system': config['name'], 'destination': target, 'location': location, 'provider': value['destination']['provider'],
            'capacity': capacity, 'organisation': value['organisation'], 'environment': value['environment'],
            'isolation': value['isolation'], 'scope': 'infrastructure', 'application': 'not installed by this plan; later milestone',
            'executor': 'separate human-operated desktop controller (manual; no automated pipeline)'}
    if target == 'local':
        base.update(action='Create one retained Multipass VM named ' + run_prefix + ' (infrastructure only)',
                    network='Guest outbound denied by the egress boundary; host administration remains',
                    charges='Uses this computer’s resources; no cloud charges', downtime='None; the VM is new')
    elif target == 'existing':
        base.update(action='Adopt the dedicated host ' + config['existing']['host'] + ' over trusted SSH (no VM is created)',
                    network='Site boundary; a probe from inside the host is recorded but does not prove disconnection',
                    charges='No cloud charges; the host is customer-provided', downtime='None; the host is only verified and recorded',
                    deletable=False)
    else:
        base.update(action='Create the private Azure VM, disks, NSG and network attachment from the saved OpenTofu plan',
                    network='Private VM; Azure platform and control-plane dependencies remain',
                    charges='Azure resources incur charges until explicitly removed; no price is estimated', downtime='None; resources are new')
    return base


def existing_runs(state_dir, foundation_id):
    """Retained runs for the same foundation. Reopening setup must show these, not duplicate them."""
    found = []
    for path in sorted(Path(state_dir).glob('runs/*/run.json')):
        try:
            run = read_json(path)
        except NomiarchError:
            continue
        foundation = run.get('foundation') or {}
        if foundation.get('id') != foundation_id:
            continue
        if run.get('cleanup', {}).get('status') == 'deleted' or run.get('phase') in {'planning-failed', 'retired'}:
            continue
        if run.get('creation_started') or run.get('phase') == 'awaiting-approval':
            found.append({'run_directory': str(path.parent), 'phase': run.get('phase'), 'inventory': run.get('inventory'),
                          'validation': run.get('validation'), 'application': run.get('application'), 'created': run.get('created')})
    return found


def status(run_directory):
    """Operational summary for the setup UI: last recorded state, never a live health query."""
    directory = Path(run_directory)
    run = read_json(directory / 'run.json')
    record = read_json(directory / 'plan.json') if (directory / 'plan.json').exists() else {}
    approval = read_json(directory / 'approval.json') if (directory / 'approval.json').exists() else None
    logs = sorted(p.name for p in (directory / 'logs').glob('*.log')) if (directory / 'logs').is_dir() else []
    return {'run_id': run['id'], 'phase': run['phase'], 'scope': run.get('scope'), 'destination': run['config']['target'],
            'inventory': run.get('inventory'), 'validation': run.get('validation'), 'application': run.get('application'),
            'approval': approval, 'repository': (run.get('foundation') or {}).get('repository_commit'),
            'plan_summary': record.get('summary'), 'retained_logs': len(logs), 'run_directory': str(directory),
            'recovery': run.get('recovery'), 'cleanup': run.get('cleanup'), 'observation': 'last recorded controller state'}
