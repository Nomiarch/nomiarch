"""Bounded foundation observations. Evaluation cannot authorize deployment."""
import hashlib
import re

from nomiarch.common import canonical


def evaluate(config):
    if set(config) != {'kind', 'name', 'desired', 'observed', 'configuration_sha256'}:
        raise ValueError('Invalid foundation observation fields')
    if not re.fullmatch(r'[0-9a-f]{32}', config['name']) or not re.fullmatch(r'[0-9a-f]{64}', config['configuration_sha256']):
        raise ValueError('Invalid foundation observation identity')
    desired, observed = config['desired'], config['observed']
    required = {'cpus', 'memory_gib', 'disk_gib', 'outbound_blocked'}
    if not isinstance(desired, dict) or not isinstance(observed, dict) or not required <= set(desired) or set(observed) != set(desired) or set(desired) - required - {'core_version', 'repository_head'}:
        raise ValueError('A complete bounded observation is required')
    allowed = set(desired)
    for key, pattern in [('core_version', r'[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?'), ('repository_head', r'[0-9a-f]{40}')]:
        if key in allowed and not all(isinstance(v[key], str) and re.fullmatch(pattern, v[key]) for v in (desired, observed)):
            raise ValueError('Invalid observed release/repository identity')
    if desired['outbound_blocked'] is not True or type(observed['outbound_blocked']) is not bool:
        raise ValueError('The observer cannot weaken the network policy')
    for field in required - {'outbound_blocked'}:
        for source in (desired, observed):
            if type(source[field]) not in (int, float) or not 0 < source[field] <= 8192:
                raise ValueError('Invalid observed capacity')
    findings = []
    for field in sorted(allowed):
        actual, expected = observed[field], desired[field]
        # Disks may grow, but this evaluator never proposes an unsafe shrink.
        matches = actual >= expected if field == 'disk_gib' else actual == expected
        findings.append({'check': field, 'status': 'pass' if matches else 'fail', 'actual': actual,
                         'expected': expected, 'recommendation': 'Keep the approved setting' if matches else 'Restore the human-approved configuration'})
        if not matches and field in {'core_version', 'repository_head'}:
            findings[-1]['recommendation'] = 'Review the repository/release change and prepare an operation-specific deployment plan'
    return {'name': config['name'], 'kind': 'foundation', 'configuration_sha256': config['configuration_sha256'],
            'observation_sha256': hashlib.sha256(canonical(config)).hexdigest(), 'findings': findings,
            'authority': 'proposal-only', 'scope': 'Controller observations; no infrastructure credentials or apply capability in Core'}
