"""Graphical Azure sign-in with an isolated Microsoft CLI profile."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import zipfile

from nomiarch.common import NomiarchError, Runner, private_dir
from .service import download


TOFU = {
    ('Darwin', 'arm64'): ('darwin_arm64', 'e083ee43790ab9e19ad66d9933e24a7244a1412e1d5728f37999ae2163fdac95'),
    ('Darwin', 'amd64'): ('darwin_amd64', '166388e5feed47e107e11721b6366bf91d21e47eccbced75f3cbe0c7184ffd9b'),
    ('Linux', 'amd64'): ('linux_amd64', '5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8'),
    ('Windows', 'amd64'): ('windows_amd64', '0d1421721cf9ec24b41b698a9620dda218d47fa7e76ac3dc15cdbc13bd79b0bb'),
}


def packaged_helper(source):
    helper = Path(source) / 'cloud-tools' / 'az' / ('az.exe' if os.name == 'nt' else 'az')
    if helper.is_file():
        if os.name != 'nt': helper.chmod(0o700)
        return str(helper)
    return None


def tools_ready(root, source, progress=lambda *args: None):
    root, source = Path(root), Path(source)
    arch = {'x86_64': 'amd64', 'AMD64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine())
    pair = TOFU.get((platform.system(), arch))
    if not pair: raise NomiarchError('This Azure setup package does not support the controller architecture')
    binary = 'tofu.exe' if os.name == 'nt' else 'tofu'
    folder = private_dir(root / 'tools')
    # Always re-check the admitted archive before re-extracting the executable.
    filename = 'tofu_1.12.6_' + pair[0] + '.zip'
    archive = download('https://github.com/opentofu/opentofu/releases/download/v1.12.6/' + filename,
                       folder / filename, pair[1], progress)
    with zipfile.ZipFile(archive) as z:
        (folder / binary).write_bytes(z.read(binary))
    (folder / binary).chmod(0o700)
    az = packaged_helper(source) or shutil.which('az')
    if not az: raise NomiarchError('The Azure sign-in component is missing. Use the full desktop download that includes cloud support.')
    # Only the controller profile is used. Existing personal Azure profiles are
    # neither overwritten nor admitted automatically into Nomiarch's deployment.
    os.environ['AZURE_CONFIG_DIR'] = str(private_dir(root / 'azure-profile'))
    os.environ['AZURE_CORE_LOGIN_EXPERIENCE_V2'] = 'off'
    os.environ['AZURE_CORE_ENABLE_BROKER_ON_WINDOWS'] = 'false'
    os.environ['AZURE_EXTENSION_USE_DYNAMIC_INSTALL'] = 'no'
    os.environ['AZURE_CORE_COLLECT_TELEMETRY'] = 'no'
    os.environ['PATH'] = str(folder) + os.pathsep + str(Path(az).parent) + os.pathsep + os.environ.get('PATH', '')
    for name in ('ssh', 'scp'):
        if not shutil.which(name): raise NomiarchError('Enable the OpenSSH Client optional feature in Windows Settings before cloud setup')
    return az


def sign_in(root, source, progress, device_message):
    az = tools_ready(root, source, progress)
    process = subprocess.Popen([az, 'login', '--use-device-code', '--output', 'json'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    errors = []
    def read_errors():
        for raw in process.stderr:
            line = raw.decode(errors='replace').strip()
            errors.append(line)
            # Show only the expected device-code instruction, never token output.
            if re.search(r'code\s+[A-Z0-9]{6,16}', line) and ('microsoft.com/devicelogin' in line or 'aka.ms/devicelogin' in line):
                device_message(line)
    thread = threading.Thread(target=read_errors, daemon=True); thread.start()
    try:
        # Azure stdout is small subscription metadata, not get-access-token output.
        output = []
        reader = threading.Thread(target=lambda: output.append(process.stdout.read()), daemon=True)
        reader.start()
        process.wait(timeout=900); reader.join(timeout=5); thread.join(timeout=5)
        if process.returncode: raise NomiarchError('Microsoft sign-in did not complete. Retry or ask your Azure administrator about the account’s sign-in policy.')
        accounts = json.loads(b''.join(output))
        return [a for a in accounts if a.get('state') == 'Enabled']
    except subprocess.TimeoutExpired:
        process.kill(); process.wait()
        raise NomiarchError('Microsoft sign-in expired. Choose Sign in to try again.') from None


def latest_ghes_image(images):
    candidates = []
    for image in images:
        urn = image.get('urn', '')
        version = image.get('version', '')
        if urn.startswith('GitHub:') and re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', version):
            candidates.append((tuple(int(x) for x in version.split('.')), urn))
    return max(candidates)[1] if candidates else None


def account_details(subscription, location):
    run = Runner()
    run.run(['az', 'account', 'set', '--subscription', subscription])
    vnets = json.loads(run.run(['az', 'network', 'vnet', 'list', '--subscription', subscription, '--output', 'json', '--only-show-errors']))
    images = json.loads(run.run(['az', 'vm', 'image', 'list', '--publisher', 'Canonical', '--offer', 'ubuntu-24_04-lts', '--sku', 'server',
                               '--location', location, '--subscription', subscription, '--all', '--output', 'json', '--only-show-errors']))
    exact = [v for v in images if v.get('publisher') == 'Canonical' and v.get('offer') == 'ubuntu-24_04-lts' and v.get('sku') == 'server'
             and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', v.get('version', ''))]
    if not exact:
        raise NomiarchError('No admitted Ubuntu 24.04 server image was found in that Azure region')
    image = max(exact, key=lambda v: tuple(int(x) for x in v['version'].split('.')))
    ghes_images = json.loads(run.run(['az', 'vm', 'image', 'list', '--all', '--location', location, '--subscription', subscription,
                                      '--output', 'json', '--only-show-errors', '-f', 'GitHub-Enterprise']))
    return {'subnets': [{'label': v['name'] + ' / ' + s['name'], 'id': s['id']} for v in vnets if v['location'] == location
                        for s in v.get('subnets', []) if s['name'] not in {'GatewaySubnet', 'AzureBastionSubnet'}],
            'image': {k: image[k] for k in ('publisher', 'offer', 'sku', 'version')},
            'ghes_image_urn': latest_ghes_image(ghes_images)}
