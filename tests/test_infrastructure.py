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
    calls, facts, probe = [], dict(FACTS), {'ipv4': 'blocked', 'ipv6': 'blocked', 'public_dns': 'unresolvable'}
    failure = None

    def __init__(self, config, run, directory, runner, source):
        self.config, self.run, self.directory = config, run, Path(directory)

    def prepare(self):
        FakeProvider.calls.append('prepare')
        if self.config['target'] == 'azure': (self.directory / 'apply.tfplan').write_bytes(b'saved plan')

    def provision(self):
        FakeProvider.calls.append('provision')
        if FakeProvider.failure: raise FakeProvider.failure
        if self.config['target'] == 'existing':
            return {'host': self.config['existing']['host'], 'created_by_nomiarch': False, 'deletable_by_nomiarch': False}
        return {'host': self.run['prefix']}

    def verify(self): return {'management': 'ok'}

    def remote(self, args, **kwargs):
        FakeProvider.calls.append('remote')
        source = str(args[-1])
        if 'meminfo' in source: return json.dumps(FakeProvider.facts)
        if 'getaddrinfo' in source: return json.dumps(FakeProvider.probe)
        return ''

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
        self.root = Path(self.temp.name)

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
        self.root = Path(self.temp.name); self.state = self.root / 'state'
        FakeProvider.calls, FakeProvider.failure = [], None
        FakeProvider.facts, FakeProvider.probe = dict(FACTS), {'ipv4': 'blocked', 'ipv6': 'blocked', 'public_dns': 'unresolvable'}
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
            return {'total_count': len(self.check_runs), 'check_runs': self.check_runs}
        return super().call(path, *args)

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
