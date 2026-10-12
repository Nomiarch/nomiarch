"""Infrastructure-only bootstrap milestone: choices, boundaries and lifecycle with fake providers."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from nomiarch.bootstrap import controller
from nomiarch.common import NomiarchError, digest, read_json, write_json
from nomiarch.desktop import service, setup_flow
from nomiarch.desktop.catalog import infrastructure_inventory
from nomiarch.foundation import backup, deployment, destinations, infrastructure, maintenance, scaffold
from nomiarch.foundation.repository import GitHub
import test_foundation as foundation_tests

ROOT = Path(__file__).resolve().parents[1]
GITHUB = {'mode': 'github', 'owner': 'customer', 'name': 'infrastructure', 'approvers': ['reviewer']}
INTERNAL = {'mode': 'github-enterprise', 'server_url': 'https://git.company.internal', 'owner': 'customer', 'name': 'infrastructure', 'approvers': ['reviewer']}
EXISTING = {'api_version': 'nomiarch.io/v1alpha1', 'name': 'site-host', 'target': 'existing', 'architecture': 'amd64',
            'capacity': {'cpus': 4, 'memory_gib': 16, 'disk_gib': 100},
            'existing': {'host': 'host.site.internal', 'ssh_user': 'nomiarch', 'ssh_key': '/private/key', 'known_hosts': '/private/known_hosts'}}
AZURE = {'api_version': 'nomiarch.io/v1alpha1', 'name': 'cloud-vm', 'target': 'azure', 'architecture': 'amd64',
         'capacity': {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40},
         'azure': {'subscription_id': '00000000-0000-0000-0000-000000000000', 'location': 'canadacentral', 'vm_size': 'Standard_D2s_v5',
                   'ssh_user': 'nomiarch', 'ssh_key': '@controller', 'ssh_public_key': '@controller', 'admin_cidr': '10.20.30.0/24',
                   'subnet_id': '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/v/subnets/s',
                   'image': {'publisher': 'Canonical', 'offer': 'ubuntu-24_04-lts', 'sku': 'server', 'version': '24.04.202609010'}}}
FACTS = {'cpus': 4, 'memory_kib': 16 * 1024 * 1024, 'disk_bytes': 100 * 1024 ** 3, 'machine': 'x86_64', 'os_id': 'ubuntu', 'os_version': '24.04', 'hostname': 'host'}


class FakeProvider:
    """Stands in for Multipass, an adopted SSH host and Azure. Records every remote command."""
    calls, facts, probe = [], dict(FACTS), {'ipv4': 'blocked', 'ipv6': 'blocked', 'public_dns': 'unresolvable'}
    failure = None
    commands = []
    # Azure shape: 32 GiB OS disk (~30 GiB root filesystem), temp disk, data disk at LUN 0.
    azure = {}

    @classmethod
    def reset_azure(cls, data_gib=40, resolved='/dev/sdc', hctl='1:0:0:0', managed_by=None, control_gib=None, disk_id=None):
        cls.azure = {'data_gib': data_gib, 'resolved': resolved, 'hctl': hctl, 'managed_by': managed_by,
                     'control_gib': data_gib if control_gib is None else control_gib, 'disk_id': disk_id}

    def __init__(self, config, run, directory, runner, source):
        self.config, self.run, self.directory = config, run, Path(directory)

    def prepare(self):
        FakeProvider.calls.append('prepare')
        if self.config['target'] == 'azure': (self.directory / 'apply.tfplan').write_bytes(b'saved plan')

    def vm_id(self):
        return '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/' + self.run['prefix'] + '-rg/providers/Microsoft.Compute/virtualMachines/' + self.run['prefix'] + '-vm'

    def provision(self):
        FakeProvider.calls.append('provision')
        if FakeProvider.failure: raise FakeProvider.failure
        if self.config['target'] == 'existing':
            return {'host': self.config['existing']['host'], 'created_by_nomiarch': False, 'deletable_by_nomiarch': False}
        if self.config['target'] == 'azure':
            group = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/' + self.run['prefix'] + '-rg'
            return {'host': '10.20.30.4', 'vm_id': self.vm_id(), 'resource_group': self.run['prefix'] + '-rg',
                    'data_disk_id': group + '/providers/Microsoft.Compute/disks/' + self.run['prefix'] + '-data',
                    'data_device': '/dev/disk/azure/scsi1/lun0', 'mode': 'azure-restricted'}
        return {'host': self.run['prefix']}

    def verify(self): return {'management': 'ok'}

    def remote(self, args, **kwargs):
        FakeProvider.calls.append('remote')
        FakeProvider.commands.append([str(a) for a in args])
        source = ' '.join(str(a) for a in args)
        if 'meminfo' in source:
            if self.config['target'] != 'azure': return json.dumps(FakeProvider.facts)
            a = FakeProvider.azure
            disks = [{'name': 'sda', 'kname': 'sda', 'size': 32 * 1024 ** 3, 'type': 'disk', 'hctl': '0:0:0:0', 'mountpoint': None},
                     {'name': 'sdb', 'kname': 'sdb', 'size': 16 * 1024 ** 3, 'type': 'disk', 'hctl': '0:0:0:1', 'mountpoint': None}]
            if a['resolved']:
                disks.append({'name': 'sdc', 'kname': 'sdc', 'size': a['data_gib'] * 1024 ** 3, 'type': 'disk', 'hctl': a['hctl'], 'mountpoint': None})
            facts = dict(FakeProvider.facts, root_bytes=int(29.4 * 1024 ** 3), disks=disks, expected_device=str(args[-1]), expected_device_resolved=a['resolved'])
            facts.pop('disk_bytes', None)
            return json.dumps(facts)
        if 'getaddrinfo' in source: return json.dumps(FakeProvider.probe)
        return ''

    def az(self, args, timeout=600):
        FakeProvider.calls.append('az:' + ' '.join(args[:2]))
        assert args[:2] == ['disk', 'show'], args
        a = FakeProvider.azure
        return {'id': a['disk_id'] or args[-1], 'managedBy': a['managed_by'] or self.vm_id(), 'diskSizeGb': a['control_gib']}

    def command(self, args, **kwargs):
        if args[:2] == ['tofu', 'show']: return json.dumps({'resource_changes': [{'address': 'module.foundation.azurerm_linux_virtual_machine.core', 'change': {'actions': ['create'], 'after': {'custom_data': 'secret-cloud-init'}, 'after_sensitive': {'custom_data': True}}}]})
        return ''

    def destroy(self): FakeProvider.calls.append('destroy'); return {'status': 'deleted'}


class ChoiceTests(unittest.TestCase):
    def test_every_destination_git_and_network_combination_is_decided_before_generation(self):
        decided = {}
        for location in destinations.LOCATIONS:
            for provider in destinations.LOCATIONS[location]['providers']:
                for git in destinations.GIT_SERVICES:
                    for mode in destinations.NETWORK_MODES:
                        problems = destinations.combination_problems(location, provider, git, mode)
                        decided[(location, git, mode)] = not problems
        self.assertTrue(decided[('local', 'github', 'restricted-connected')])
        self.assertTrue(decided[('on-premises', 'github-enterprise', 'fully-disconnected')])
        self.assertTrue(decided[('local', 'github-enterprise', 'fully-disconnected')])
        self.assertTrue(decided[('cloud', 'github', 'restricted-connected')])
        self.assertFalse(decided[('cloud', 'github', 'fully-disconnected')])
        self.assertFalse(decided[('cloud', 'github-enterprise', 'fully-disconnected')])
        self.assertFalse(decided[('local', 'github', 'fully-disconnected')])
        self.assertFalse(decided[('on-premises', 'github', 'fully-disconnected')])
        problems = destinations.combination_problems('cloud', 'azure', 'github', 'fully-disconnected')
        self.assertEqual(len(problems), 2)
        self.assertIn('Azure', problems[0]); self.assertIn('github.com', problems[1])

    def test_unimplemented_providers_are_never_selectable(self):
        with patch.dict(destinations.PROVIDERS, {'aws': {'label': 'AWS', 'target': 'aws', 'implemented': False, 'note': ''}}), \
             patch.dict(destinations.LOCATIONS, {'cloud': dict(destinations.LOCATIONS['cloud'], providers=('azure', 'aws'))}):
            self.assertEqual([name for name, _, _ in setup_flow.provider_options('cloud')], ['azure'])
            with self.assertRaises(NomiarchError): destinations.provider_for('cloud', 'aws')
        with self.assertRaises(NomiarchError): destinations.provider_for('local', 'azure')

    def test_disconnected_choices_are_rejected_not_downgraded(self):
        for config, repo, mode in ((AZURE, GITHUB, 'fully-disconnected'), (AZURE, INTERNAL, 'fully-disconnected'),
                                   (foundation_tests.LOCAL, GITHUB, 'fully-disconnected'), (EXISTING, GITHUB, 'fully-disconnected')):
            with self.subTest(target=config['target'], repo=repo['mode']), self.assertRaises(NomiarchError):
                scaffold.infrastructure_project(config, 'customer', 'site', 'organisation', mode, repo)
        value = scaffold.infrastructure_project(EXISTING, 'customer', 'site', 'organisation', 'fully-disconnected', INTERNAL)
        self.assertEqual(value['isolation'], 'disconnected')
        self.assertEqual(destinations.network_mode_of(value['isolation']), 'fully-disconnected')

    def test_settings_shown_match_the_selected_destination_only(self):
        self.assertIn('ssh-host', setup_flow.settings_needed('on-premises', 'existing-host'))
        self.assertNotIn('azure-sign-in', setup_flow.settings_needed('on-premises', 'existing-host'))
        self.assertIn('azure-sign-in', setup_flow.settings_needed('cloud', 'azure'))
        self.assertEqual(setup_flow.settings_needed('local', 'multipass'), ['capacity', 'admitted-image'])
        self.assertTrue(any('does not create' in item for item in setup_flow.prerequisites('on-premises', 'existing-host', 'github-enterprise', 'fully-disconnected')))
        self.assertEqual(setup_flow.architecture_for('cloud', 'azure', 'arm64'), 'amd64')
        with self.assertRaises(NomiarchError): setup_flow.architecture_for('on-premises', 'existing-host', 'amd64', None)


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # canonical, like production paths

    def test_infrastructure_recipe_needs_no_core_model_or_runtime_pins(self):
        for config, repo, mode in ((foundation_tests.LOCAL, GITHUB, 'restricted-connected'), (EXISTING, INTERNAL, 'fully-disconnected'), (AZURE, GITHUB, 'restricted-connected')):
            value = scaffold.infrastructure_project(config, 'customer', 'site', 'organisation', mode, repo)
            self.assertEqual(value['recipe_version'], scaffold.INFRASTRUCTURE_RECIPE_VERSION)
            self.assertEqual(value['scope'], 'infrastructure')
            self.assertNotIn('runtime', value)
            files = scaffold.render(value, ROOT)
            self.assertNotIn('runtime.json', files)
            joined = ''.join(files.values())
            for forbidden in ('bundle_sha256', 'release_key_sha256', 'core_version', '/private/', 'gguf'):
                self.assertNotIn(forbidden, joined)
            self.assertIn('validation/validate.py', files); self.assertIn('validation/manifest.json', files)
            self.assertIn('.github/workflows/nomiarch-validate.yml', files)
            self.assertEqual(json.loads(files['policies/approvals.json'])['allow_agent_apply'], False)
            self.assertEqual(json.loads(files['policies/approvals.json'])['executor'], 'separate-human-operated-controller')
        inventory = infrastructure_inventory('amd64')
        self.assertNotIn('bundle', json.dumps(inventory)); self.assertIn('not required', inventory['models_and_core'])

    def test_legacy_recipes_are_unchanged_and_fields_do_not_leak_between_recipes(self):
        legacy = scaffold.project(foundation_tests.LOCAL, 'customer', 'evaluation')
        self.assertEqual(legacy['recipe_version'], '0.3.0'); self.assertIn('runtime', legacy)
        broken = copy.deepcopy(legacy); broken['scope'] = 'infrastructure'
        with self.assertRaises(NomiarchError): scaffold.validate_project(broken)
        value = scaffold.infrastructure_project(foundation_tests.LOCAL, 'customer', 'evaluation', 'organisation', 'restricted-connected', GITHUB)
        with_runtime = copy.deepcopy(value); with_runtime['runtime'] = legacy['runtime']
        with self.assertRaises(NomiarchError): scaffold.validate_project(with_runtime)
        downgraded = copy.deepcopy(value); downgraded['recipe_version'] = '0.3.0'
        with self.assertRaises(NomiarchError): scaffold.validate_project(downgraded)
        folder = self.root / 'legacy'; scaffold.create(folder, legacy, ROOT)
        loaded, _ = scaffold.load_supported(folder, ROOT)
        self.assertEqual(loaded, legacy)

    def test_vendored_validator_passes_generated_repository_and_rejects_weakened_policy(self):
        for config, repo in ((foundation_tests.LOCAL, GITHUB), (EXISTING, INTERNAL), (AZURE, GITHUB)):
            with self.subTest(target=config['target']):
                value = scaffold.infrastructure_project(config, 'customer', 'site', 'organisation', 'fully-disconnected' if config is EXISTING else 'restricted-connected', repo)
                folder = self.root / config['target']; scaffold.create(folder, value, ROOT)
                env = {'PATH': '/nonexistent'}  # tofu absent: the validator must skip, never download
                result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=folder, capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['deployment_credentials'], 'none')
                approvals = folder / 'policies/approvals.json'
                approvals.write_text(json.dumps(dict(scaffold.INFRASTRUCTURE_APPROVALS, allow_agent_apply=True)))
                result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=folder, capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1)
                with self.assertRaises(NomiarchError): scaffold.load_supported(folder, ROOT)
                # Even with a consistent manifest, the validator compares the policy content itself.
                manifest = read_json(folder / 'validation/manifest.json')
                manifest['files']['policies/approvals.json'] = digest(approvals)
                write_json(folder / 'validation/manifest.json', manifest)
                result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=folder, capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1); self.assertIn('approval policy', result.stdout)

    def test_validator_keeps_tofu_data_outside_the_repository_and_rejects_stray_files(self):
        value = scaffold.infrastructure_project(AZURE, 'customer', 'site', 'organisation', 'restricted-connected', GITHUB)
        folder = self.root / 'tofu'; scaffold.create(folder, value, ROOT)
        bin_dir = self.root / 'bin'; bin_dir.mkdir()
        tofu = bin_dir / 'tofu'
        # Stand-in tofu: records TF_DATA_DIR and would pollute the cwd if the variable were missing.
        tofu.write_text('#!/bin/sh\nmkdir -p "${TF_DATA_DIR:-.terraform}/providers"\necho "$TF_DATA_DIR" >> "' + str(self.root / 'data-dirs') + '"\n')
        tofu.chmod(0o755)
        env = {'PATH': str(bin_dir) + ':/usr/bin:/bin'}
        before = scaffold.snapshot(folder)
        for _ in range(2):
            result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=folder, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['terraform'], 'passed')
        self.assertEqual(scaffold.snapshot(folder), before)
        data_dirs = (self.root / 'data-dirs').read_text().split()
        self.assertTrue(data_dirs and all(not d.startswith(str(folder)) for d in data_dirs))
        stray = folder / 'environments/site/.terraform/providers'; stray.mkdir(parents=True); (stray / 'x').write_text('cache')
        result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=folder, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1); self.assertIn('not listed in the manifest', result.stdout)

    def test_validator_resource_loads_from_the_package_not_the_source_checkout(self):
        from nomiarch.desktop import selftest
        text = scaffold.validator_source()
        self.assertEqual(text, (ROOT / 'nomiarch/foundation/customer_validator.py').read_text())
        # Rendering must not depend on the `source` checkout for the validator.
        value = scaffold.infrastructure_project(foundation_tests.LOCAL, 'customer', 'site', 'organisation', 'restricted-connected', GITHUB)
        with patch('nomiarch.foundation.scaffold.validator_source', return_value=text) as loader:
            files = scaffold.render(value, self.root / 'not-a-checkout')
        loader.assert_called_once(); self.assertEqual(files['validation/validate.py'], text)
        # A damaged or missing resource fails closed instead of generating an empty check.
        with patch('nomiarch.foundation.scaffold.Path.read_text', side_effect=OSError('gone')):
            with self.assertRaises(NomiarchError): scaffold.validator_source()
        # The packaged self-test generates and validates every destination from package resources.
        results = selftest.scaffold_acceptance(ROOT, self.root / 'acceptance')
        self.assertEqual({results[k]['validator'] for k in ('local', 'existing', 'azure')}, {'passed'})
        self.assertEqual(results['core_recipe'], '0.3.0')

    def test_generated_files_never_contain_credentials_or_private_material(self):
        value = scaffold.infrastructure_project(EXISTING, 'customer', 'site', 'organisation', 'fully-disconnected', INTERNAL)
        files = scaffold.render(value, ROOT)
        joined = ''.join(files.values())
        self.assertNotIn('/private/key', joined); self.assertNotIn('/private/known_hosts', joined)
        self.assertEqual(json.loads(files['foundation.json'])['configuration']['existing']['ssh_key'], '@controller')
        self.assertIn('known_hosts', files['.gitignore'])
        self.assertEqual(json.loads(files['environments/site/host.json'])['deletable_by_nomiarch'], False)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.state = self.root / 'state'  # canonical, like production paths
        FakeProvider.calls, FakeProvider.failure, FakeProvider.commands = [], None, []
        FakeProvider.facts, FakeProvider.probe = dict(FACTS), {'ipv4': 'blocked', 'ipv6': 'blocked', 'public_dns': 'unresolvable'}
        FakeProvider.reset_azure()
        self.addCleanup(patch.stopall)
        self.install = patch('nomiarch.bootstrap.controller.install_remote', side_effect=AssertionError('application installation must not run')).start()
        patch('nomiarch.foundation.infrastructure.network.configure', return_value={'guest_egress': 'blocked', 'configured': True}).start()
        patch('nomiarch.foundation.infrastructure.network.verify', return_value={'guest_egress': 'blocked', 'configured': False}).start()
        self.image = self.root / 'ubuntu.img'; self.image.write_bytes(b'admitted image')
        pins = copy.deepcopy(scaffold.PINS); pins['amd64']['image'] = ('ubuntu.img', digest(self.image))
        patch('nomiarch.foundation.scaffold.PINS', pins).start()
        self.key = self.root / 'id_ed25519'; self.key.write_bytes(b'private key material'); self.key.chmod(0o600)
        self.known = self.root / 'known_hosts'; self.known.write_text('host.site.internal ssh-ed25519 AAAAC3Nza\n')

    def configuration(self, target):
        if target == 'local':
            config = copy.deepcopy(foundation_tests.LOCAL); config['local'] = {'image': str(self.image), 'image_sha256': digest(self.image)}
            return config
        return copy.deepcopy({'existing': EXISTING, 'azure': AZURE}[target])

    def paths(self, target):
        return {'local': {'image': str(self.image)}, 'existing': {'ssh_key': str(self.key), 'known_hosts': str(self.known)}, 'azure': {}}[target]

    def plan(self, target='local', repo=None, mode='restricted-connected', organisation=False):
        repo = repo or (GITHUB if organisation else {'mode': 'local'})
        self.value = scaffold.infrastructure_project(self.configuration(target), 'customer', 'site', 'organisation' if organisation else 'personal', mode, repo)
        self.folder = self.root / ('project-' + target + self.value['id'][:6]); scaffold.create(self.folder, self.value, ROOT)
        result = deployment.prepare(self.folder, self.state, ROOT, self.paths(target), factory=FakeProvider)
        return Path(result['run_directory']), result['plan']

    def test_planning_creates_nothing_reads_no_release_and_refuses_release_files(self):
        for target in ('local', 'existing', 'azure'):
            with self.subTest(target=target):
                FakeProvider.calls = []
                directory, plan = self.plan(target)
                self.assertEqual([c for c in FakeProvider.calls if c != 'remote'], ['prepare'])
                self.assertEqual(plan['scope'], 'infrastructure')
                self.assertNotIn('bundle', plan['artifacts']); self.assertNotIn('key', plan['artifacts'])
                self.assertEqual(read_json(directory / 'run.json')['phase'], 'awaiting-approval')
                self.assertEqual(read_json(directory / 'run.json')['application']['status'], 'not-installed')
                self.assertIn('infrastructure only' if target == 'local' else 'no VM is created' if target == 'existing' else 'OpenTofu plan', plan['summary']['action'])
        self.assertNotIn('secret-cloud-init', json.dumps(plan))
        with self.assertRaises(NomiarchError):
            deployment.prepare(self.root / 'none', self.state, ROOT, dict(self.paths('local'), bundle='/x', key='/y'), factory=FakeProvider)

    def test_approved_plan_establishes_infrastructure_without_installing_anything(self):
        for target in ('local', 'existing', 'azure'):
            with self.subTest(target=target):
                directory, plan = self.plan(target)
                result = deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
                self.assertEqual(result['phase'], 'infrastructure-ready')
                self.assertEqual(result['validation']['scope'], 'infrastructure')
                self.assertEqual(result['application']['status'], 'not-installed')
                self.install.assert_not_called()
                run = read_json(directory / 'run.json')
                self.assertNotEqual(run['phase'], 'ready')  # application readiness is a different state
                checks = run['validation']['checks']
                self.assertEqual(checks['observed']['cpus'], 4)
                self.assertEqual(checks['physical_air_gap'], 'not established by software checks')
                if target == 'local': self.assertTrue(checks['network']['configured'])
                if target == 'existing': self.assertFalse(run['inventory']['deletable_by_nomiarch'])
                with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
                status = infrastructure.status(directory)
                self.assertEqual(status['phase'], 'infrastructure-ready'); self.assertEqual(status['application']['status'], 'not-installed')

    def azure_result(self, verify_again=False):
        directory, plan = self.plan('azure')
        result = deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        if verify_again and result['phase'] == 'infrastructure-ready':
            return directory, deployment.verify_infrastructure(directory, ROOT, factory=FakeProvider)
        return directory, result

    def test_azure_os_disk_plus_attached_data_disk_passes_and_reports_what_was_measured(self):
        for data_gib in (40, 64, 512):
            with self.subTest(data_gib=data_gib):
                FakeProvider.reset_azure(data_gib=data_gib)
                FakeProvider.calls = []
                _, result = self.azure_result()
                self.assertEqual(result['phase'], 'infrastructure-ready', result['validation'].get('error'))
                storage = result['validation']['checks']['storage']
                self.assertLess(storage['os_root_filesystem_gib'], 40)  # the root is the 32 GiB OS disk, not the data disk
                self.assertEqual(storage['data_disk']['attached_size_gib'], data_gib)
                self.assertEqual(storage['data_disk']['lun'], '0')
                self.assertEqual(storage['data_disk']['control_plane']['disk_size_gb'], data_gib)
                self.assertIn('not formatted or mounted', storage['data_disk']['inspection'])
                self.assertIn('az:disk show', FakeProvider.calls)

    def test_azure_missing_wrong_or_undersized_data_disk_fails(self):
        other_vm = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/other/providers/Microsoft.Compute/virtualMachines/x'
        cases = {'missing': dict(resolved=None), 'wrong-lun': dict(hctl='1:0:0:3'), 'undersized-device': dict(data_gib=20),
                 'undersized-control-plane': dict(control_gib=20), 'attached-elsewhere': dict(managed_by=other_vm),
                 'different-managed-disk': dict(disk_id='/subscriptions/x/resourceGroups/y/providers/Microsoft.Compute/disks/other'),
                 'not-a-whole-disk': dict(resolved='/dev/sdc1')}
        messages = {}
        for label, kwargs in cases.items():
            with self.subTest(label=label):
                FakeProvider.reset_azure(**kwargs)
                directory, result = self.azure_result()
                self.assertEqual(result['phase'], 'failed', label)
                messages[label] = result['validation']['error']
                self.assertEqual(read_json(directory / 'run.json')['application']['status'], 'not-installed')
        self.assertIn('not attached', messages['missing'])
        self.assertIn('LUN', messages['wrong-lun'])
        self.assertIn('requires 40 GiB', messages['undersized-device'])
        self.assertIn('not attached to the approved VM', messages['attached-elsewhere'])

    def test_azure_root_filesystem_is_not_compared_with_the_data_disk_size(self):
        # The reviewed bug: 32 GiB OS disk vs. a 40 GiB declared data disk must not fail.
        FakeProvider.reset_azure(data_gib=40)
        _, result = self.azure_result()
        self.assertEqual(result['phase'], 'infrastructure-ready')
        # Local and adopted hosts keep root-filesystem semantics.
        FakeProvider.facts = dict(FACTS, disk_bytes=20 * 1024 ** 3)
        directory, plan = self.plan('existing')
        result = deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        self.assertEqual(result['phase'], 'failed'); self.assertIn('Root storage', result['validation']['error'])

    def test_verification_performs_no_storage_mutation(self):
        FakeProvider.reset_azure()
        directory, result = self.azure_result(verify_again=True)
        self.assertEqual(result['validation']['status'], 'passed')
        joined = ' '.join(' '.join(c) for c in FakeProvider.commands)
        for forbidden in ('mkfs', 'mount ', 'parted', 'fdisk', 'wipefs', 'sgdisk', 'dd ', 'tee ', 'installer.pyz'):
            self.assertNotIn(forbidden, joined)
        self.assertTrue(all(c[:2] == ['python3', '-c'] for c in FakeProvider.commands))
        self.install.assert_not_called()

    def test_adopted_host_requires_capacity_and_disconnected_probe(self):
        FakeProvider.facts = dict(FACTS, cpus=2)
        directory, plan = self.plan('existing', INTERNAL, 'fully-disconnected', organisation=True)
        result = deployment.apply(directory, ROOT, plan['digest'], repository_check=lambda *args: {'merge': 'b' * 40}, factory=FakeProvider)
        self.assertEqual(result['phase'], 'failed'); self.assertIn('CPUs', result['validation']['error'])
        self.assertTrue((directory / 'report.json').exists())
        FakeProvider.facts = dict(FACTS); FakeProvider.probe = {'ipv4': 'reachable', 'ipv6': 'blocked', 'public_dns': 'resolves'}
        directory, plan = self.plan('existing', INTERNAL, 'fully-disconnected', organisation=True)
        result = deployment.apply(directory, ROOT, plan['digest'], repository_check=lambda *args: {'merge': 'b' * 40}, factory=FakeProvider)
        self.assertEqual(result['phase'], 'failed'); self.assertIn('not inside a disconnected boundary', result['validation']['error'])

    def test_adopted_hosts_are_never_destroyed(self):
        directory, plan = self.plan('existing')
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        with self.assertRaises(NomiarchError): maintenance.propose(directory, ROOT, 'destroy')
        run = read_json(directory / 'run.json')
        with self.assertRaises(NomiarchError): controller.cleanup(directory, run, ROOT, factory=FakeProvider)
        self.assertNotIn('destroy', FakeProvider.calls)
        forged = copy.deepcopy(self.value); forged['maintenance'] = {'action': 'destroy', 'id': 'c' * 32, 'configuration_sha256': 'd' * 64}
        with self.assertRaises(NomiarchError): scaffold.validate_project(forged)

    def test_removal_of_created_vm_needs_its_own_reviewed_operation(self):
        directory, plan = self.plan('local', GITHUB, organisation=True)
        deployment.apply(directory, ROOT, plan['digest'], repository_check=lambda *args: {'merge': 'b' * 40}, factory=FakeProvider)
        for action in ('upgrade', 'repair'):
            with self.assertRaises(NomiarchError): maintenance.propose(directory, ROOT, action)
        proposal = maintenance.propose(directory, ROOT, 'destroy')
        folder = self.root / 'removal'; scaffold.create(folder, proposal, ROOT)
        planned = maintenance.prepare(directory, folder, ROOT, factory=FakeProvider)
        with self.assertRaises(NomiarchError): maintenance.apply(planned['operation_directory'], ROOT, planned['plan']['digest'], factory=FakeProvider)
        self.assertNotIn('destroy', FakeProvider.calls)
        maintenance.apply(planned['operation_directory'], ROOT, planned['plan']['digest'], repository_check=lambda *args: {'merge': 'c' * 40}, factory=FakeProvider)
        self.assertIn('destroy', FakeProvider.calls)

    def test_tampered_expired_and_replayed_plans_are_refused(self):
        directory, plan = self.plan('existing')
        record = read_json(directory / 'plan.json'); record['configuration']['capacity']['cpus'] = 1
        write_json(directory / 'plan.json', record)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        directory, plan = self.plan('existing')
        with patch('nomiarch.foundation.deployment.time.time', return_value=plan['expires'] + 1):
            with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        directory, plan = self.plan('existing')
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, 'f' * 64, factory=FakeProvider)
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        self.assertEqual(FakeProvider.calls.count('provision'), 1)

    def test_configuration_or_private_inputs_changed_after_planning_invalidate_it(self):
        directory, plan = self.plan('existing')
        changed = copy.deepcopy(self.value); changed['configuration']['capacity']['cpus'] = 8
        for name, content in scaffold.render(changed, ROOT).items(): (self.folder / name).write_text(content)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        directory, plan = self.plan('existing')
        (directory / 'known_hosts').write_text('host.site.internal ssh-ed25519 REPLACED\n')
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        directory, plan = self.plan('azure')
        (directory / 'apply.tfplan').write_bytes(b'different plan')
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def test_failed_provisioning_retains_state_and_blocks_duplicate_runs(self):
        directory, plan = self.plan('local')
        FakeProvider.failure = NomiarchError('hypervisor rejected the launch')
        result = deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        self.assertEqual(result['phase'], 'failed')
        run = read_json(directory / 'run.json')
        self.assertTrue(run['creation_started']); self.assertIn('Nothing was deleted', run['recovery']['instruction'])
        self.assertTrue((directory / 'plan.json').exists() and (directory / 'report.json').exists())
        with self.assertRaises(NomiarchError) as caught:
            deployment.prepare(self.folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)
        self.assertIn('duplicate', str(caught.exception))
        self.assertEqual(len(infrastructure.existing_runs(self.state, self.value['id'])), 1)
        self.assertEqual(FakeProvider.calls.count('provision'), 1)

    def test_reopened_valid_plan_is_approved_once(self):
        directory, plan = self.plan('local')
        pending = deployment.saved_plan(self.state, self.value['id'], ROOT)
        self.assertEqual((pending['status'], pending['run_directory']), ('valid', str(directory)))
        self.assertEqual(pending['plan']['digest'], plan['digest'])
        with self.assertRaises(NomiarchError): deployment.retire_plan(directory, ROOT, 'replace a valid plan')
        deployment.apply(directory, ROOT, pending['plan']['digest'], factory=FakeProvider)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, pending['plan']['digest'], factory=FakeProvider)
        self.assertIsNone(deployment.saved_plan(self.state, self.value['id'], ROOT))
        self.assertEqual(FakeProvider.calls.count('provision'), 1)

    def test_expired_unused_plan_is_retired_with_audit_and_replaced(self):
        old_dir, old = self.plan('local')
        later = old['expires'] + 10
        with patch('nomiarch.foundation.deployment.time.time', return_value=later):
            pending = deployment.saved_plan(self.state, self.value['id'], ROOT)
            self.assertEqual(pending['status'], 'expired'); self.assertTrue(pending['unused'])
            with self.assertRaises(NomiarchError) as caught:
                deployment.prepare(self.folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)
            self.assertIn('retire', str(caught.exception))
            evidence = deployment.retire_plan(old_dir, ROOT, 'expired before approval')
            self.assertTrue(evidence['expired']); self.assertIn('expired', evidence['invalid_because'])
            fresh = deployment.prepare(self.folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)
            # The retired plan never becomes executable again, even with a matching digest.
            with self.assertRaises(NomiarchError): deployment.apply(old_dir, ROOT, old['digest'], factory=FakeProvider)
            with self.assertRaises(NomiarchError): deployment.retire_plan(old_dir, ROOT, 'again')
            deployment.apply(fresh['run_directory'], ROOT, fresh['plan']['digest'], factory=FakeProvider)
        self.assertEqual(FakeProvider.calls.count('provision'), 1)
        self.assertTrue((old_dir / 'plan.json').exists() and (old_dir / 'retired.json').exists())
        self.assertEqual(read_json(old_dir / 'run.json')['phase'], 'retired')
        log = [json.loads(line) for line in (self.state / 'foundations' / (self.value['id'] + '.audit.jsonl')).read_text().splitlines()]
        self.assertEqual([e['event'] for e in log], ['plan-prepared', 'plan-retired', 'plan-prepared'])
        # Restoring an old run.json phase cannot resurrect a retired plan.
        run = read_json(old_dir / 'run.json'); run['phase'] = 'awaiting-approval'; write_json(old_dir / 'run.json', run)
        with self.assertRaises(NomiarchError): deployment.apply(old_dir, ROOT, old['digest'], factory=FakeProvider)

    def test_configuration_change_invalidates_saved_plan_and_requires_a_new_one(self):
        old_dir, old = self.plan('existing')
        changed = copy.deepcopy(self.value); changed['configuration']['capacity']['cpus'] = 8
        for name, content in scaffold.render(changed, ROOT).items(): (self.folder / name).write_text(content)
        pending = deployment.saved_plan(self.state, self.value['id'], ROOT)
        self.assertEqual(pending['status'], 'invalid'); self.assertIn('changed', pending['reason'])
        with self.assertRaises(NomiarchError): deployment.apply(old_dir, ROOT, old['digest'], factory=FakeProvider)
        deployment.retire_plan(old_dir, ROOT, 'configuration changed after review')
        fresh = deployment.prepare(self.folder, self.state, ROOT, self.paths('existing'), factory=FakeProvider)
        self.assertEqual(fresh['plan']['configuration']['capacity']['cpus'], 8)
        self.assertNotEqual(fresh['plan']['digest'], old['digest'])

    def test_provisioned_partial_or_uncertain_runs_cannot_be_retired(self):
        directory, plan = self.plan('local')
        FakeProvider.failure = NomiarchError('launch interrupted')
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)  # partial: creation started, failed
        with patch('nomiarch.foundation.deployment.time.time', return_value=plan['expires'] + 10):
            with self.assertRaises(NomiarchError) as caught: deployment.retire_plan(directory, ROOT, 'try to hide a partial run')
        self.assertIn('Only an unused plan', str(caught.exception))
        FakeProvider.failure = None
        for mutate in (lambda d, r: (d / 'approval.json').write_text('{}'),  # approved but run record not yet updated
                       lambda d, r: write_json(d / 'run.json', dict(r, creation_started=True)),
                       lambda d, r: write_json(d / 'run.json', dict(r, inventory={'host': 'x'}))):
            with self.subTest(mutate=mutate):
                self.root.joinpath('again').mkdir(exist_ok=True)
                other = scaffold.infrastructure_project(self.configuration('local'), 'customer', 'site')
                folder = self.root / ('again/' + other['id']); scaffold.create(folder, other, ROOT)
                result = deployment.prepare(folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)
                d = Path(result['run_directory']); mutate(d, read_json(d / 'run.json'))
                with patch('nomiarch.foundation.deployment.time.time', return_value=result['plan']['expires'] + 10):
                    with self.assertRaises(NomiarchError): deployment.retire_plan(d, ROOT, 'uncertain state')
                self.assertFalse((d / 'retired.json').exists())
        # Adopted hosts keep their own never-delete rule after any plan lifecycle.
        directory, plan = self.plan('existing')
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        with self.assertRaises(NomiarchError): deployment.retire_plan(directory, ROOT, 'retire adopted host')
        with self.assertRaises(NomiarchError): maintenance.propose(directory, ROOT, 'destroy')

    def test_concurrent_planning_and_retirement_cannot_create_two_executable_plans(self):
        import threading
        value = scaffold.infrastructure_project(self.configuration('local'), 'customer', 'race')
        folder = self.root / 'race'; scaffold.create(folder, value, ROOT)
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeProvider):
            def prepare(self):
                entered.set(); release.wait(5); super().prepare()
        results, errors = [], []
        def plan():
            try: results.append(deployment.prepare(folder, self.state, ROOT, self.paths('local'), factory=Slow))
            except NomiarchError as error: errors.append(str(error))
        first = threading.Thread(target=plan); first.start(); entered.wait(5)
        plan()  # second window while the first holds the foundation lock
        release.set(); first.join(5)
        self.assertEqual(len(results), 1); self.assertEqual(len(errors), 1)
        self.assertIn('Another controller', errors[0])
        directory = Path(results[0]['run_directory'])
        # An apply in progress holds the run lock; retirement cannot race it.
        provisioning, finish = threading.Event(), threading.Event()
        class SlowApply(FakeProvider):
            def provision(self):
                provisioning.set(); finish.wait(5); return super().provision()
        applied = threading.Thread(target=lambda: deployment.apply(directory, ROOT, results[0]['plan']['digest'], factory=SlowApply))
        applied.start(); provisioning.wait(5)
        with patch('nomiarch.foundation.deployment.time.time', return_value=results[0]['plan']['expires'] + 10):
            with self.assertRaises(NomiarchError): deployment.retire_plan(directory, ROOT, 'race the apply')
        with self.assertRaises(NomiarchError): deployment.prepare(folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)
        finish.set(); applied.join(5)
        self.assertEqual(read_json(directory / 'run.json')['phase'], 'infrastructure-ready')
        self.assertFalse((directory / 'retired.json').exists())
        self.assertEqual(len([r for r in infrastructure.existing_runs(self.state, value['id'])]), 1)

    def test_reopening_a_planned_configuration_does_not_plan_twice(self):
        self.plan('local')
        with self.assertRaises(NomiarchError): deployment.prepare(self.folder, self.state, ROOT, self.paths('local'), factory=FakeProvider)

    def test_organisation_requires_current_repository_proof(self):
        directory, plan = self.plan('local', GITHUB, organisation=True)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def test_verification_is_read_only_and_requires_retained_infrastructure(self):
        directory, plan = self.plan('local')
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        result = deployment.verify_infrastructure(directory, ROOT, factory=FakeProvider)
        self.assertFalse(result['validation']['checks']['network']['configured'])
        self.assertTrue((directory / 'verification.json').exists())
        self.install.assert_not_called()
        planned, _ = self.plan('existing')
        with self.assertRaises(NomiarchError): deployment.verify_infrastructure(planned, ROOT, factory=FakeProvider)

    def test_backup_exports_records_and_lists_private_material_separately(self):
        directory, plan = self.plan('existing')
        deployment.apply(directory, ROOT, plan['digest'], factory=FakeProvider)
        (directory / 'logs').mkdir(); (directory / 'logs/1.log').write_text('command output')
        result = backup.export(directory, self.root / 'backup.zip', ROOT)
        names = set(result['manifest']['files'])
        self.assertIn('repository/foundation.json', names); self.assertIn('controller/run.json', names); self.assertIn('controller/known_hosts', names)
        self.assertNotIn('controller/ssh-client', names); self.assertNotIn('controller/logs/1.log', names)
        self.assertIn('ssh-client', result['manifest']['kept_separately'])
        self.assertIn('logs/1.log', result['manifest']['exported_only_when_encrypted'])
        import zipfile
        with zipfile.ZipFile(self.root / 'backup.zip') as z:
            self.assertNotIn('private key material', b''.join(z.read(n) for n in z.namelist()).decode(errors='replace'))
        with self.assertRaises(NomiarchError): backup.export(directory, self.root / 'backup.zip', ROOT)

    def test_private_paths_and_secrets_stay_out_of_plan_summaries(self):
        directory, plan = self.plan('azure')
        self.assertNotIn('secret-cloud-init', json.dumps(plan['summary']))
        self.assertEqual(plan['summary']['resources'][0]['planned']['custom_data'], '[sensitive value omitted]')
        directory, plan = self.plan('existing')
        self.assertNotIn('private key material', json.dumps(plan))


class OfflineKitTests(unittest.TestCase):
    def test_missing_offline_artifacts_fail_closed_without_download(self):
        with tempfile.TemporaryDirectory() as folder, patch('urllib.request.urlopen') as network:
            with self.assertRaises(NomiarchError) as caught: service.prepare_infrastructure_kit(folder, 'amd64', False)
            self.assertIn('no download was attempted', str(caught.exception))
            network.assert_not_called()
            name, _ = scaffold.PINS['amd64']['image']
            (Path(folder) / name).write_bytes(b'altered image')
            with self.assertRaises(NomiarchError): service.prepare_infrastructure_kit(folder, 'amd64', False)
            network.assert_not_called()

    def test_kit_admits_only_infrastructure_artifacts(self):
        with tempfile.TemporaryDirectory() as folder, patch('nomiarch.desktop.service.platform.system', return_value='Linux'):
            name, sha = scaffold.PINS['amd64']['image']
            with patch('nomiarch.desktop.service.download') as download:
                download.side_effect = lambda url, path, expected, *args: Path(path)
                paths = service.prepare_infrastructure_kit(folder, 'amd64', True)
            self.assertEqual(set(paths), {'image'})
            self.assertTrue(all('nomiarch-amd64' not in call.args[0] for call in download.call_args_list))


class RequiredCheckTests(foundation_tests.RepositoryReviewTests):
    """Exact-commit review plus the trusted validation check for infrastructure recipes."""
    def setUp(self):
        super().setUp()
        self.value = scaffold.infrastructure_project(foundation_tests.LOCAL, 'example', 'evaluation', 'organisation', 'restricted-connected', dict(GITHUB, name='foundation'))
        self.protection['required_status_checks'] = {'strict': True, 'contexts': ['nomiarch-validate'], 'checks': [{'context': 'nomiarch-validate', 'app_id': 15368}]}
        self.check_runs = [{'id': 1, 'name': 'nomiarch-validate', 'head_sha': self.head, 'status': 'completed', 'conclusion': 'success',
                            'completed_at': '2026-10-12T10:00:00Z', 'app': {'id': 15368, 'slug': 'github-actions'}, 'html_url': 'https://github.com/run/1'}]
        self.client.call = self.call

    def call(self, path, *args):
        if '/check-runs?' in path:
            self.assertIn('/commits/' + self.head + '/', path)
            self.assertIn('filter=all', path); self.assertIn('check_name=nomiarch-validate', path)
            self.check_queries = getattr(self, 'check_queries', []) + [path]
            page = int(path.rsplit('page=', 1)[1])
            size = int(path.split('per_page=')[1].split('&')[0])
            total = getattr(self, 'reported_total', None)
            return {'total_count': len(self.check_runs) if total is None else total,
                    'check_runs': self.check_runs[(page - 1) * size: page * size]}
        return super().call(path, *args)

    def run_of(self, run_id, status='completed', conclusion='success', **extra):
        return dict(self.check_runs_template, id=run_id, status=status, conclusion=conclusion,
                    completed_at=None if status != 'completed' else '2026-10-12T1%d:00:00Z' % (run_id % 10), **extra)

    def decide(self, runs):
        self.check_runs = runs
        try:
            return self.client.approved(self.value, self.snapshot, 7)['validation']['check_run_id']
        except NomiarchError:
            return None

    def test_newest_run_decides_and_older_success_is_never_reused(self):
        self.check_runs_template = dict(self.check_runs[0])
        old_success = self.run_of(10)
        for label, newer in {'queued': self.run_of(11, 'queued', None), 'in-progress': self.run_of(11, 'in_progress', None),
                             'waiting': self.run_of(11, 'waiting', None), 'failure': self.run_of(11, conclusion='failure'),
                             'cancelled': self.run_of(11, conclusion='cancelled'), 'timed-out': self.run_of(11, conclusion='timed_out'),
                             'action-required': self.run_of(11, conclusion='action_required'), 'neutral': self.run_of(11, conclusion='neutral'),
                             'skipped': self.run_of(11, conclusion='skipped'), 'stale': self.run_of(11, conclusion='stale')}.items():
            with self.subTest(label=label):
                self.assertIsNone(self.decide([old_success, newer]))
                self.assertIsNone(self.decide([newer, old_success]))  # API order does not matter
        # Old failure, newer completed success: accepted, and the newer run is the evidence.
        self.assertEqual(self.decide([self.run_of(10, conclusion='failure'), self.run_of(12)]), 12)
        self.assertEqual(self.decide([self.run_of(12), self.run_of(10, conclusion='failure')]), 12)
        # A newer completion time on an older run does not outrank a newer run.
        older_finished_late = dict(self.run_of(10), completed_at='2026-10-13T00:00:00Z')
        self.assertIsNone(self.decide([older_finished_late, self.run_of(11, 'in_progress', None)]))
        # Untrusted or unrelated newer runs are ignored, not allowed to authorize or to block.
        self.assertEqual(self.decide([self.run_of(10), self.run_of(20, app={'id': 99, 'slug': 'someones-app'})]), 10)
        self.assertEqual(self.decide([self.run_of(10), self.run_of(20, head_sha='c' * 40)]), 10)
        self.assertEqual(self.decide([self.run_of(10), self.run_of(20, name='nomiarch-validate-lookalike')]), 10)
        self.assertIsNone(self.decide([self.run_of(20, app={'id': 99, 'slug': 'someones-app'})]))
        # Malformed identities fail closed.
        self.assertIsNone(self.decide([dict(self.run_of(10), id='10')]))

    def test_all_pages_are_read_and_incomplete_listings_fail_closed(self):
        self.check_runs_template = dict(self.check_runs[0])
        runs = [self.run_of(i) for i in range(1, 151)] + [self.run_of(151, 'in_progress', None)]
        self.check_queries = []
        self.assertIsNone(self.decide(runs))  # the pending run is on page 2
        self.assertEqual([q.rsplit('page=', 1)[1] for q in self.check_queries], ['1', '2'])
        self.assertEqual(self.decide(runs[:-1]), 150)
        self.reported_total = 400  # API claims more runs than it returns
        self.assertIsNone(self.decide(runs[:-1]))

    def test_exact_review_matches_scaffold_and_separate_human(self):
        proof = self.client.approved(self.value, self.snapshot, 7)
        self.assertEqual(proof['human_reviewers'], ['reviewer'])
        self.assertEqual(proof['validation']['head_sha'], self.head)
        self.assertEqual(proof['validation']['check_run_id'], 1)

    def test_missing_failed_stale_untrusted_or_unfinished_checks_are_rejected(self):
        variants = {'missing': [], 'failed': [dict(self.check_runs[0], conclusion='failure')],
                    'stale': [dict(self.check_runs[0], head_sha='c' * 40)], 'unfinished': [dict(self.check_runs[0], status='in_progress', conclusion=None)],
                    'other-app': [dict(self.check_runs[0], app={'id': 99, 'slug': 'someones-app'})],
                    'spoofed-slug': [dict(self.check_runs[0], app={'id': 99, 'slug': 'github-actions'})],
                    'renamed': [dict(self.check_runs[0], name='nomiarch-validate-lookalike')],
                    'later-failure': [self.check_runs[0], dict(self.check_runs[0], id=2, conclusion='failure', completed_at='2026-10-12T11:00:00Z')]}
        for label, runs in variants.items():
            with self.subTest(label=label):
                self.check_runs = runs
                with self.assertRaises(NomiarchError): self.client.approved(self.value, self.snapshot, 7)

    def test_proposer_cannot_approve_own_change_even_when_designated(self):
        self.value['repository']['approvers'] = ['proposer', 'reviewer']
        self.reviews = [{'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'proposer', 'type': 'User'}}]
        with self.assertRaises(NomiarchError): self.client.approved(self.value, self.snapshot, 7)

    def test_weakened_required_checks_block_planning_and_protection_requests_them(self):
        for weakened in ({'strict': False, 'checks': [{'context': 'nomiarch-validate', 'app_id': 15368}]},
                         {'strict': True, 'checks': [{'context': 'nomiarch-validate', 'app_id': 99}]},
                         {'strict': True, 'checks': [{'context': 'other', 'app_id': 15368}]}, None):
            with self.subTest(weakened=weakened):
                self.protection['required_status_checks'] = weakened
                with self.assertRaises(NomiarchError): self.client.assert_protection(self.value)
        self.client.call = Mock(return_value={})
        self.client.protect(self.value)
        body = self.client.call.call_args.args[1]
        self.assertEqual(body['required_status_checks'], {'strict': True, 'checks': [{'context': 'nomiarch-validate', 'app_id': 15368}]})
        self.assertTrue(body['enforce_admins'])
        internal = GitHub('test-memory-token', INTERNAL)
        value = scaffold.infrastructure_project(EXISTING, 'example', 'site', 'organisation', 'fully-disconnected', INTERNAL)
        internal.call = Mock(return_value={})
        internal.protect(value)
        self.assertEqual(internal.call.call_args.args[1]['required_status_checks'], {'strict': True, 'checks': [{'context': 'nomiarch-validate'}]})

    def test_legacy_recipe_protection_contract_is_untouched(self):
        legacy = scaffold.project(foundation_tests.LOCAL, 'example', 'evaluation', 'organisation', repository=dict(GITHUB, name='foundation'))
        self.client.call = Mock(return_value={})
        self.client.protect(legacy)
        self.assertIsNone(self.client.call.call_args.args[1]['required_status_checks'])


class InternalRequiredCheckTests(RequiredCheckTests):
    def setUp(self):
        super().setUp()
        self.value = scaffold.infrastructure_project(foundation_tests.LOCAL, 'example', 'evaluation', 'organisation', 'fully-disconnected', dict(INTERNAL, name='foundation'))
        self.client = GitHub('test-memory-token', dict(INTERNAL, name='foundation'))
        self.client.call = self.call
        self.protection['required_status_checks'] = {'strict': True, 'contexts': ['nomiarch-validate'], 'checks': [{'context': 'nomiarch-validate', 'app_id': None}]}
        self.check_runs[0]['app'] = {'id': 7, 'slug': 'github-actions'}

    def test_missing_failed_stale_untrusted_or_unfinished_checks_are_rejected(self):
        for runs in ([], [dict(self.check_runs[0], head_sha='c' * 40)], [dict(self.check_runs[0], app={'id': 7, 'slug': 'other'})], [dict(self.check_runs[0], conclusion='failure')]):
            self.check_runs = runs
            with self.assertRaises(NomiarchError): self.client.approved(self.value, self.snapshot, 7)

    def test_weakened_required_checks_block_planning_and_protection_requests_them(self):
        self.protection['required_status_checks'] = {'strict': False, 'contexts': ['nomiarch-validate'], 'checks': [{'context': 'nomiarch-validate', 'app_id': None}]}
        with self.assertRaises(NomiarchError): self.client.assert_protection(self.value)

    def test_legacy_recipe_protection_contract_is_untouched(self):
        pass


class SetupFlowTests(unittest.TestCase):
    def test_build_assembles_each_destination_without_release_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder).resolve()  # setup_flow returns canonical paths
            key = Path(folder) / 'key'; key.write_bytes(b'k'); hosts = Path(folder) / 'kh'; hosts.write_text('h ssh-ed25519 A\n')
            existing = setup_flow.existing_host_settings('host.site.internal', 'nomiarch', str(key), str(hosts))
            repository = setup_flow.repository_settings('github-enterprise', 'customer', 'infra', '@reviewer, second', 'https://Git.Company.Internal/', None, 'organisation', 'disconnected')
            choices = {'location': 'on-premises', 'provider': 'existing-host', 'git_service': 'github-enterprise', 'network_mode': 'fully-disconnected', 'organisation': 'customer', 'environment': 'site'}
            value = setup_flow.build(choices, {'name': 'site-host', 'architecture': 'arm64', 'capacity': setup_flow.capacity('4', '16', '100'), 'existing': existing}, repository)
            self.assertEqual(value['configuration']['existing']['ssh_key'], '@controller')
            self.assertEqual(setup_flow.controller_paths(value, {'existing': existing}), {'ssh_key': str(key), 'known_hosts': str(hosts)})
            self.assertEqual(value['repository']['approvers'], ['reviewer', 'second'])
            local = setup_flow.build(dict(choices, location='local', provider='multipass', git_service='github', network_mode='restricted-connected'),
                                     {'name': 'local-vm', 'architecture': 'amd64', 'capacity': setup_flow.capacity(2, 8, 40), 'image': '/kit/ubuntu.img'},
                                     setup_flow.repository_settings('github', 'customer', 'infra', 'reviewer'))
            self.assertEqual(setup_flow.controller_paths(local, {'image': '/kit/ubuntu.img'}), {'image': '/kit/ubuntu.img'})
            azure = setup_flow.build(dict(choices, location='cloud', provider='azure', git_service='github', network_mode='restricted-connected'),
                                     {'name': 'cloud-vm', 'architecture': 'amd64', 'capacity': setup_flow.capacity(2, 8, 40),
                                      'azure': {k: v for k, v in AZURE['azure'].items() if k not in {'ssh_key', 'ssh_public_key'}}},
                                     setup_flow.repository_settings('github', 'customer', 'infra', 'reviewer'))
            self.assertEqual(setup_flow.controller_paths(azure, {}), {})
            with self.assertRaises(NomiarchError):
                setup_flow.build(dict(choices, location='cloud', provider='azure'), {'name': 'x', 'architecture': 'amd64', 'capacity': setup_flow.capacity(2, 8, 40), 'azure': AZURE['azure']}, repository)
            with self.assertRaises(NomiarchError): setup_flow.capacity('two', 8, 40)
            with self.assertRaises(NomiarchError): setup_flow.existing_host_settings('host; rm', 'nomiarch', str(key), str(hosts))


if __name__ == '__main__': unittest.main()
