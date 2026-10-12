"""User-facing bootstrap choices and their compatibility rules (pure data, no I/O).

Location → provider adapters are deliberately modular: a new hypervisor or cloud
adapter registers a provider here plus a `nomiarch.bootstrap.providers` class.
Unimplemented providers are listed so the UI can explain them, never selected.
"""
from nomiarch.bootstrap.config import require

LOCATIONS = {
    'local': {'label': 'Local — create a dedicated VM on this computer',
              'creates_vm': True, 'providers': ('multipass',)},
    'on-premises': {'label': 'On-premises — adopt a dedicated Ubuntu VM/host you provide over trusted SSH',
                    'creates_vm': False, 'providers': ('existing-host',)},
    'cloud': {'label': 'Cloud — create the VM with a cloud provider',
              'creates_vm': True, 'providers': ('azure',)},
}
PROVIDERS = {
    'multipass': {'label': 'Multipass VM on this computer', 'target': 'local', 'implemented': True,
                  'note': 'Creates one retained Ubuntu 24.04 VM from the admitted cloud image.'},
    'existing-host': {'label': 'Existing dedicated Ubuntu host (SSH)', 'target': 'existing', 'implemented': True,
                      'note': 'Does not create the VM. Your organisation provides a dedicated Ubuntu 24.04 host, '
                              'an SSH user with sudo and its host key enrolled out of band. The host is never deleted by Nomiarch.'},
    'azure': {'label': 'Microsoft Azure', 'target': 'azure', 'implemented': True,
              'note': 'Creates a private VM, NSG, disks and network attachment from the vendored OpenTofu module. Charges apply until removed.'},
}
GIT_SERVICES = {
    'github': {'label': 'GitHub SaaS — github.com, including Enterprise Cloud organisations', 'internal': False},
    'github-enterprise': {'label': 'GitHub Enterprise Server — your organisation’s own server (HTTPS address, optional private CA)', 'internal': True,
                          'note': 'Selecting this does not install or license a Git server.'},
}
NETWORK_MODES = {
    'restricted-connected': {'label': 'Restricted connected — outbound denied by policy; platform, Git and administration routes remain'},
    'fully-disconnected': {'label': 'Fully disconnected — no internet, SaaS, github.com or external model access; everything is admitted through controlled transfer'},
}
ISOLATION = {  # (target, network mode) → recorded isolation value
    ('local', 'restricted-connected'): 'local-isolated', ('local', 'fully-disconnected'): 'disconnected',
    ('existing', 'restricted-connected'): 'site-restricted', ('existing', 'fully-disconnected'): 'disconnected',
    ('azure', 'restricted-connected'): 'cloud-restricted',
}


def provider_for(location, provider=None):
    require(location in LOCATIONS, 'Choose Local, On-premises or Cloud')
    options = LOCATIONS[location]['providers']
    provider = provider or (options[0] if len(options) == 1 else None)
    require(provider in options, 'Choose a provider offered for this location')
    require(PROVIDERS[provider]['implemented'], PROVIDERS[provider]['label'] + ' is not implemented yet')
    return provider


def target_for(location, provider=None):
    return PROVIDERS[provider_for(location, provider)]['target']


def location_of(target):
    for name, details in PROVIDERS.items():
        if details['target'] == target:
            return next(location for location, entry in LOCATIONS.items() if name in entry['providers']), name
    raise KeyError(target)


def isolation_for(target, network_mode):
    require(network_mode in NETWORK_MODES, 'Choose restricted connected or fully disconnected operation')
    value = ISOLATION.get((target, network_mode))
    require(value is not None, 'Fully disconnected operation needs Local or On-premises infrastructure; cloud destinations keep platform dependencies')
    return value


def network_mode_of(isolation):
    return 'fully-disconnected' if isolation == 'disconnected' else 'restricted-connected'


def combination_problems(location, provider, git_service, network_mode):
    """Explain every incompatible choice before anything is generated or deployed."""
    problems = []
    try:
        provider = provider_for(location, provider)
    except Exception as error:
        return [str(error)]
    if git_service not in GIT_SERVICES:
        problems.append('Choose GitHub SaaS or GitHub Enterprise Server')
    if network_mode not in NETWORK_MODES:
        problems.append('Choose restricted connected or fully disconnected operation')
    if problems:
        return problems
    if network_mode == 'fully-disconnected':
        if location == 'cloud':
            problems.append('Fully disconnected operation cannot use a cloud provider: Azure keeps control-plane and platform dependencies. Choose Local or On-premises, or choose restricted connected.')
        if git_service == 'github':
            problems.append('Fully disconnected operation cannot use GitHub SaaS: github.com is outside the boundary. Choose GitHub Enterprise Server inside the boundary.')
    # Restricted connected is never substituted for an air gap: the caller must
    # change a choice; nothing here rewrites network_mode.
    return problems


def describe(location, provider, git_service, network_mode):
    provider = provider_for(location, provider)
    return {'location': location, 'provider': provider, 'target': PROVIDERS[provider]['target'],
            'git_service': git_service, 'network_mode': network_mode,
            'creates_vm': LOCATIONS[location]['creates_vm'],
            'physical_air_gap': 'site qualification required; software settings do not prove disconnection'}
