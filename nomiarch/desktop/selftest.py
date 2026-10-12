"""Packaged-executable acceptance: generate real infrastructure scaffolds and validate them.

Runs inside the packaged desktop app (`--self-test`) on every supported platform, so
a missing bundled resource fails the build rather than the first customer setup.
No VM, host, cloud resource, repository or network connection is used.
"""
import copy
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile

from nomiarch.common import NomiarchError
from nomiarch.foundation import scaffold
from nomiarch.desktop.catalog import PINS

AZURE = {'subscription_id': '00000000-0000-0000-0000-000000000000', 'location': 'canadacentral', 'vm_size': 'Standard_D2s_v5',
         'ssh_user': 'nomiarch', 'ssh_key': '@controller', 'ssh_public_key': '@controller', 'admin_cidr': '10.20.30.0/24',
         'subnet_id': '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/v/subnets/s',
         'image': {'publisher': 'Canonical', 'offer': 'ubuntu-24_04-lts', 'sku': 'server', 'version': '24.04.202609010'}}
GITHUB = {'mode': 'github', 'owner': 'customer', 'name': 'infrastructure', 'approvers': ['reviewer']}
INTERNAL = {'mode': 'github-enterprise', 'server_url': 'https://git.company.internal', 'owner': 'customer', 'name': 'infrastructure', 'approvers': ['reviewer']}


def cases():
    base = {'api_version': 'nomiarch.io/v1alpha1', 'capacity': {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40}}
    examples = [
        ('local', dict(base, name='self-test-local', target='local', architecture='amd64',
                       local={'image': '/kit/ubuntu.img', 'image_sha256': PINS['amd64']['image'][1]}), GITHUB, 'restricted-connected'),
        ('existing', dict(base, name='self-test-host', target='existing', architecture='arm64',
                          existing={'host': 'host.site.internal', 'ssh_user': 'nomiarch', 'ssh_key': '/private/key', 'known_hosts': '/private/known_hosts'}), INTERNAL, 'fully-disconnected'),
        ('azure', dict(base, name='self-test-cloud', target='azure', architecture='amd64', azure=AZURE), GITHUB, 'restricted-connected'),
    ]

    result = list(examples)
    seen = {(c['target'], c['architecture'], repo['mode'], mode) for _, c, repo, mode in result}
    for _, template, _, _ in examples:
        for arch in (('amd64',) if template['target'] == 'azure' else ('amd64', 'arm64')):
            for repository in (GITHUB, INTERNAL):
                for mode in ('restricted-connected', 'fully-disconnected'):
                    if mode == 'fully-disconnected' and (template['target'] == 'azure' or repository['mode'] == 'github'):
                        continue
                    key = (template['target'], arch, repository['mode'], mode)
                    if key in seen: continue
                    seen.add(key)
                    config = copy.deepcopy(template)
                    config['architecture'] = arch
                    if config['target'] == 'local': config['local']['image_sha256'] = PINS[arch]['image'][1]
                    result.append(('-'.join(key), config, repository, mode))
    return result


def run_validator(folder):
    """Execute the vendored validator in-process exactly as the customer workflow would."""
    script = Path(folder) / 'validation' / 'validate.py'
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        module = runpy.run_path(str(script), run_name='nomiarch_validate')
        code = module['main']()
    report = json.loads(output.getvalue())
    return code, report


def scaffold_acceptance(source, folder):
    """Generate and validate every supported infrastructure combination from packaged resources."""
    results = {'validator_source_bytes': len(scaffold.validator_source())}
    for label, config, repository, mode in cases():
        value = scaffold.infrastructure_project(config, 'selftest', 'acceptance', 'organisation', mode, repository)
        destination = Path(folder) / ('scaffold-' + label)
        scaffold.create(destination, value, source)
        loaded, snapshot = scaffold.load_supported(destination, source)
        if loaded != value:
            raise NomiarchError('Packaged scaffold round-trip differs for ' + label)
        code, report = run_validator(destination)
        if code != 0 or report.get('status') != 'passed':
            raise NomiarchError('Packaged validator rejected its own scaffold for ' + label + ': ' + json.dumps(report))
        if 'runtime' in loaded or 'runtime.json' in snapshot['files']:
            raise NomiarchError('Infrastructure scaffold must not carry runtime pins: ' + label)
        results[label] = {'files': len(snapshot['files']), 'validator': report['status'], 'terraform': report.get('terraform'),
                          'scope': loaded['scope'], 'isolation': loaded['isolation']}
    legacy = scaffold.project(dict(cases()[0][1], name='self-test-core'), 'selftest', 'legacy')
    scaffold.create(Path(folder) / 'scaffold-core', legacy, source)
    results['core_recipe'] = scaffold.load_supported(Path(folder) / 'scaffold-core', source)[0]['recipe_version']
    return results


def main(source):
    with tempfile.TemporaryDirectory(prefix='nomiarch-selftest-') as folder:
        return scaffold_acceptance(source, folder)
