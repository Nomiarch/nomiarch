import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from nomiarch.common import NomiarchError, Runner, atomic_write, digest, file_lock
from nomiarch.desktop import service


class DesktopTests(unittest.TestCase):
    def test_download_rejects_tampering_and_removes_partial_file(self):
        class Response(io.BytesIO):
            url='https://example.test/file'
            headers={'Content-Length':'3'}
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen',return_value=Response(b'bad')):
            path=Path(d)/'bundle'
            with self.assertRaises(NomiarchError):service.download('https://example.test/file',path,'0'*64)
            self.assertFalse(path.exists());self.assertFalse(path.with_name('bundle.part').exists())

    def test_offline_kit_never_downloads_missing_files(self):
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen') as network:
            with self.assertRaises(NomiarchError):service.prepare_kit(d,'amd64',False)
            network.assert_not_called()

    def test_verified_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen') as network:
            path=Path(d)/'cached';path.write_bytes(b'valid')
            self.assertEqual(service.download('https://example.test/file',path,digest(path)),path)
            network.assert_not_called()

    def test_atomic_write_and_cross_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';atomic_write(p,'first');atomic_write(p,'second')
            self.assertEqual(p.read_text(),'second')
            lock=Path(d)/'controller.lock'
            with file_lock(lock):
                code='from nomiarch.common import file_lock; import sys\nwith file_lock(sys.argv[1], blocking=False): pass'
                result=subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True)
                self.assertNotEqual(result.returncode,0)
            with file_lock(lock,blocking=False):pass

    def test_process_timeout(self):
        with self.assertRaises(NomiarchError):Runner().run([sys.executable,'-c','import time; time.sleep(15)'],timeout=.2)

    def test_recovery_key_is_accepted_by_age_and_not_overwritten(self):
        if not shutil.which('age-keygen'):
            self.skipTest('age-keygen required for independent identity interoperability')
        try:import bech32
        except ImportError:self.skipTest('desktop dependencies required')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'identity.txt'
            recipient=service.recovery_key(path)
            result=subprocess.check_output(['age-keygen','-y',str(path)],text=True).strip()
            self.assertEqual(result,recipient)
            with self.assertRaises(FileExistsError):service.recovery_key(path)
