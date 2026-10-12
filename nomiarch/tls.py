"""Verified HTTPS using native trust stores in distributed desktop builds."""
import ssl
import sys


def default_context():
    try:
        import truststore
    except ImportError:
        if getattr(sys, 'frozen', False):
            raise RuntimeError('The installer is missing its certificate trust component. Download a complete release.') from None
        context = ssl.create_default_context()
    else:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context
