"""Infrastructure bootstrap choices as plain data and checks; no Tk objects here so the
flow is unit-testable and the screens stay thin."""
import re
from pathlib import Path

from nomiarch.bootstrap.config import require
from nomiarch.common import NomiarchError
from nomiarch.foundation import destinations, scaffold
from nomiarch.foundation.repository_transport import REVIEW_MODES, server_origin
from .catalog import PINS


def location_options():
    return [(name, entry['label']) for name, entry in destinations.LOCATIONS.items()]


def provider_options(location):
    options = []
    for name in destinations.LOCATIONS[location]['providers']:
        entry = destinations.PROVIDERS[name]
        if entry['implemented']:
            options.append((name, entry['label'], entry['note']))
    return options


def git_service_options():
    return [(name, entry['label'], entry.get('note', '')) for name, entry in destinations.GIT_SERVICES.items()]


def network_mode_options():
    return [(name, entry['label']) for name, entry in destinations.NETWORK_MODES.items()]


def problems(location, provider, git_service, network_mode):
    return destinations.combination_problems(location, provider, git_service, network_mode)


def settings_needed(location, provider):
    """Which setting groups the UI shows for the selected destination; nothing else is asked."""
    target = destinations.target_for(location, provider)
    groups = ['capacity']
    if target == 'local':
        groups += ['admitted-image']
    elif target == 'existing':
        groups += ['architecture', 'ssh-host', 'ssh-key', 'known-hosts']
    else:
        groups += ['azure-sign-in', 'azure-network', 'azure-vm-size']
    return groups


def prerequisites(location, provider, git_service, network_mode):
    """Human-readable prerequisites explained before anything is created."""
    target = destinations.target_for(location, provider)
    items = ['A designated independent human reviewer who is not the person running setup',
             'Repository creation/protection permission on the chosen Git service, used once by this bootstrap and removed afterwards',
             'GitHub Actions enabled for the new repository so the nomiarch-validate check can run (hosted runners for GitHub SaaS)']
    if target == 'local':
        items.append('Multipass VM support on this computer and the admitted Ubuntu 24.04 cloud image (from the kit)')
    elif target == 'existing':
        items += ['A dedicated Ubuntu 24.04 host or VM your organisation already created (this option does not create it)',
                  'An SSH user with non-interactive sudo, its private key file, and the host key enrolled in a known_hosts file out of band']
    else:
        items += ['A scoped Azure provisioning identity with an enabled subscription',
                  'A privately routed subnet and management CIDR provided by your cloud administrator; no public SSH is opened']
    if git_service == 'github-enterprise':
        items += ['Your GitHub Enterprise Server HTTPS address, optional public CA certificate and a token issued by that server',
                  'A self-hosted validation runner labelled nomiarch-validation (Python 3.11+, no deployment credentials)']
    if network_mode == 'fully-disconnected':
        items += ['All artifacts admitted through your controlled transfer process: no download is attempted inside the boundary',
                  'Internal Git, DNS, time and reviewers inside the boundary; physical disconnection is qualified by the site, not by software']
    return items


def architecture_for(location, provider, local_architecture, selected=None):
    target = destinations.target_for(location, provider)
    if target == 'azure':
        return 'amd64'
    if target == 'local':
        return local_architecture
    require(selected in {'amd64', 'arm64'}, 'Choose the adopted host CPU architecture')
    return selected


def capacity(cpus, memory_gib, disk_gib):
    try:
        values = {'cpus': int(cpus), 'memory_gib': int(memory_gib), 'disk_gib': int(disk_gib)}
    except (TypeError, ValueError):
        raise NomiarchError('CPU, memory and storage must be whole numbers') from None
    return values


def existing_host_settings(host, ssh_user, ssh_key, known_hosts):
    host, ssh_user = (host or '').strip(), (ssh_user or '').strip()
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', host)), 'Enter the adopted host name or private IP address')
    require(bool(re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', ssh_user)), 'Enter the SSH administration user')
    require(ssh_key and Path(ssh_key).is_file(), 'Choose the SSH private key file for the adopted host')
    require(known_hosts and Path(known_hosts).is_file(), 'Choose the known_hosts file with the enrolled host key')
    return {'host': host, 'ssh_user': ssh_user, 'ssh_key': str(Path(ssh_key).resolve()), 'known_hosts': str(Path(known_hosts).resolve())}


def repository_settings(git_service, owner, name, approvers, server_url=None, ca_certificate=None, usage='organisation', isolation='local-isolated'):
    require(git_service in REVIEW_MODES, 'Choose GitHub SaaS or GitHub Enterprise Server')
    repository = {'mode': git_service, 'owner': (owner or '').strip(), 'name': (name or '').strip(),
                  'approvers': [a.strip().lstrip('@') for a in (approvers or '').split(',') if a.strip()]}
    if git_service == 'github-enterprise':
        repository['server_url'] = server_origin((server_url or '').strip())
        if ca_certificate is not None:
            repository['ca_certificate'] = ca_certificate
    return scaffold.validate_repository(repository, usage, isolation)


def build(choices, settings, repository):
    """Assemble and validate the infrastructure-only foundation from UI choices.

    choices: location, provider, git_service, network_mode, organisation, environment
    settings: name, architecture, capacity, plus local/existing/azure details
    """
    target = destinations.target_for(choices['location'], choices.get('provider'))
    config = {'api_version': 'nomiarch.io/v1alpha1', 'name': settings['name'], 'target': target,
              'architecture': settings['architecture'], 'capacity': settings['capacity'],
              'lifecycle': {'destroy_after': False, 'max_runtime_minutes': 120}}
    if target == 'local':
        config['local'] = {'image': settings.get('image') or '@admitted-image', 'image_sha256': PINS[settings['architecture']]['image'][1]}
    elif target == 'existing':
        config['existing'] = dict(settings['existing'])
    else:
        config['azure'] = dict(settings['azure'], ssh_key='@controller', ssh_public_key='@controller')
    return scaffold.infrastructure_project(config, choices['organisation'], choices['environment'], 'organisation',
                                           choices['network_mode'], repository, choices['git_service'])


def controller_paths(value, settings):
    """Private inputs for the controller only: never written into the configuration folder."""
    target = value['configuration']['target']
    if target == 'local':
        return {'image': settings['image']}
    if target == 'existing':
        return {'ssh_key': settings['existing']['ssh_key'], 'known_hosts': settings['existing']['known_hosts']}
    return {}
