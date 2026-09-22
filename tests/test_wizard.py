import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from nomiarch.bootstrap.wizard import run
from nomiarch.common import NomiarchError, digest


class WizardTests(unittest.TestCase):
    def exercise(self, confirm="yes", checksum=None, architecture="amd64"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "ubuntu.img"
            image.write_bytes(b"test image")
            output = root / "env.json"
            answers = [str(image), str(image), str(image), checksum or digest(image),
                       "nomiarch-test", "", "", "", "no", "", confirm]
            with patch("builtins.input", side_effect=answers), patch("builtins.print"), \
                 patch("nomiarch.bootstrap.wizard.shutil.which", return_value="/usr/bin/tool"), \
                 patch("nomiarch.bootstrap.wizard.platform.machine", return_value="x86_64"), \
                 patch("nomiarch.bootstrap.wizard.bundle.inspect", return_value={"architecture": architecture, "version": "0.1.0.dev1"}), \
                 patch("nomiarch.bootstrap.wizard.controller.apply", return_value={"validation": {"status": "passed"}}) as apply:
                if checksum or architecture != "amd64":
                    with self.assertRaises(NomiarchError):
                        run(output, root, root, action="install")
                    apply.assert_not_called()
                    self.assertFalse(output.exists())
                else:
                    run(output, root, root, action="install")
                    if confirm == "yes":
                        apply.assert_called_once()
                        self.assertEqual(apply.call_args.args[2], "core")
                        self.assertFalse(apply.call_args.args[0]["lifecycle"]["destroy_after"])
                        self.assertTrue(output.exists())
                    else:
                        apply.assert_not_called()
                        self.assertFalse(output.exists())

    def test_installs_full_core_and_keeps_vm(self):
        self.exercise()

    def test_cancel_does_not_create_resources(self):
        self.exercise(confirm="no")

    def test_bad_image_prevents_creation(self):
        self.exercise(checksum="0" * 64)

    def test_wrong_architecture_prevents_creation(self):
        self.exercise(architecture="arm64")

class MaintenanceWizardTests(unittest.TestCase):
    def test_upgrade_selects_exact_run_and_requires_backup_recipient(self):
        from nomiarch.bootstrap.wizard import maintain
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'release.tar'
            artifact.write_bytes(b'test')
            record = {'inventory': {'host': 'vm'}, 'cleanup': {}, 'config': {'name': 'lab', 'target': 'local', 'architecture': 'amd64'}}
            seen = []
            def dispatch(args):
                seen.append(vars(args).copy())
                return record if args.action == 'status' else {'upgraded': True}
            with patch('builtins.input', side_effect=[directory, str(artifact), str(artifact), 'age1' + 'q' * 58, 'yes']), \
                 patch('builtins.print'), patch('nomiarch.bootstrap.wizard.controller.dispatch', side_effect=dispatch), \
                 patch('nomiarch.bootstrap.wizard.bundle.inspect', return_value={'version': '0.1.0.dev2', 'architecture': 'amd64', 'manifest_sha256': 'a' * 64}):
                self.assertEqual(maintain('upgrade', directory, directory), {'upgraded': True})
            self.assertEqual([x['action'] for x in seen], ['status', 'upgrade'])
            self.assertEqual(seen[-1]['run'], directory)
            self.assertTrue(seen[-1]['backup_recipient'].startswith('age1'))

    def test_cancel_verify_does_not_execute_task(self):
        from nomiarch.bootstrap.wizard import maintain
        record = {'inventory': {'host': 'vm'}, 'cleanup': {}, 'config': {'name': 'lab', 'target': 'local'}}
        with patch('builtins.input', side_effect=['/tmp/example-run', 'no']), patch('builtins.print'), \
             patch('nomiarch.bootstrap.wizard.controller.dispatch', return_value=record) as dispatch:
            self.assertIsNone(maintain('verify', '/tmp/no-state', '.'))
            dispatch.assert_called_once()

    def test_deleted_vm_cannot_be_upgraded(self):
        from nomiarch.bootstrap.wizard import maintain
        with patch('builtins.input', return_value='/tmp/deleted-run'), patch('builtins.print'), \
             patch('nomiarch.bootstrap.wizard.controller.dispatch', return_value={'inventory': {}, 'cleanup': {'status': 'deleted'}}) as dispatch:
            with self.assertRaises(NomiarchError):
                maintain('upgrade', '/tmp/no-state', '.')
            dispatch.assert_called_once()
