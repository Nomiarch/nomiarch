"""TLS regression tests: real sockets, untrusted issuers and hostname checks."""
import datetime
import http.server
import ipaddress
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nomiarch.common import digest
from nomiarch.desktop import service
from nomiarch.foundation.repository_transport import certificate_context
from nomiarch.tls import default_context


class TLSTests(unittest.TestCase):
    def test_verified_download_and_bad_certificates(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        pem = cert.public_bytes(serialization.Encoding.PEM)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'fixture')
            def log_message(self, *args): pass
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder/'cert.pem').write_bytes(pem)
            (folder/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(folder/'cert.pem', folder/'key.pem')
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            url = f'https://localhost:{server.server_port}/file'
            path = folder/'download'
            (folder/'expected').write_bytes(b'fixture')
            expected = digest(folder/'expected')
            try:
                with self.assertRaises(urllib.error.URLError):
                    service.download(url, path, expected)
                self.assertFalse(path.exists())
                self.assertFalse(path.with_name(path.name + '.part').exists())
                # Explicit GHES CA trust remains isolated and still verifies hostnames.
                trusted = certificate_context(pem.decode())
                with patch('nomiarch.desktop.service.default_context', return_value=trusted):
                    service.download(url, path, expected)
                    self.assertEqual(path.read_bytes(), b'fixture')
                    with self.assertRaises(urllib.error.URLError):
                        service.download(url.replace('localhost', '127.0.0.1'), folder/'bad-host', expected)
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_native_context_keeps_verification_enabled(self):
        context = default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_core_entry_resets_infrastructure_state(self):
        from nomiarch.desktop.foundation import FoundationScreens
        app = Mock(flow='infrastructure', paths={'image': 'old'}, resume_after_files=True)
        app.clear.side_effect = RuntimeError('stop before rendering')
        with self.assertRaisesRegex(RuntimeError, 'stop before rendering'):
            FoundationScreens.customer(app, 'local')
        self.assertEqual(app.flow, 'core')
        self.assertIsNone(app.paths)
        self.assertFalse(app.resume_after_files)

    def test_local_infrastructure_checks_vm_support_before_files(self):
        from nomiarch.desktop.infrastructure import InfrastructureScreens
        app = Mock(target='local')
        app.cpus.get.return_value = '2'; app.memory.get.return_value = '8'; app.disk.get.return_value = '40'
        app.location.get.return_value = 'local'; app.provider.get.return_value = 'multipass'
        with patch('nomiarch.desktop.service.architecture', return_value='arm64'):
            InfrastructureScreens.infrastructure_settings_continue(app)
        app.prerequisites.assert_called_once_with()
        app.download_screen.assert_not_called()
