"""Internal approval transport: actual TLS plus fail-closed controller boundaries."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from nomiarch.common import NomiarchError, read_json
from nomiarch.foundation import deployment, changes, maintenance, scaffold
from nomiarch.foundation.repository import GitHub
from nomiarch.foundation.repository_transport import certificate_context, private_address, private_connection, server_origin
import test_foundation as foundation_tests

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = {'mode': 'github-enterprise', 'server_url': 'https://git.company.internal',
            'owner': 'customer', 'name': 'foundation', 'approvers': ['reviewer']}


class InternalSettingsTests(unittest.TestCase):
    def test_existing_local_and_azure_scaffolds_still_match_their_reviewed_bytes(self):
        for fixture in read_json(ROOT / 'tests/fixtures/foundation-v02.json'):
            files = scaffold.render(fixture['project'], ROOT)
            self.assertEqual({k: hashlib.sha256(v.encode()).hexdigest() for k, v in files.items()}, fixture['files'])

    def test_disconnected_organisation_uses_internal_review_and_preserves_trust(self):
        value = scaffold.project(foundation_tests.LOCAL, 'customer', 'evaluation', 'organisation', 'disconnected', INTERNAL)
        files = scaffold.render(value, ROOT)
        self.assertIn('.github/CODEOWNERS', files)
        self.assertIn(INTERNAL['server_url'], files['README.md'])
        value['recipe_version'] = '0.2.0'
        with self.assertRaises(NomiarchError): scaffold.validate_project(value)
        bad = dict(INTERNAL, mode='github')
        with self.assertRaises(NomiarchError): scaffold.validate_repository(bad, 'organisation', 'local-isolated')

    def test_server_address_rejects_credentials_public_services_and_ambiguous_urls(self):
        for url in ('http://git.internal', 'https://user:secret@git.internal', 'https://@git.internal',
                    'https://git.internal/api/v3', 'https://git.internal?x', 'https://git.internal#x',
                    'https://github.com', 'https://tenant.ghe.com', 'https://api.github.com',
                    'https://8.8.8.8', 'https://169.254.169.254', 'https://[fe80::1]',
                    'https://git.internal\\@elsewhere', 'https://git.internal:99999', 'https://git.internal\n'):
            with self.subTest(url=url), self.assertRaises(NomiarchError): server_origin(url)
        self.assertEqual(server_origin('https://Git.Company.Internal:443/'), INTERNAL['server_url'])
        self.assertEqual(server_origin('https://[fd00::1]:8443'), 'https://[fd00::1]:8443')

    def test_ca_input_never_accepts_private_key_or_invalid_certificate(self):
        for text in ('', '-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----',
                     '-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----', 'a' * 65537):
            with self.assertRaises(NomiarchError): certificate_context(text)

    def test_dns_rejects_every_public_or_mixed_answer_before_opening_a_socket(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 443))
        for ip in ('8.8.8.8', '169.254.169.254', '192.0.2.1', '100.64.0.1', '::ffff:8.8.8.8', 'fe80::1'):
            public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
            with patch('socket.getaddrinfo', return_value=[private, public]), patch('socket.socket') as connection:
                with self.assertRaises(NomiarchError): private_connection(('git.internal', 443))
                connection.assert_not_called()

    def test_connect_uses_the_single_checked_dns_answer(self):
        answer = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.1.2.3', 8443))
        with patch('socket.getaddrinfo', return_value=[answer]) as dns, patch('socket.socket') as constructor:
            self.assertIs(private_connection(('git.internal', 8443), 5), constructor.return_value)
            dns.assert_called_once()
            constructor.return_value.connect.assert_called_once_with(('10.1.2.3', 8443))
        self.assertTrue(private_address('::ffff:10.1.2.3'))
        self.assertTrue(private_address('fd00::1'))

    def test_credentials_are_bound_to_declared_server_trust_and_repository(self):
        value = scaffold.project(foundation_tests.LOCAL, 'customer', 'evaluation', 'organisation', 'disconnected', INTERNAL)
        for client in (GitHub('memory-only-token'), GitHub('memory-only-token', INTERNAL)):
            for change in ({'server_url': 'https://another.internal'}, {'name': 'another'}, {'approvers': ['someone']}):
                changed = copy.deepcopy(value); changed['repository'].update(change)
                client.call = Mock()
                with self.assertRaises(NomiarchError): client.create_repository(changed)
                client.call.assert_not_called()
        client = GitHub('memory-only-token', INTERNAL)
        self.assertEqual(client.repository_url(value), INTERNAL['server_url'] + '/customer/foundation')


class InternalReviewTests(foundation_tests.RepositoryReviewTests):
    """Run the same stale/bot/dismissal/content/protection tests for internal Git."""
    def setUp(self):
        super().setUp()
        self.value['repository'] = dict(INTERNAL)
        self.value['isolation'] = 'disconnected'
        self.client = GitHub('test-memory-token', INTERNAL)
        self.client.call = self.call


class InternalControllerTests(foundation_tests.FoundationTests):
    """Exercise all existing lifecycle boundaries with an internal organisation."""
    def plan(self, organisation=False):
        original = scaffold.project
        def internal_project(*args, **kwargs):
            kwargs.update(repository=dict(INTERNAL), isolation='disconnected')
            return original(*args, **kwargs)
        with patch('nomiarch.foundation.scaffold.project', side_effect=internal_project):
            return super().plan(organisation=True)

    # Inherited tests use local approval for ordinary installs. Supply repository
    # proof there, but retain dedicated rejection tests below for all apply paths.
    def setUp(self):
        super().setUp()
        self.proof = {'server': INTERNAL['server_url'], 'merge': 'b'*40, 'human_reviewers': ['reviewer']}
        self.real_apply = deployment.apply
        def initial(*args, **kwargs):
            if kwargs.get('repository_check') is None: kwargs['repository_check'] = lambda *args: self.proof
            return self.real_apply(*args, **kwargs)
        self.addCleanup(patch.stopall)
        patch('nomiarch.foundation.deployment.apply', side_effect=initial).start()

    def test_organisation_requires_current_external_review(self):
        directory, sha = self.plan()
        with self.assertRaises(NomiarchError): self.real_apply(directory, ROOT, sha, factory=foundation_tests.FakeProvider)
        self.assertNotIn('provision', foundation_tests.FakeProvider.calls)
        deployment.apply(directory, ROOT, sha, factory=foundation_tests.FakeProvider)
        evidence = read_json(directory / 'approval.json')
        self.assertEqual(evidence['repository']['server'], INTERNAL['server_url'])

    def test_repair_requires_new_exact_approval_and_is_single_use(self):
        _, planned = self.repair_plan()
        args = (planned['change_directory'], ROOT, planned['plan']['digest'])
        with self.assertRaises(NomiarchError): changes.apply(*args, factory=foundation_tests.FakeProvider)
        self.assertFalse(self.observed['outbound_blocked'])
        changes.apply(*args, repository_check=lambda *args: self.proof, factory=foundation_tests.FakeProvider)
        self.assertTrue(self.observed['outbound_blocked'])
        with self.assertRaises(NomiarchError): changes.apply(*args, repository_check=lambda *args: self.proof, factory=foundation_tests.FakeProvider)

    def test_failed_pre_upgrade_backup_never_starts_release_change(self):
        _, result = self.operation_plan('upgrade')
        args = (result['operation_directory'], ROOT, result['plan']['digest'])
        with self.assertRaises(NomiarchError): maintenance.apply(*args, factory=foundation_tests.FakeProvider)
        with patch('nomiarch.foundation.maintenance.controller.backup_remote', side_effect=NomiarchError('backup failed')):
            with patch('nomiarch.foundation.maintenance.controller.install_remote') as install:
                with self.assertRaises(NomiarchError): maintenance.apply(*args, repository_check=lambda *args: self.proof, factory=foundation_tests.FakeProvider)
                install.assert_not_called()

    def test_repair_operation_updates_baseline_and_cannot_be_replayed(self):
        directory, result = self.operation_plan()
        args = (result['operation_directory'], ROOT, result['plan']['digest'])
        with self.assertRaises(NomiarchError): maintenance.apply(*args, factory=foundation_tests.FakeProvider)
        maintenance.apply(*args, repository_check=lambda *args: self.proof, factory=foundation_tests.FakeProvider)
        self.assertEqual(read_json(directory / 'run.json')['foundation']['snapshot'], result['plan']['snapshot'])
        with self.assertRaises(NomiarchError): maintenance.apply(*args, repository_check=lambda *args: self.proof, factory=foundation_tests.FakeProvider)


def tls_material(folder, prefix):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    key = ec.generate_private_key(ec.SECP256R1())
    authority = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test CA ' + prefix)])
    now = datetime.now(timezone.utc)
    def builder(subject, public, is_ca):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(authority).public_key(public)
                .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=is_ca, path_length=0 if is_ca else None), critical=True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, is_ca, is_ca, False, False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False))
    ca = builder(authority, key.public_key(), True).sign(key, hashes.SHA256())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]), leaf_key.public_key(), False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    certificate, private = folder / (prefix+'.crt'), folder / (prefix+'.key')
    certificate.write_bytes(leaf.public_bytes(pem))
    private.write_bytes(leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca.public_bytes(pem).decode(), certificate, private


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'TLS tests require the pinned desktop cryptography dependency')
class InternalTLSTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.ca, cert, key = tls_material(self.folder, 'internal')
        self.seen = []
        self.respond = lambda path: (200, {}, {'ok': True})
        case = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                case.seen.append((self.path, self.headers.get('Authorization')))
                code, headers, body = case.respond(self.path)
                self.send_response(code)
                for name, value in headers.items(): self.send_header(name, value)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5); self.addCleanup(server.shutdown)
        self.repository = dict(INTERNAL, server_url='https://localhost:' + str(server.server_port), ca_certificate=self.ca)
        self.client = GitHub('memory-only-test-token', self.repository)

    def test_trusted_https_uses_private_api_and_ignores_environment_proxies(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://192.0.2.1:1234', 'https_proxy': 'http://192.0.2.1:1234'}):
            self.assertEqual(self.client.call('/user'), {'ok': True})
        self.assertEqual(self.seen, [('/api/v3/user', 'Bearer memory-only-test-token')])

    def test_wrong_ca_and_hostname_fail_before_any_credentials_are_sent(self):
        wrong_ca, _, _ = tls_material(self.folder, 'wrong')
        for repository in (dict(self.repository, ca_certificate=wrong_ca),
                           dict(self.repository, server_url=self.repository['server_url'].replace('localhost', '127.0.0.1'))):
            with self.assertRaises(NomiarchError): GitHub('memory-only-test-token', repository).call('/user')
        self.assertEqual(self.seen, [])

    def test_redirect_cannot_forward_token_and_server_errors_do_not_echo_secrets(self):
        self.respond = lambda path: (302, {'Location': 'https://public.example.invalid/steal'}, {})
        with self.assertRaisesRegex(NomiarchError, 'redirect refused'): self.client.call('/user')
        self.assertEqual(len(self.seen), 1)
        self.respond = lambda path: (403, {}, {'error': 'memory-only-test-token'})
        with self.assertRaises(NomiarchError) as caught: self.client.call('/user')
        self.assertNotIn('memory-only-test-token', str(caught.exception))

    def test_review_of_actual_scaffold_over_tls_records_authority_and_blocks_stale_review(self):
        value = scaffold.project(foundation_tests.LOCAL, 'customer', 'evaluation', 'organisation', 'disconnected', self.repository)
        files = scaffold.render(value, ROOT)
        self.assertEqual(files['trust/repository-ca.crt'], self.ca)
        self.assertNotIn('memory-only-test-token', ''.join(files.values()))
        snapshot = {'files': {k: hashlib.sha256(v.encode()).hexdigest() for k,v in files.items()}}
        fixture = foundation_tests.RepositoryReviewTests(); fixture.setUp()
        entries = {str(i): (name, content) for i,(name,content) in enumerate(files.items())}
        def respond(path):
            path = path.removeprefix('/api/v3')
            if '/git/trees/' in path:
                body = {'tree': [{'path': name, 'sha': sha, 'type': 'blob', 'mode': '100644'} for sha,(name,_) in entries.items()]}
            elif '/git/blobs/' in path:
                body = {'encoding': 'base64', 'content': base64.b64encode(entries[path.rsplit('/',1)[1]][1].encode()).decode()}
            else: body = fixture.call(path)
            return 200, {}, body
        self.respond = respond
        proof = self.client.approved(value, snapshot, 7)
        self.assertEqual(proof['server'], self.repository['server_url'])
        self.assertEqual(proof['human_reviewers'], ['reviewer'])
        fixture.reviews[0]['commit_id'] = 'c'*40
        with self.assertRaises(NomiarchError): self.client.approved(value, snapshot, 7)


if __name__ == '__main__': unittest.main()
