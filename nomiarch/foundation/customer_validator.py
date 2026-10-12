#!/usr/bin/python3
"""Nomiarch customer repository validation (vendored, standard library only).

This file is generated into every infrastructure-only customer repository and
runs as the `nomiarch-validate` check. It has no deployment credentials and
never creates, changes or reads infrastructure. It checks that the proposed
repository content is a complete, unmodified instance of the admitted recipe
and that the approval policy was not weakened.

The separate human-operated controller independently re-verifies the merged
commit against the recipe before any plan or deployment; this check is an
early, credential-free gate, not the final authority.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPE_VERSION = '0.4.0'
CHECK_NAME = 'nomiarch-validate'
ALLOWED_ROOTS = ('policies/', 'validation/', 'environments/', 'modules/', 'inventory/', 'requests/', 'trust/', '.github/')
ALLOWED_FILES = {'README.md', 'foundation.json', '.gitignore'}
REQUIRED_APPROVALS = {'schema': 1, 'require_human': True, 'allow_agent_apply': False, 'changes_require_new_plan': True,
                      'approval_lifetime_minutes': 60, 'validation_has_deployment_credentials': False,
                      'executor': 'separate-human-operated-controller', 'nomi_may_apply': False,
                      'nomi_may_merge': False, 'nomi_may_modify_policy': False}
SLUG = r'[a-z][a-z0-9-]{1,39}'


class Failure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def load(name):
    with open(os.path.join(ROOT, name), 'rb') as stream:
        raw = stream.read()
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError as error:
        raise Failure(name + ': invalid JSON (' + str(error) + ')')


def tracked_files():
    files = {}
    for directory, folders, names in os.walk(ROOT):
        folders[:] = sorted(f for f in folders if f != '.git')
        for name in sorted(names):
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, ROOT).replace(os.sep, '/')
            require(not os.path.islink(path), 'Symlinks are not accepted: ' + relative)
            allowed = relative in ALLOWED_FILES or any(relative.startswith(prefix) for prefix in ALLOWED_ROOTS)
            require(allowed, 'File outside the recipe layout: ' + relative)
            require(os.path.getsize(path) < 2 * 1024 * 1024, 'Unexpected large file: ' + relative)
            with open(path, 'rb') as stream:
                files[relative] = hashlib.sha256(stream.read()).hexdigest()
    require(0 < len(files) <= 100, 'Invalid file count')
    return files


def check_manifest(files):
    manifest = load('validation/manifest.json')
    require(manifest.get('schema') == 1 and isinstance(manifest.get('files'), dict), 'validation/manifest.json is malformed')
    listed = dict(manifest['files'])
    actual = {k: v for k, v in files.items() if k != 'validation/manifest.json'}
    missing = sorted(set(listed) - set(actual))
    extra = sorted(set(actual) - set(listed))
    changed = sorted(k for k in set(listed) & set(actual) if listed[k] != actual[k])
    require(not missing, 'Files listed in the manifest are missing: ' + ', '.join(missing))
    require(not extra, 'Files not listed in the manifest: ' + ', '.join(extra))
    require(not changed, 'Files differ from the manifest: ' + ', '.join(changed))
    return len(actual)


def check_foundation():
    value = load('foundation.json')
    require(isinstance(value, dict), 'foundation.json must be an object')
    allowed = {'schema', 'id', 'recipe_version', 'scope', 'destination', 'organisation', 'environment', 'usage',
               'isolation', 'repository', 'configuration', 'maintenance'}
    unknown = sorted(set(value) - allowed)
    require(not unknown, 'foundation.json has unknown fields: ' + ', '.join(unknown))
    require(value.get('schema') == 1, 'Unsupported foundation schema')
    require(value.get('recipe_version') == RECIPE_VERSION, 'This validator admits recipe ' + RECIPE_VERSION + ' only')
    require(value.get('scope') == 'infrastructure', 'This repository must declare an infrastructure-only scope')
    require('runtime' not in value, 'Infrastructure-only foundations carry no Core/model runtime pins')
    require(isinstance(value.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', value['id']), 'Invalid foundation identity')
    for key in ('organisation', 'environment'):
        require(isinstance(value.get(key), str) and re.fullmatch(SLUG, value[key]), 'Invalid ' + key)
    require(value.get('usage') in ('personal', 'organisation'), 'Invalid usage')
    config = value.get('configuration')
    require(isinstance(config, dict), 'configuration must be an object')
    target = config.get('target')
    require(target in ('local', 'existing', 'azure'), 'Unsupported destination target')
    destination = value.get('destination')
    require(isinstance(destination, dict) and set(destination) == {'location', 'provider'}, 'Invalid destination declaration')
    expected = {'local': ('local', 'multipass'), 'existing': ('on-premises', 'existing-host'), 'azure': ('cloud', 'azure')}[target]
    require((destination['location'], destination['provider']) == expected, 'Destination does not match the configured target')
    isolation = value.get('isolation')
    allowed_isolation = {'local': ('local-isolated', 'disconnected'), 'existing': ('site-restricted', 'disconnected'), 'azure': ('cloud-restricted',)}
    require(isolation in allowed_isolation[target], 'Network mode is not available for this destination')
    repository = value.get('repository', {})
    mode = repository.get('mode')
    require(mode in ('local', 'github', 'github-enterprise', 'offline'), 'Unsupported repository mode')
    if isolation == 'disconnected':
        require(mode != 'github', 'Fully disconnected operation cannot use github.com')
        require(target != 'azure', 'Fully disconnected operation cannot use a cloud destination')
    capacity = config.get('capacity', {})
    for key, minimum, maximum in (('cpus', 2, 64), ('memory_gib', 4, 512), ('disk_gib', 30, 4096)):
        require(type(capacity.get(key)) is int and minimum <= capacity[key] <= maximum, 'capacity.' + key + ' is out of range')
    for other in {'local', 'existing', 'azure'} - {target}:
        require(other not in config, 'Only the selected destination settings are allowed')
    lifecycle = config.get('lifecycle', {})
    require(lifecycle.get('destroy_after') is False, 'Customer infrastructure must be retained')
    if target == 'existing':
        details = config.get('existing', {})
        require(details.get('ssh_key') == '@controller' and details.get('known_hosts') == '@controller', 'Private SSH material belongs outside the repository')
    if target == 'azure':
        details = config.get('azure', {})
        require(details.get('ssh_key') == '@controller' and details.get('ssh_public_key') == '@controller', 'SSH key references belong outside the repository')
    if 'maintenance' in value:
        operation = value['maintenance']
        require(isinstance(operation, dict) and operation.get('action') == 'destroy', 'Only a reviewed removal request is supported on infrastructure-only foundations')
        require(target != 'existing', 'Adopted hosts are never deleted by Nomiarch')
    return value


def check_policies(value):
    approvals = load('policies/approvals.json')
    require(approvals == REQUIRED_APPROVALS, 'policies/approvals.json differs from the required approval policy')
    network = load('policies/network.json')
    require(network.get('mode') == value['isolation'] and network.get('default_outbound') == 'deny', 'policies/network.json was weakened or does not match the declared network mode')
    validation = load('policies/validation.json')
    require(CHECK_NAME in validation.get('required_checks', []) and validation.get('strict') is True
            and validation.get('deployment_credentials') == 'none', 'policies/validation.json no longer requires the trusted validation check')
    if value['repository'].get('mode') in ('github', 'github-enterprise'):
        with open(os.path.join(ROOT, '.github', 'CODEOWNERS'), 'r', encoding='utf-8') as stream:
            owners = stream.read().split()
        reviewers = {'@' + login for login in value['repository'].get('approvers', [])}
        require(owners and owners[0] == '*' and set(owners[1:]) == reviewers, 'CODEOWNERS does not match the designated reviewers')
        workflow = os.path.join(ROOT, '.github', 'workflows', 'nomiarch-validate.yml')
        require(os.path.isfile(workflow), 'The nomiarch-validate workflow is missing')


def check_terraform(value):
    if value['configuration']['target'] != 'azure':
        return 'not applicable'
    tofu = shutil.which('tofu')
    if not tofu:
        return 'skipped: tofu is not admitted on this runner; the controller validates the plan before approval'
    environment = os.path.join(ROOT, 'environments', value['environment'])
    for args in (['fmt', '-check', '-recursive'], ['init', '-backend=false', '-input=false', '-lockfile=readonly'], ['validate', '-no-color']):
        result = subprocess.run([tofu, *args], cwd=environment, capture_output=True, text=True, timeout=600)
        require(result.returncode == 0, 'tofu ' + args[0] + ' failed: ' + result.stdout[-800:] + result.stderr[-800:])
    return 'passed'


def main():
    try:
        files = tracked_files()
        counted = check_manifest(files)
        value = check_foundation()
        check_policies(value)
        terraform = check_terraform(value)
        result = {'check': CHECK_NAME, 'status': 'passed', 'files': counted, 'recipe_version': RECIPE_VERSION,
                  'scope': value['scope'], 'target': value['configuration']['target'], 'terraform': terraform,
                  'deployment_credentials': 'none', 'authority': 'validation only; approval and execution are separate'}
        print(json.dumps(result, indent=2))
        return 0
    except Failure as error:
        print(json.dumps({'check': CHECK_NAME, 'status': 'failed', 'error': str(error)}, indent=2))
        return 1


if __name__ == '__main__':
    sys.exit(main())
