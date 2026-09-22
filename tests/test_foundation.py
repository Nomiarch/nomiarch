import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nomiarch.common import NomiarchError, digest, read_json, write_json
from nomiarch.core.foundation import evaluate
from nomiarch.foundation import changes, deployment, maintenance, scaffold
from nomiarch.foundation.repository import GitHub


ROOT = Path(__file__).resolve().parents[1]
LOCAL = {'api_version': 'nomiarch.io/v1alpha1', 'name': 'customer-vm', 'target': 'local', 'architecture': 'amd64',
         'capacity': {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40}, 'lifecycle': {'destroy_after': False, 'max_runtime_minutes': 120},
         'local': {'image': '/private/image', 'image_sha256': scaffold.PINS['amd64']['image'][1]}}


class FakeProvider:
    calls = []
    def __init__(self, config, run, directory, runner, source): self.directory = Path(directory)
    def prepare(self):
        self.calls.append('prepare')
        (self.directory / 'apply.tfplan').write_bytes(b'exact saved plan')
    def provision(self): self.calls.append('provision'); return {'host': 'customer-vm'}
    def verify(self): return {'vm': 'running'}
    def remote(self, *args, **kwargs): return ''
    def command(self, *args, **kwargs): return ''
    def destroy(self): self.calls.append('destroy'); return {'status': 'deleted'}


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.folder = self.root / 'customer'
        self.value = scaffold.project(LOCAL, 'example', 'evaluation')

    def test_scaffold_is_deterministic_and_excludes_private_inputs(self):
        files = scaffold.render(self.value, ROOT)
        self.assertEqual(files, scaffold.render(copy.deepcopy(self.value), ROOT))
        self.assertNotIn('/private/image', ''.join(files.values()))
        scaffold.create(self.folder, self.value, ROOT)
        value, _ = scaffold.load_supported(self.folder, ROOT)
        self.assertEqual(value, self.value)
        with self.assertRaises(NomiarchError): scaffold.create(self.folder, self.value, ROOT)
        (self.folder / 'policies/approvals.json').write_text('{"allow_agent_apply": true}')
        with self.assertRaises(NomiarchError): scaffold.load_supported(self.folder, ROOT)

    def test_organisation_cannot_fall_back_to_local_approval(self):
        with self.assertRaises(NomiarchError): scaffold.project(LOCAL, 'example', 'evaluation', usage='organisation')
        repo = {'mode': 'github', 'owner': 'customer', 'name': 'foundation', 'approvers': ['human']}
        with self.assertRaises(NomiarchError): scaffold.project(LOCAL, 'example', 'evaluation', 'organisation', 'disconnected', repo)

    def test_input_validation_rejects_paths_and_executable_customer_fields(self):
        for name in ('../other', 'x; touch stolen', 'UPPER', '', '/tmp/config'):
            with self.assertRaises(NomiarchError): scaffold.project(LOCAL, name, 'evaluation')
        value = copy.deepcopy(self.value); value['configuration']['provisioner'] = 'run arbitrary code'
        with self.assertRaises(NomiarchError): scaffold.validate_project(value)

    def plan(self, organisation=False):
        FakeProvider.calls = []
        paths = {}
        pins = copy.deepcopy(scaffold.PINS)
        for role in ('bundle', 'key', 'image'):
            path = self.root / role; path.write_text('admitted-' + role)
            paths[role] = str(path)
            pins['amd64'][role] = (path.name, digest(path))
        config = copy.deepcopy(LOCAL); config['local']['image_sha256'] = pins['amd64']['image'][1]
        self.addCleanup(patch.stopall)
        patch('nomiarch.foundation.scaffold.PINS', pins).start()
        patch('nomiarch.foundation.deployment.bundle.inspect', return_value={'architecture': 'amd64', 'version': scaffold.CORE_VERSION}).start()
        patch('nomiarch.foundation.deployment.controller.install_remote', return_value={'core': 'passed'}).start()
        patch('nomiarch.foundation.deployment.network.configure', return_value={'blocked': True}).start()
        patch('nomiarch.foundation.deployment.network.verify', return_value={'blocked': True}).start()
        repo = {'mode': 'github', 'owner': 'customer', 'name': 'foundation', 'approvers': ['human']} if organisation else None
        self.value = scaffold.project(config, 'example', 'evaluation', 'organisation' if organisation else 'personal', repository=repo)
        scaffold.create(self.folder, self.value, ROOT)
        result = deployment.prepare(self.folder, self.root / 'state', ROOT, paths, factory=FakeProvider)
        return Path(result['run_directory']), result['plan']['digest']

    def test_plan_creates_no_vm_and_approval_uses_it_once_without_replanning(self):
        directory, sha = self.plan()
        self.assertEqual(FakeProvider.calls, ['prepare'])
        result = deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        self.assertEqual(result['validation']['status'], 'passed')
        self.assertEqual(FakeProvider.calls, ['prepare', 'provision'])
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)

    def test_altered_saved_plan_cannot_be_applied(self):
        directory, sha = self.plan()
        (directory / 'apply.tfplan').write_text('replace other resources')
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def test_changed_scaffold_invalidates_review_even_when_recipe_is_valid(self):
        directory, sha = self.plan()
        value = copy.deepcopy(self.value); value['configuration']['capacity']['cpus'] = 4
        for name, content in scaffold.render(value, ROOT).items(): (self.folder / name).write_text(content)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def test_forged_or_expired_plan_is_rejected_before_creation(self):
        directory, sha = self.plan()
        record = read_json(directory / 'plan.json'); record['expires'] += 360000
        write_json(directory / 'plan.json', record)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def test_reviewed_plan_expires_without_configuration_change(self):
        directory, sha = self.plan()
        expiry = read_json(directory / 'plan.json')['expires']
        with patch('nomiarch.foundation.deployment.time.time', return_value=expiry + 1):
            with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)

    def test_organisation_requires_current_external_review(self):
        directory, sha = self.plan(organisation=True)
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        def rejected(*args): raise NomiarchError('head changed')
        with self.assertRaises(NomiarchError): deployment.apply(directory, ROOT, sha, repository_check=rejected, factory=FakeProvider)
        self.assertNotIn('provision', FakeProvider.calls)

    def repair_plan(self):
        directory, sha = self.plan()
        deployment.apply(directory, ROOT, sha, factory=FakeProvider)
        run = read_json(directory / 'run.json')
        proposed = copy.deepcopy(self.value)
        proposed['reconciliation'] = {'configuration_sha256': run['foundation']['snapshot']['sha256'],
                                      'observation_sha256': 'd'*64, 'checks': ['outbound_blocked']}
        folder = self.root / 'proposal'; scaffold.create(folder, proposed, ROOT)
        self.observed = dict(proposed['configuration']['capacity'], outbound_blocked=False)
        patch('nomiarch.foundation.changes.observe.local', side_effect=lambda *args: dict(self.observed)).start()
        patch('nomiarch.foundation.changes.network.configure', side_effect=lambda *args: self.observed.update(outbound_blocked=True)).start()
        result = changes.prepare(directory, folder, ROOT, factory=FakeProvider)
        return directory, result

    def test_repair_requires_new_exact_approval_and_is_single_use(self):
        directory, planned = self.repair_plan()
        self.assertFalse(self.observed['outbound_blocked'])
        result = changes.apply(planned['change_directory'], ROOT, planned['plan']['digest'], factory=FakeProvider)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(self.observed['outbound_blocked'])
        with self.assertRaises(NomiarchError): changes.apply(planned['change_directory'], ROOT, planned['plan']['digest'], factory=FakeProvider)

    def test_new_drift_after_repair_plan_prevents_application(self):
        _, planned = self.repair_plan()
        self.observed['cpus'] = 4
        with self.assertRaises(NomiarchError): changes.apply(planned['change_directory'], ROOT, planned['plan']['digest'], factory=FakeProvider)
        self.assertFalse(self.observed['outbound_blocked'])

    def test_plan_summary_redacts_sensitive_nested_values(self):
        value = {'name': 'public', 'custom_data': 'host private key', 'items': [{'token': 'secret', 'other': 'hidden'}]}
        result = deployment.public_plan(value, {'items': [{'other': True}]})
        self.assertEqual(result['name'], 'public')
        self.assertNotIn('host private key', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result)); self.assertNotIn('hidden', json.dumps(result))

    def operation_plan(self, action='repair', organisation=False):
        directory, sha = self.plan(organisation=organisation)
        proof = (lambda *args: {'merge': 'b'*40}) if organisation else None
        deployment.apply(directory, ROOT, sha, repository_check=proof, factory=FakeProvider)
        paths = read_json(directory / 'plan.json')['artifacts']
        proposal = maintenance.propose(directory, ROOT, action, paths['bundle']['path'], paths['key']['path'])
        folder = self.root / 'operation'; scaffold.create(folder, proposal, ROOT)
        plan = maintenance.prepare(directory, folder, ROOT, paths['bundle']['path'], paths['key']['path'], 'age1'+'q'*58, factory=FakeProvider)
        return directory, plan

    def test_organisation_install_approval_cannot_authorize_removal(self):
        directory, result = self.operation_plan('destroy', organisation=True)
        with self.assertRaises(NomiarchError): maintenance.apply(result['operation_directory'], ROOT, result['plan']['digest'], factory=FakeProvider)
        self.assertNotIn('destroy', FakeProvider.calls)
        value = maintenance.apply(result['operation_directory'], ROOT, result['plan']['digest'], repository_check=lambda *args: {'merge': 'c'*40}, factory=FakeProvider)
        self.assertEqual(value['status'], 'passed')
        self.assertIn('destroy', FakeProvider.calls)
        self.assertEqual(read_json(directory / 'run.json')['phase'], 'destroyed')

    def test_failed_pre_upgrade_backup_never_starts_release_change(self):
        _, result = self.operation_plan('upgrade')
        with patch('nomiarch.foundation.maintenance.controller.backup_remote', side_effect=NomiarchError('backup failed')):
            with patch('nomiarch.foundation.maintenance.controller.install_remote') as install:
                with self.assertRaises(NomiarchError): maintenance.apply(result['operation_directory'], ROOT, result['plan']['digest'], factory=FakeProvider)
                install.assert_not_called()

    def test_repair_operation_updates_baseline_and_cannot_be_replayed(self):
        directory, result = self.operation_plan()
        maintenance.apply(result['operation_directory'], ROOT, result['plan']['digest'], factory=FakeProvider)
        self.assertEqual(read_json(directory / 'run.json')['foundation']['snapshot'], result['plan']['snapshot'])
        with self.assertRaises(NomiarchError): maintenance.apply(result['operation_directory'], ROOT, result['plan']['digest'], factory=FakeProvider)

    def test_existing_runtime_pins_survive_a_new_desktop_catalog(self):
        scaffold.create(self.folder, self.value, ROOT)
        with patch('nomiarch.foundation.scaffold.CORE_VERSION', '0.1.0.dev99'):
            loaded, _ = scaffold.load_supported(self.folder, ROOT)
        self.assertEqual(loaded['runtime'], self.value['runtime'])


class CoreObservationTests(unittest.TestCase):
    def config(self):
        desired = {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40, 'outbound_blocked': True}
        return {'kind': 'foundation', 'name': 'a'*32, 'configuration_sha256': 'b'*64, 'desired': desired, 'observed': dict(desired)}

    def test_drift_is_proposal_only_and_disk_growth_never_proposes_shrink(self):
        config = self.config(); config['observed']['outbound_blocked'] = False; config['observed']['disk_gib'] = 80
        result = evaluate(config)
        self.assertEqual([f['check'] for f in result['findings'] if f['status'] == 'fail'], ['outbound_blocked'])
        self.assertEqual(result['authority'], 'proposal-only')
        self.assertNotIn('apply', result)

    def test_partial_unknown_or_weakened_observations_are_not_safe_defaults(self):
        for mutate in (lambda c: c['observed'].pop('outbound_blocked'), lambda c: c['desired'].update(outbound_blocked=False),
                       lambda c: c['observed'].update(cpus=True), lambda c: c.update(authorize_apply=True)):
            config = self.config(); mutate(config)
            with self.assertRaises(ValueError): evaluate(config)

    def test_repository_and_release_changes_require_a_separate_review(self):
        config = self.config()
        config['desired'].update(core_version='0.1.0.dev3', repository_head='a'*40)
        config['observed'].update(core_version='0.1.0.dev2', repository_head='b'*40)
        result = evaluate(config)
        self.assertEqual({f['check'] for f in result['findings'] if f['status'] == 'fail'}, {'core_version', 'repository_head'})


class RepositoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.value = scaffold.project(LOCAL, 'example', 'evaluation', 'organisation', repository={
            'mode': 'github', 'owner': 'customer', 'name': 'foundation', 'approvers': ['reviewer']})
        self.client = GitHub('test-memory-token')
        self.head, self.merge = 'a'*40, 'b'*40
        self.files = {'foundation.json': 'reviewed content'}
        self.snapshot = {'files': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in self.files.items()}}
        self.reviews = [{'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'reviewer', 'type': 'User'}}]
        self.protection = {'enforce_admins': {'enabled': True}, 'required_pull_request_reviews': {
            'dismiss_stale_reviews': True, 'require_code_owner_reviews': True, 'required_approving_review_count': 1, 'require_last_push_approval': True}}
        self.client.call = self.call

    def call(self, path, *args):
        if path.endswith('/protection'): return self.protection
        if path.endswith('/pulls/7'): return {'merged': True, 'merge_commit_sha': self.merge,
            'base': {'ref': 'main', 'repo': {'full_name': 'customer/foundation'}},
            'head': {'sha': self.head, 'repo': {'full_name': 'customer/foundation'}}, 'user': {'login': 'proposer'}}
        if path.endswith('/git/ref/heads/main'): return {'object': {'sha': self.merge}}
        if '/git/commits/' in path: return {'tree': {'sha': 'tree'}}
        if '/git/trees/' in path: return {'tree': [{'path': 'foundation.json', 'type': 'blob', 'mode': '100644', 'sha': 'blob'}]}
        if '/git/blobs/' in path: return {'encoding': 'base64', 'content': base64.b64encode(self.files['foundation.json'].encode()).decode()}
        if '/reviews?' in path: return self.reviews
        raise AssertionError(path)

    def test_exact_review_matches_scaffold_and_separate_human(self):
        proof = self.client.approved(self.value, self.snapshot, 7)
        self.assertEqual(proof['human_reviewers'], ['reviewer'])

    def test_stale_review_bot_and_dismissal_cannot_approve(self):
        for update in ({'commit_id': 'c'*40}, {'user': {'login': 'reviewer', 'type': 'Bot'}}, {'state': 'DISMISSED'}):
            with self.subTest(update=update):
                original = copy.deepcopy(self.reviews)
                self.reviews[0].update(update)
                with self.assertRaises(NomiarchError): self.client.approved(self.value, self.snapshot, 7)
                self.reviews = original

    def test_weak_protection_and_changed_repository_content_block_apply(self):
        self.files['foundation.json'] = 'unreviewed settings'
        with self.assertRaises(NomiarchError): self.client.approved(self.value, self.snapshot, 7)
        self.protection['enforce_admins']['enabled'] = False
        with self.assertRaises(NomiarchError): self.client.assert_protection(self.value)


if __name__ == '__main__': unittest.main()
