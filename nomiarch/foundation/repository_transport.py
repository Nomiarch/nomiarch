"""Verified HTTPS to a declared repository authority; internal hosts stay private."""
import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
import urllib.request

from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError


REVIEW_MODES = frozenset({'github', 'github-enterprise'})
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128'))


def private_address(address):
    try:
        value = ipaddress.ip_address(address)
        if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped:
            value = value.ipv4_mapped
        return any(value.version == network.version and value in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def server_origin(value):
    require(isinstance(value, str) and 1 <= len(value) <= 300
            and not any(c.isspace() or ord(c) < 32 for c in value)
            and not any(c in value for c in ('\\', '%', '?', '#')),
            'Enter the internal server HTTPS address without a path or credentials')
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
        require(parsed.scheme == 'https' and host and parsed.username is None and parsed.password is None
                and parsed.path in {'', '/'} and (port is None or 1 <= port <= 65535),
                'The internal repository requires an HTTPS server address')
        host = host.lower().rstrip('.')
        require(host.isascii(), 'Use the ASCII hostname supplied by your administrator')
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
            require(len(host) <= 253 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                    for label in host.split('.')), 'Invalid internal server hostname')
        if literal:
            require(private_address(host), 'Internal repositories require private or loopback addresses')
        require(not any(host == name or host.endswith('.' + name) for name in
                ('github.com', 'githubusercontent.com', 'githubenterprise.com', 'ghe.com')),
                'Choose your internal GitHub Enterprise Server address')
        authority = '[' + host + ']' if ':' in host else host
        return 'https://' + authority + (':' + str(port) if port and port != 443 else '')
    except (ValueError, AttributeError):
        raise NomiarchError('Invalid internal repository server address') from None


def certificate_context(certificate=None):
    if certificate is None:
        context = ssl.create_default_context()
    else:
        require(isinstance(certificate, str) and 1 <= len(certificate) <= 65536,
                'The public CA certificate file must be at most 64 KiB')
        blocks = re.findall(r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----', certificate)
        remainder = certificate
        for block in blocks:
            remainder = remainder.replace(block, '', 1)
        require(blocks and not remainder.strip(), 'Choose public PEM certificates only; private keys are not accepted')
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=certificate)
        except (ssl.SSLError, ValueError):
            raise NomiarchError('The supplied public CA certificate could not be loaded') from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def private_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once, reject mixed/public answers, and connect to the checked IP.

    TLS still verifies the original hostname. Resolving again at connect time
    would permit DNS rebinding between admission and credential transmission.
    """
    host, port = address
    answers = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    require(answers and all(private_address(answer[4][0]) for answer in answers),
            'The internal repository must resolve only to private or loopback addresses')
    last_error = None
    for family, kind, protocol, _, endpoint in answers:
        connection = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    raise last_error or OSError('Internal repository is unreachable')


class PrivateHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = private_connection


class PrivateHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PrivateHTTPSConnection, request, context=self._context)
