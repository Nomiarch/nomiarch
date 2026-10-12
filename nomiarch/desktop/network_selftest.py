"""Opt-in network acceptance for the actual frozen executable; no credentials."""
import tempfile
import urllib.request
from pathlib import Path

from nomiarch.tls import default_context
from . import service
from .catalog import BASE, IMAGE_BASE, PINS, PREREQUISITES


def run():
    import truststore  # Mandatory in release tests, even when running from source.
    results = {}
    with tempfile.TemporaryDirectory() as folder:
        # Exercise the real downloader, GitHub asset redirects and pinned SHA256.
        for architecture, pins in PINS.items():
            name, sha = pins['key']
            service.download(BASE + name, Path(folder) / name, sha)
            results['release-key-' + architecture] = 'verified'
    urls = {'github-api': 'https://api.github.com/'}
    urls.update({'ubuntu-' + arch: IMAGE_BASE + pins['image'][0] for arch, pins in PINS.items()})
    urls.update({'vm-support-' + system: values[0] for system, values in PREREQUISITES.items()})
    for name, url in urls.items():
        request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Nomiarch-Setup-Test'})
        with urllib.request.urlopen(request, context=default_context(), timeout=45) as response:
            if response.status != 200 or not response.url.startswith('https://'):
                raise RuntimeError('HTTPS acceptance failed for ' + name)
        results[name] = 'verified'
    return results
