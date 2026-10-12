"""Drive the desktop infrastructure setup flow headlessly with a tkinter stand-in.

These tests exercise screen logic, validation messages and the controller calls the
screens make. They do not render widgets; native package CI renders the real screens.
"""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tk_stub
from nomiarch.common import digest, read_json
from nomiarch.foundation import scaffold
import test_infrastructure as infrastructure_tests

ROOT = Path(__file__).resolve().parents[1]


class FakeGitHub:
    created, proposed = [], []
    def __init__(self, token, repository=None):
        assert token == 'memory-only-token'
        self.repository = repository
    def create_repository(self, value): FakeGitHub.created.append(value['id']); return 'https://example.invalid/customer/infrastructure'
    def propose(self, value, files, title, body):
        FakeGitHub.proposed.append((title, sorted(files)))
        return {'url': 'https://example.invalid/pull/7', 'number': 7, 'head': 'a' * 40, 'base': 'b' * 40}
    def approved(self, value, snapshot, number):
        assert number == 7
        return {'server': 'https://example.invalid', 'merge': 'c' * 40, 'head': 'a' * 40, 'human_reviewers': ['reviewer'],
                'validation': {'check': 'nomiarch-validate', 'head_sha': 'a' * 40}}


class SetupScreenTests(unittest.TestCase):
    def setUp(self):
        self.dialogs = tk_stub.install(); self.addCleanup(tk_stub.remove, self.dialogs)
        self.addCleanup(patch.stopall)
        from nomiarch.desktop import app as desktop_app
        self.desktop_app = desktop_app
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # canonical, like production paths
        self.image = self.root / 'ubuntu.img'; self.image.write_bytes(b'admitted image')
        pins = copy.deepcopy(scaffold.PINS); pins['amd64']['image'] = ('ubuntu.img', digest(self.image))
        patch('nomiarch.foundation.scaffold.PINS', pins).start()
        patch('nomiarch.desktop.service.PINS', pins).start()
        patch('nomiarch.desktop.setup_flow.PINS', pins).start()
        patch('nomiarch.desktop.foundation.shutil.which', return_value='/usr/bin/ssh').start()
        patch('nomiarch.desktop.service.architecture', return_value='amd64').start()
        patch('nomiarch.desktop.service.preflight', return_value='VM support is ready').start()
        patch('nomiarch.desktop.service.locate_multipass', return_value='/usr/bin/multipass').start()
        patch('nomiarch.desktop.service.open_folder').start()
        patch('nomiarch.desktop.foundation.webbrowser.open').start()
        patch('nomiarch.desktop.foundation.GitHub', FakeGitHub).start()
        patch('nomiarch.bootstrap.controller.install_remote', side_effect=AssertionError('no application install')).start()
        patch('nomiarch.foundation.infrastructure.network.configure', return_value={'guest_egress': 'blocked'}).start()
        patch('nomiarch.foundation.infrastructure.network.verify', return_value={'guest_egress': 'blocked'}).start()
        from nomiarch.foundation import deployment
        real_prepare, real_apply, real_verify = deployment.prepare, deployment.apply, deployment.verify_infrastructure
        patch('nomiarch.desktop.foundation.deployment.prepare', side_effect=lambda *a, **k: real_prepare(*a, factory=infrastructure_tests.FakeProvider, **k)).start()
        patch('nomiarch.desktop.foundation.deployment.apply', side_effect=lambda *a, **k: real_apply(*a, factory=infrastructure_tests.FakeProvider, **k)).start()
        patch('nomiarch.foundation.deployment.verify_infrastructure', side_effect=lambda *a, **k: real_verify(*a, factory=infrastructure_tests.FakeProvider, **k)).start()
        infrastructure_tests.FakeProvider.calls, infrastructure_tests.FakeProvider.failure = [], None
        infrastructure_tests.FakeProvider.facts = dict(infrastructure_tests.FACTS)
        FakeGitHub.created, FakeGitHub.proposed = [], []
        tk_stub.Widget.registry.clear()
        self.app = desktop_app.App(self.root / 'data')
        self.app.start = self.run_now  # synchronous stand-in for the worker thread
        self.app.github_token.set('memory-only-token')

    def run_now(self, task, done, cancellable=False):
        # Mirror App.start: failures become the "needs your attention" dialog, never a crash.
        try: result = task()
        except Exception as error: self.dialogs.showerror('Nomiarch needs your attention', str(error)); return
        try: done(result)
        except Exception as error: self.dialogs.showerror('Nomiarch needs your attention', str(error))

    def labels(self):
        return [w.options.get('text', '') for w in tk_stub.Widget.registry if 'text' in w.options]

    def buttons(self):
        return {w.options['text']: w for w in tk_stub.Widget.registry if w.__class__.__name__ == 'Button' and 'text' in w.options and w.master is not None and w in w.master.children}

    def test_home_offers_infrastructure_first_and_keeps_core_preview_paths(self):
        names = list(self.buttons())
        self.assertTrue(names[0].startswith('Set up infrastructure'))
        self.assertTrue(any('Core (preview)' in n for n in names))

    def choose_destination(self, location, git='github-enterprise', mode='fully-disconnected'):
        app = self.app
        app.infrastructure_home()
        app.organisation.set('customer'); app.environment.set('site'); app.location.set(location); app.location_changed()
        app.location_continue()
        app.git_service.set(git); app.git_service_changed()
        app.repo_owner.set('customer'); app.repo_name.set('infrastructure'); app.approvers.set('reviewer')
        if git == 'github-enterprise': app.server_url.set('https://git.company.internal')
        app.git_service_continue()
        app.github_token.set('memory-only-token')
        app.network_mode.set(mode)
        app.network_mode_continue()

    def test_incompatible_disconnected_choices_are_explained_and_block_progress(self):
        self.choose_destination('cloud', 'github', 'fully-disconnected')
        self.assertEqual(self.dialogs.errors[-1][0], 'Incompatible choices')
        self.assertIn('Azure', self.dialogs.errors[-1][1]); self.assertIn('github.com', self.dialogs.errors[-1][1])
        self.assertEqual(self.app.network_mode.get(), 'fully-disconnected')  # never downgraded for the user
        self.assertIsNone(self.app.project_path)

    def test_cloud_shows_azure_only_and_on_premises_explains_no_vm_creation(self):
        self.app.infrastructure_home(); self.app.location.set('cloud'); self.app.location_changed()
        texts = ' '.join(self.labels())
        self.assertIn('Azure is the only implemented provider', texts)
        self.app.location.set('on-premises'); self.app.location_changed()
        self.assertIn('does not create the VM', ' '.join(self.labels()))

    def test_on_premises_disconnected_flow_reaches_infrastructure_ready_without_application(self):
        key = self.root / 'key'; key.write_bytes(b'private'); key.chmod(0o600)
        hosts = self.root / 'known_hosts'; hosts.write_text('host.site.internal ssh-ed25519 AAAA\n')
        self.choose_destination('on-premises')
        app = self.app
        self.assertIn('Host name or private IP', ' '.join(self.labels()))
        self.assertNotIn('Azure VM size', ' '.join(self.labels()))
        app.name.set('site-host'); app.cpus.set('4'); app.memory.set('16'); app.disk.set('100')
        app.existing_host.set('host.site.internal'); app.existing_user.set('nomiarch')
        app.existing_key.set(str(key)); app.existing_known_hosts.set(str(hosts))
        app.infrastructure_settings_continue()
        self.assertIn('adopts an existing host; no VM is created', ' '.join(self.labels()))
        app.make_infrastructure_scaffold()
        value, _ = scaffold.load_supported(app.project_path, ROOT)
        self.assertEqual(value['scope'], 'infrastructure'); self.assertEqual(value['isolation'], 'disconnected')
        self.assertEqual(app.infrastructure_paths, {'ssh_key': str(key), 'known_hosts': str(hosts)})
        self.assertIn('Prepare deployment plan', self.buttons())
        app.create_customer_repo(); self.assertEqual(FakeGitHub.created, [value['id']])
        app.open_initial_pr()
        self.assertEqual(FakeGitHub.proposed[0][0], 'Establish Nomiarch infrastructure foundation')
        self.assertIn('.github/workflows/nomiarch-validate.yml', FakeGitHub.proposed[0][1])
        self.assertEqual(app.pull_number.get(), '7')
        app.prepare_foundation()
        self.assertEqual([c for c in infrastructure_tests.FakeProvider.calls if c != 'remote'], ['prepare'])
        self.assertIn('Approve this plan and establish the infrastructure', self.buttons())
        self.assertIn('Adopt the dedicated host', ' '.join(self.labels()))
        app.apply_foundation()
        self.assertIn('provision', infrastructure_tests.FakeProvider.calls)
        self.assertIn('Infrastructure ready', ' '.join(self.labels()))
        self.assertIn('Application: not-installed', ' '.join(self.labels()))
        run_dir = Path(app.pending_foundation['run_directory'])
        run = read_json(run_dir / 'run.json')
        self.assertEqual(run['phase'], 'infrastructure-ready'); self.assertEqual(run['application']['status'], 'not-installed')
        self.assertEqual(run['foundation']['repository_commit'], 'c' * 40)
        # Reopening the configuration shows the existing run and offers no second plan.
        app.scaffold_ready()
        self.assertNotIn('Prepare deployment plan', self.buttons())
        self.assertIn('already has a retained run', ' '.join(self.labels()))
        # Manage: infrastructure status, read-only verification, no application maintenance, no host deletion.
        app.manage(); app.listbox.current(0)
        app.infrastructure_selected(); self.assertIn('Infrastructure status', ' '.join(self.labels()))
        app.manage(); app.listbox.current(0); app.maintenance('upgrade')
        self.assertEqual(self.dialogs.infos[-1][0], 'Infrastructure only')
        app.manage(); app.listbox.current(0); app.maintenance('verify')
        self.assertTrue((run_dir / 'verification.json').exists())
        app.manage(); app.listbox.current(0); app.maintenance('destroy')
        self.assertIn('never deleted', self.dialogs.errors[-1][1])
        self.assertNotIn('destroy', infrastructure_tests.FakeProvider.calls)
        self.dialogs.files = [str(self.root / 'backup.zip')]
        app.backup_foundation(str(run_dir))
        self.assertTrue((self.root / 'backup.zip').exists())
        self.assertEqual(self.dialogs.infos[-1][0], 'Backup saved')

    def test_local_flow_admits_image_only_and_resume_detects_existing_plan(self):
        self.choose_destination('local', 'github', 'restricted-connected')
        app = self.app
        app.name.set('local-vm'); app.cpus.set('2'); app.memory.set('8'); app.disk.set('40')
        app.infrastructure_settings_continue()
        self.assertEqual(app.flow, 'infrastructure')
        app.kit.set(str(self.root)); app.mode.set('offline')
        with patch('nomiarch.desktop.service.prepare_kit', side_effect=AssertionError('Core kit must not be used')), \
             patch('nomiarch.desktop.app.bundle.inspect', side_effect=AssertionError('bundle must not be inspected')), \
             patch('nomiarch.desktop.service.platform.system', return_value='Linux'):
            app.prepare()
        self.assertEqual(app.paths, {'image': str(self.image)})
        self.assertIn('Review before creating the configuration', ' '.join(self.labels()))
        app.make_infrastructure_scaffold()
        self.assertEqual(app.infrastructure_paths, {'image': str(self.image)})
        app.pull_number.set('7'); app.prepare_foundation()
        self.assertIn('Create one retained Multipass VM', ' '.join(self.labels()))
        folder = app.project_path
        app.home()
        self.dialogs.files = [str(folder)]
        with patch('nomiarch.desktop.service.platform.system', return_value='Linux'):
            app.resume_foundation()
            self.assertEqual(app.flow, 'infrastructure'); self.assertEqual(app.location.get(), 'local')
            app.kit.set(str(self.root)); app.mode.set('offline'); app.prepare()
        # The closed-and-reopened setup offers the saved plan, not a second plan.
        self.assertIn('A saved plan is waiting for your approval', ' '.join(self.labels()))
        self.assertNotIn('Prepare deployment plan', self.buttons())
        self.assertEqual(app.github_token.get(), '')  # reopening never carries a token over
        app.github_token.set('memory-only-token'); app.pull_number.set('7')
        self.buttons()['Review the saved plan'].invoke()
        self.assertIn('Approve this plan and establish the infrastructure', self.buttons())
        app.apply_foundation()
        self.assertIn('Infrastructure ready', ' '.join(self.labels()))
        self.assertEqual(infrastructure_tests.FakeProvider.calls.count('provision'), 1)
        app.scaffold_ready()
        self.assertNotIn('Review the saved plan', self.buttons()); self.assertNotIn('Prepare deployment plan', self.buttons())

    def test_expired_unused_plan_is_retired_from_setup_and_replaced(self):
        self.choose_destination('local', 'github', 'restricted-connected')
        app = self.app
        app.cpus.set('2'); app.memory.set('8'); app.disk.set('40'); app.infrastructure_settings_continue()
        app.kit.set(str(self.root)); app.mode.set('offline')
        with patch('nomiarch.desktop.service.platform.system', return_value='Linux'): app.prepare()
        app.make_infrastructure_scaffold(); app.pull_number.set('7'); app.prepare_foundation()
        old = dict(app.pending_foundation)
        expires = old['plan']['expires']
        with patch('nomiarch.foundation.deployment.time.time', return_value=expires + 5):
            app.scaffold_ready()
            self.assertIn('expired and was never used', ' '.join(self.labels()))
            self.assertNotIn('Review the saved plan', self.buttons())
            self.buttons()['Retire this unused plan'].invoke()
            self.assertTrue((Path(old['run_directory']) / 'retired.json').exists())
            self.assertIn('Prepare deployment plan', self.buttons())
        app.prepare_foundation()
        self.assertNotEqual(app.pending_foundation['run_directory'], old['run_directory'])
        app.apply_foundation()
        self.assertEqual(infrastructure_tests.FakeProvider.calls.count('provision'), 1)
        self.assertEqual(read_json(Path(old['run_directory']) / 'run.json')['phase'], 'retired')

    def test_missing_offline_image_stops_the_local_flow_without_download(self):
        self.choose_destination('local', 'github-enterprise', 'fully-disconnected')
        app = self.app
        app.cpus.set('2'); app.memory.set('8'); app.disk.set('40'); app.infrastructure_settings_continue()
        empty = self.root / 'empty'; empty.mkdir()
        app.kit.set(str(empty)); app.mode.set('offline')
        with patch('urllib.request.urlopen') as network, patch('nomiarch.desktop.service.platform.system', return_value='Linux'):
            app.prepare()
            network.assert_not_called()
        self.assertIn('no download was attempted', self.dialogs.errors[-1][1])
        self.assertIsNone(app.paths)

    def test_self_test_screens_render_in_order(self):
        app = self.app
        app.target = 'existing'; app.location.set('on-premises'); app.provider.set('existing-host')
        for screen in (app.infrastructure_home, app.git_service_screen, app.network_mode_screen, app.infrastructure_settings_screen, app.manage, app.home):
            screen(); app.set_buttons(True); app.set_buttons(False)
        self.assertEqual(self.dialogs.errors, [])


if __name__ == '__main__': unittest.main()
