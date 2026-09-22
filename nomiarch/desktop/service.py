import ctypes
import base64
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request

from nomiarch.common import NomiarchError, Runner, digest, private_dir
from .catalog import BASE, IMAGE_BASE, PINS, PREREQUISITES


def architecture():
    value = {'AMD64':'amd64','x86_64':'amd64','arm64':'arm64','aarch64':'arm64'}.get(platform.machine())
    if not value or (os.name == 'nt' and value != 'amd64'):
        raise NomiarchError('This preview supports Intel/AMD Windows and Linux/macOS on Intel/AMD or ARM.')
    return value


def data_root():
    if os.name == 'nt':
        base = Path(os.environ['LOCALAPPDATA']) / 'Nomiarch'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'Nomiarch'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'nomiarch'
    return private_dir(base)


def locate_multipass():
    candidates = [shutil.which('multipass')]
    if os.name == 'nt':
        candidates += [str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Multipass/bin/multipass.exe')]
    else:
        candidates += ['/usr/local/bin/multipass', '/snap/bin/multipass']
    for value in candidates:
        if value and Path(value).is_file():
            os.environ['PATH'] = str(Path(value).parent) + os.pathsep + os.environ.get('PATH', '')
            return value
    raise NomiarchError('VM support is missing. Choose Install VM support, then Check again.')


def preflight():
    architecture()
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion') as key:
            edition = winreg.QueryValueEx(key, 'EditionID')[0]
            build = int(winreg.QueryValueEx(key, 'CurrentBuildNumber')[0])
        if build < 22000 or not any(x in edition for x in ('Professional', 'Enterprise', 'Education')):
            raise NomiarchError('This preview needs Windows 11 Pro, Enterprise or Education (Intel/AMD). Windows Home and ARM Windows are not yet supported.')
        runner = Runner()
        status = runner.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                             "(Get-Service vmms -ErrorAction SilentlyContinue).Status"], check=False, timeout=20)
        if 'Running' not in status:
            raise NomiarchError('Hyper-V is not running. Choose Enable Hyper-V, approve Windows setup, restart if asked, then reopen Nomiarch.')
    tool = locate_multipass()
    Runner().run([tool, 'list', '--format', 'json'], timeout=30)
    return 'VM support is ready. Your existing VMs will not be changed.'


def download(url, destination, expected, progress=lambda *args: None, cancel=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and digest(destination) == expected:
        progress(destination.name, 1, 1)
        return destination
    if not url.startswith('https://'):
        raise NomiarchError('Downloads require HTTPS')
    part = destination.with_name(destination.name + '.part')
    try:
        req = urllib.request.Request(url, headers={'User-Agent':'Nomiarch-Setup/0.1'})
        with urllib.request.urlopen(req, timeout=45) as response, part.open('wb') as output:
            if not response.url.startswith('https://'):
                raise NomiarchError('Insecure download redirect')
            total = int(response.headers.get('Content-Length', 0))
            received, h = 0, hashlib.sha256()
            while True:
                if cancel and cancel.is_set():
                    raise NomiarchError('Download cancelled. Verified completed files are kept for your next attempt.')
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > 5 * 1024**3:
                    raise NomiarchError('Download exceeds the admitted size limit')
                output.write(block)
                h.update(block)
                progress(destination.name, received, total)
        if h.hexdigest() != expected:
            raise NomiarchError('Downloaded file failed verification. No installation was attempted.')
        os.replace(part, destination)
        return destination
    finally:
        part.unlink(missing_ok=True)


def prepare_kit(folder, arch, online, progress=lambda *args: None, cancel=None):
    paths = {}
    for role, (name, sha) in PINS[arch].items():
        path = Path(folder) / name
        if online:
            base = IMAGE_BASE if role == 'image' else BASE
            download(base + name, path, sha, progress, cancel)
        elif not path.is_file() or digest(path) != sha:
            raise NomiarchError(f'Offline kit needs the verified file {name}. Use Download kit on a connected computer first.')
        paths[role] = str(path.resolve())
    return paths


def install_vm_support(folder, progress):
    entry = PREREQUISITES.get(platform.system())
    if not entry:
        raise NomiarchError('On Linux, install Multipass through your approved software manager, then choose Check again.')
    url, sha, filename = entry
    path = download(url, Path(folder) / filename, sha, progress)
    if os.name == 'nt':
        # The pinned Canonical installer is opened through Windows Installer/UAC.
        code = ctypes.windll.shell32.ShellExecuteW(None, 'open', str(path), None, None, 1)
        if code <= 32:
            raise NomiarchError('Windows could not open the VM support installer')
    else:
        Runner().run(['open', str(path)], timeout=30)
    return 'Finish the Canonical installer, then choose Check again. Restart if Windows asks.'


def enable_hyperv():
    if os.name != 'nt':
        raise NomiarchError('Hyper-V setup is only available on Windows')
    script = "Add-Type -AssemblyName System.Windows.Forms; try { $ErrorActionPreference='Stop'; Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All -NoRestart | Out-Null; [System.Windows.Forms.MessageBox]::Show('Hyper-V is enabled. Restart Windows, then reopen Nomiarch.','Nomiarch setup') } catch { [System.Windows.Forms.MessageBox]::Show($_.Exception.Message,'Windows setup failed') }"
    args = '-NoProfile -WindowStyle Hidden -EncodedCommand ' + base64.b64encode(script.encode('utf-16-le')).decode()
    code = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'powershell.exe', args, None, 1)
    if code <= 32:
        raise NomiarchError('Windows setup was cancelled or could not be opened')


def recovery_key(destination):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    from bech32 import bech32_encode, convertbits
    secret = X25519PrivateKey.generate()
    raw = secret.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = secret.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    recipient = bech32_encode('age', convertbits(public, 8, 5))
    identity = bech32_encode('age-secret-key-', convertbits(raw, 8, 5)).upper()
    # Never overwrite an existing recovery identity.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write('# Nomiarch recovery key — keep private and separate from backups\n' + identity + '\n')
    return recipient
