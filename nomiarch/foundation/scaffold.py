"""Deterministic, bounded scaffolds. Customer data never becomes executable code."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

from nomiarch.bootstrap.config import keys, require, validate
from nomiarch.common import NomiarchError, canonical, digest, read_json
from nomiarch.desktop.catalog import CORE_VERSION, PINS
from . import INFRASTRUCTURE_RECIPE_VERSION, RECIPE_VERSION, SUPPORTED_RECIPE_VERSIONS
from . import destinations
from .repository_transport import REVIEW_MODES, certificate_context, server_origin

VALIDATION_CHECK = 'nomiarch-validate'
GITHUB_ACTIONS_APP_ID = 15368  # github.com's GitHub Actions app; GHES verifies by app slug.
CHECKOUT_ACTION = 'actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09'
INFRASTRUCTURE_APPROVALS = {'schema': 1, 'require_human': True, 'allow_agent_apply': False, 'changes_require_new_plan': True,
                            'approval_lifetime_minutes': 60, 'validation_has_deployment_credentials': False,
                            'executor': 'separate-human-operated-controller', 'nomi_may_apply': False,
                            'nomi_may_merge': False, 'nomi_may_modify_policy': False}


def slug(value, label):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,39}", value),
            label + " must be 2–40 lowercase letters, digits or hyphens")
    return value


def is_infrastructure(value):
    return value.get('recipe_version') == INFRASTRUCTURE_RECIPE_VERSION


def validate_project(value):
    value = copy.deepcopy(value)
    if is_infrastructure(value):
        return validate_infrastructure_project(value)
    keys(value, {"schema", "id", "recipe_version", "organisation", "environment", "usage", "isolation", "repository", "configuration", "runtime", "reconciliation", "maintenance"}, "foundation")
    require(value.get("schema") == 1, "Unsupported foundation schema")
    require(value.get("recipe_version") in SUPPORTED_RECIPE_VERSIONS, "This foundation needs its matching installer recipe version")
    require(isinstance(value.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["id"]), "Invalid foundation identity")
    slug(value.get("organisation"), "Organisation")
    slug(value.get("environment"), "Environment")
    require(value.get("usage") in {"personal", "organisation"}, "Choose personal or organisation setup")
    config = validate(value.get("configuration"))
    runtime = value.get('runtime', {})
    keys(runtime, {'core_version', 'architecture', 'bundle_sha256', 'release_key_sha256'}, 'runtime')
    require(runtime.get('architecture') == config['architecture'], 'Runtime architecture does not match the VM')
    require(isinstance(runtime.get('core_version'), str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?', runtime['core_version']), 'Invalid Core release version')
    require(all(isinstance(runtime.get(k), str) and re.fullmatch(r'[0-9a-f]{64}', runtime[k]) for k in ('bundle_sha256', 'release_key_sha256')), 'Runtime artifacts must be pinned by SHA-256')
    require(config["target"] in {"local", "azure"}, "This recipe supports local or Azure foundations")
    require(not config["lifecycle"]["destroy_after"], "Customer foundations must be retained")
    allowed = {"local": {"local-isolated", "disconnected"}, "azure": {"cloud-restricted"}}
    require(value.get("isolation") in allowed[config["target"]], "Isolation mode does not match the destination")
    # Paths and private material belong in the controller's private run directory.
    details = config[config["target"]]
    if config["target"] == "local":
        require(details["image"] == "@admitted-image", "Store the image path outside the customer repository")
    else:
        for field in ("ssh_key", "ssh_public_key"):
            require(details[field] == "@controller", "SSH key paths belong outside the customer repository")
    validate_repository(value.get('repository', {}), value['usage'], value['isolation'], value['recipe_version'])
    if 'reconciliation' in value:
        change = value['reconciliation']
        keys(change, {'observation_sha256', 'configuration_sha256', 'checks'}, 'reconciliation')
        require(all(isinstance(change.get(k), str) and re.fullmatch(r'[0-9a-f]{64}', change[k]) for k in ('observation_sha256', 'configuration_sha256')), 'Invalid reconciliation evidence')
        require(isinstance(change.get('checks'), list) and 1 <= len(change['checks']) <= 4
                and all(k in {'cpus', 'memory_gib', 'disk_gib', 'outbound_blocked'} for k in change['checks']), 'Unsupported reconciliation request')
    if 'maintenance' in value:
        require('reconciliation' not in value, 'Review one operation at a time')
        operation = value['maintenance']
        keys(operation, {'action', 'id', 'configuration_sha256'}, 'maintenance')
        require(operation.get('action') in {'upgrade', 'repair', 'destroy'}, 'Unsupported maintenance operation')
        require(isinstance(operation.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', operation['id']), 'Invalid maintenance identity')
        require(isinstance(operation.get('configuration_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', operation['configuration_sha256']), 'Invalid maintenance baseline')
    return value


def validate_repository(repository, usage, isolation, recipe_version=RECIPE_VERSION):
    keys(repository, {"mode", "owner", "name", "approvers", "server_url", "ca_certificate"}, "repository")
    mode = repository.get('mode')
    require(mode in {'local', 'offline'} | REVIEW_MODES, 'Choose local records, a supported repository or configuration export')
    if usage == 'organisation':
        require(mode in REVIEW_MODES | {'offline'}, 'Organisation setup requires a customer repository')
    if isolation == 'disconnected':
        require(mode != 'github', 'Disconnected operation requires an internal server or configuration export')
    if mode in REVIEW_MODES:
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", repository.get("owner", ""))), "Invalid GitHub owner")
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository.get("name", ""))), "Invalid GitHub repository name")
        approvers = repository.get("approvers", [])
        require(isinstance(approvers, list) and 1 <= len(approvers) <= 10, "Enter at least one human GitHub approver")
        require(all(isinstance(a, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", a) for a in approvers), "Invalid approver login")
        require(len(set(a.lower() for a in approvers)) == len(approvers), "Approvers must be unique")
        if mode == 'github-enterprise':
            require(recipe_version != '0.2.0', 'Internal repository support requires recipe 0.3.0 or later')
            require(repository.get('server_url') == server_origin(repository.get('server_url')), 'Use the canonical internal HTTPS server address')
            if 'ca_certificate' in repository:
                certificate_context(repository['ca_certificate'])
        else:
            require(not {'server_url', 'ca_certificate'} & set(repository), 'Public GitHub uses its fixed server and system certificate trust')
    else:
        require(set(repository) == {'mode'}, 'Repository account settings require a supported repository mode')
    return repository


def validate_infrastructure_project(value):
    """Recipe 0.4.0: infrastructure only. No Core, model or inference artifact is referenced."""
    keys(value, {"schema", "id", "recipe_version", "scope", "destination", "organisation", "environment", "usage",
                 "isolation", "repository", "configuration", "maintenance"}, "foundation")
    require(value.get("schema") == 1, "Unsupported foundation schema")
    require(value.get('scope') == 'infrastructure', 'This recipe establishes infrastructure only; declare scope "infrastructure"')
    require('runtime' not in value, 'Infrastructure-only foundations carry no Core/model runtime pins')
    require(isinstance(value.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["id"]), "Invalid foundation identity")
    slug(value.get("organisation"), "Organisation")
    slug(value.get("environment"), "Environment")
    require(value.get("usage") in {"personal", "organisation"}, "Choose personal or organisation setup")
    config = validate(value.get("configuration"))
    target = config['target']
    destination = value.get('destination')
    keys(destination, {'location', 'provider'}, 'destination')
    require(destinations.target_for(destination.get('location'), destination.get('provider')) == target,
            'The declared destination does not match the configured target')
    require(not config["lifecycle"]["destroy_after"], "Customer infrastructure must be retained")
    allowed = {'local': {'local-isolated', 'disconnected'}, 'existing': {'site-restricted', 'disconnected'}, 'azure': {'cloud-restricted'}}
    require(value.get("isolation") in allowed[target],
            'Fully disconnected operation needs Local or On-premises infrastructure' if target == 'azure' else "Network mode does not match the destination")
    details = config[target]
    if target == 'local':
        require(details["image"] == "@admitted-image", "Store the image path outside the customer repository")
    elif target == 'existing':
        require(details['ssh_key'] == '@controller' and details['known_hosts'] == '@controller', 'Private SSH material belongs outside the customer repository')
    else:
        for field in ("ssh_key", "ssh_public_key"):
            require(details[field] == "@controller", "SSH key paths belong outside the customer repository")
    validate_repository(value.get('repository', {}), value['usage'], value['isolation'], value['recipe_version'])
    if 'maintenance' in value:
        operation = value['maintenance']
        keys(operation, {'action', 'id', 'configuration_sha256'}, 'maintenance')
        require(operation.get('action') == 'destroy', 'Only a reviewed removal is supported before application installation')
        require(target != 'existing', 'Adopted on-premises hosts are never deleted by Nomiarch; retire the controller record instead')
        require(isinstance(operation.get('id'), str) and re.fullmatch(r'[0-9a-f]{32}', operation['id']), 'Invalid maintenance identity')
        require(isinstance(operation.get('configuration_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', operation['configuration_sha256']), 'Invalid maintenance baseline')
    return value


def infrastructure_project(configuration, organisation, environment, usage='personal', network_mode='restricted-connected', repository=None, git_service=None):
    """Build an infrastructure-only foundation. Incompatible choices fail here, before any file exists."""
    config = copy.deepcopy(configuration)
    target = config['target']
    location, provider = destinations.location_of(target)
    repository = repository or {'mode': 'local'}
    git_service = git_service or (repository['mode'] if repository['mode'] in REVIEW_MODES else None)
    if git_service is not None:
        problems = destinations.combination_problems(location, provider, git_service, network_mode)
        require(not problems, ' '.join(problems))
    isolation = destinations.isolation_for(target, network_mode)
    if target == 'local':
        config['local']['image'] = '@admitted-image'
    elif target == 'existing':
        config['existing']['ssh_key'] = config['existing']['known_hosts'] = '@controller'
    else:
        config['azure']['ssh_key'] = config['azure']['ssh_public_key'] = '@controller'
    config['lifecycle'] = {'destroy_after': False, 'max_runtime_minutes': 120}
    return validate_project({'schema': 1, 'id': uuid.uuid4().hex, 'recipe_version': INFRASTRUCTURE_RECIPE_VERSION,
                             'scope': 'infrastructure', 'destination': {'location': location, 'provider': provider},
                             'organisation': organisation, 'environment': environment, 'usage': usage,
                             'isolation': isolation, 'repository': repository, 'configuration': config})


def project(configuration, organisation, environment, usage="personal", isolation=None, repository=None):
    config = copy.deepcopy(configuration)
    target = config["target"]
    if target == "local":
        config["local"]["image"] = "@admitted-image"
    elif target == "azure":
        config["azure"]["ssh_key"] = config["azure"]["ssh_public_key"] = "@controller"
    config["lifecycle"] = {"destroy_after": False, "max_runtime_minutes": 120}
    return validate_project({"schema": 1, "id": uuid.uuid4().hex, "recipe_version": RECIPE_VERSION,
                             "organisation": organisation, "environment": environment, "usage": usage,
                             "isolation": isolation or ("local-isolated" if target == "local" else "cloud-restricted"),
                             "repository": repository or {"mode": "local"}, "configuration": config,
                             "runtime": {"core_version": CORE_VERSION, "architecture": config['architecture'],
                                         "bundle_sha256": PINS[config['architecture']]['bundle'][1],
                                         "release_key_sha256": PINS[config['architecture']]['key'][1]}})


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def admitted_runtime(architecture):
    return {'core_version': CORE_VERSION, 'architecture': architecture,
            'bundle_sha256': PINS[architecture]['bundle'][1], 'release_key_sha256': PINS[architecture]['key'][1]}


def azure_root_files(value, source, files):
    """Vendor the versioned module and generate the environment root (shared by both recipes)."""
    config = value["configuration"]
    env = "environments/" + value["environment"]
    module = Path(source) / "infra/azure"
    for name in ("main.tf", "variables.tf", ".terraform.lock.hcl"):
        files["modules/azure/" + name] = (module / name).read_text()
    # The root has the same variable schema as the versioned module. Runtime-only
    # keys and identifiers are supplied privately by the independent controller.
    files[env + "/variables.tf"] = (module / "variables.tf").read_text()
    files[env + "/.terraform.lock.hcl"] = (module / ".terraform.lock.hcl").read_text()
    names = re.findall(r'variable "([a-z_]+)"', files[env + "/variables.tf"])
    width = max(map(len, ['source', *names]))
    files[env + "/main.tf"] = ('terraform {\n  required_version = ">= 1.10.0, < 2.0.0"\n}\n\n'
        'module "foundation" {\n' + f'  {"source":<{width}} = "../../modules/azure"\n'
        + "".join(f"  {name:<{width}} = var.{name}\n" for name in names) + '}\n\n'
        'output "inventory" { value = module.foundation.inventory }\n')
    details = config["azure"]
    public = {k: v for k, v in details.items() if k not in {"ssh_key", "ssh_public_key"}}
    public["disk_gib"] = config["capacity"]["disk_gib"]
    files[env + "/settings.auto.tfvars.json"] = json_text(public)


def validator_source():
    """The vendored customer validator, loaded from this package in both source and packaged runs.

    PyInstaller ships modules as bytecode, so the source file is added to the bundle as
    data next to this module (see .github/workflows/desktop.yml). Nothing is downloaded
    and the source checkout is never required at run time.
    """
    candidates = []
    try:
        from importlib import resources
        candidates.append(lambda: resources.files('nomiarch.foundation').joinpath('customer_validator.py').read_text(encoding='utf-8'))
    except (ImportError, AttributeError):
        pass
    here = Path(__file__).resolve().parent
    candidates.append(lambda: (here / 'customer_validator.py').read_text(encoding='utf-8'))
    frozen = getattr(sys, '_MEIPASS', None)
    if frozen:
        candidates.append(lambda: (Path(frozen) / 'nomiarch' / 'foundation' / 'customer_validator.py').read_text(encoding='utf-8'))
    for load in candidates:
        try:
            text = load()
        except (OSError, TypeError, ValueError):
            continue
        require(text.startswith('#!/usr/bin/python3') and 'def main():' in text and len(text) < 65536, 'The packaged customer validator is damaged')
        return text.replace('\r\n', '\n')
    raise NomiarchError('The customer validator resource is missing from this installation; reinstall the desktop package')


def validation_workflow(repo_mode):
    runner = "ubuntu-24.04" if repo_mode == 'github' else "[self-hosted, linux, nomiarch-validation]"
    return f'''# Generated by the Nomiarch recipe. Runs with read-only repository access and no
# deployment credentials. The separate controller re-verifies the merged commit.
name: {VALIDATION_CHECK}
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  {VALIDATION_CHECK}:
    name: {VALIDATION_CHECK}
    runs-on: {runner}
    timeout-minutes: 10
    steps:
      - uses: {CHECKOUT_ACTION}
        with:
          persist-credentials: false
      - name: Validate the customer repository against the admitted recipe
        run: python3 validation/validate.py
'''


def render_infrastructure(value, source):
    config = value["configuration"]
    target = config["target"]
    env = "environments/" + value["environment"]
    repo_mode = value["repository"]["mode"]
    network_mode = destinations.network_mode_of(value['isolation'])
    files = {
        "foundation.json": json_text(value),
        ".gitignore": "# Secrets, keys, plans and state stay outside this repository.\n.terraform/\n*.tfstate*\n*.tfplan\n*.pem\n*.key\n*.age\n*.log\nprivate/\nknown_hosts\n",
        "policies/approvals.json": json_text(INFRASTRUCTURE_APPROVALS),
        "policies/network.json": json_text({"schema": 1, "mode": value["isolation"], "network_mode": network_mode, "default_outbound": "deny",
                                            "administration": "explicitly approved connections only",
                                            "physical_air_gap": "site qualification required; a private IP or firewall does not prove disconnection"}),
        "policies/validation.json": json_text({"schema": 1, "required_checks": [VALIDATION_CHECK], "strict": True,
                                               "trusted_source": "github-actions", "deployment_credentials": "none",
                                               "bound_to": "exact reviewed head commit"}),
        "inventory/repositories.json": json_text({"schema": 1, "repositories": [
            {"owner": value["repository"].get("owner"), "name": value["repository"].get("name"), "service": repo_mode,
             "purpose": "foundation configuration", "created_by": "human-initiated bootstrap"}] if repo_mode in REVIEW_MODES else [],
            "provisioning": "Propose additional repositories through a pull request to this file. An approved provisioning "
                            "workflow creates them; agents never create repositories directly. General-purpose provisioning is deferred."}),
        "validation/validate.py": validator_source(),
    }
    if target == "azure":
        azure_root_files(value, source, files)
    elif target == 'existing':
        details = config['existing']
        files[env + '/host.json'] = json_text({"driver": "existing-host", "host": details['host'], "ssh_user": details['ssh_user'],
                                               "minimum_capacity": config["capacity"], "isolation": value["isolation"],
                                               "created_by_nomiarch": False, "deletable_by_nomiarch": False})
    else:
        files[env + "/local.json"] = json_text({"driver": "multipass", "capacity": config["capacity"],
                                              "image_sha256": config["local"]["image_sha256"],
                                              "isolation": value["isolation"], "scope": "infrastructure"})
    if repo_mode in REVIEW_MODES:
        files[".github/CODEOWNERS"] = "* " + " ".join("@" + a for a in value["repository"]["approvers"]) + "\n"
        files['.github/workflows/' + VALIDATION_CHECK + '.yml'] = validation_workflow(repo_mode)
    if repo_mode == 'github-enterprise' and 'ca_certificate' in value['repository']:
        files['trust/repository-ca.crt'] = value['repository']['ca_certificate']
    if 'maintenance' in value:
        files['requests/maintenance.json'] = json_text(value['maintenance'])
    location = value['destination']['location']
    files["README.md"] = f'''# {value['organisation']} / {value['environment']} — infrastructure foundation

This is your Nomiarch infrastructure configuration (recipe {value['recipe_version']}, scope:
infrastructure only). You own this repository and the environment it describes.
Destination: {location} ({value['destination']['provider']}); network mode: {network_mode}.

This recipe establishes infrastructure only. It does not install Nomi Core, models,
agents or application containers; application installation is a separate, later
reviewed operation with its own recipe. "Infrastructure ready" is not "application ready".

Change control: request → proposal branch and pull request → `{VALIDATION_CHECK}` check
(no deployment credentials) → independent human review → authorized merge → separate
human-operated controller prepares an exact plan → explicit plan approval → execution →
verification and audit record. A pull request never authorizes deployment. The controller
stores keys, plans, state and logs privately outside this repository.

`validation/validate.py` is the vendored, standard-library check that runs as
`{VALIDATION_CHECK}`. It verifies this repository against `validation/manifest.json`, the
recipe schema and the fixed approval policy. The controller independently compares the
merged commit with the admitted recipe before planning; a modified validator cannot
substitute for that comparison. Editing `policies/*` cannot grant an AI deployment rights.

`inventory/repositories.json` declares the repositories this foundation owns. New
repositories are proposed by pull request to that file and created by an approved
provisioning workflow, never directly by an agent.

{'Azure uses the vendored OpenTofu module; the controller supplies private runtime inputs. Azure platform dependencies remain; this is a restricted private cloud workload, not an air gap.' if target == 'azure' else 'The adopted host is provided by your organisation and is never created or deleted by Nomiarch. Its SSH host key and private key stay with the controller.' if target == 'existing' else 'Local VM lifecycle uses the Multipass controller adapter and local.json. This recipe does not claim Terraform manages local VMs.'}

Network isolation configured here is a software setting. A fully disconnected site must be
qualified by the site: external routes absent, all artifacts admitted through controlled
transfer, internal Git, DNS, time and reviewers inside the boundary. Restricted connectivity
is never a substitute for that qualification.

This scaffold is not a certification, production qualification or disaster-recovery claim.
Test your actual host, network, permissions and the documented recovery path.
'''
    if repo_mode == 'github-enterprise':
        files['README.md'] += f'''\nInternal GitHub Enterprise Server: {value['repository']['server_url']}\n
The controller connects directly over verified HTTPS to private network addresses. An optional
public CA certificate is part of the reviewed configuration; tokens and private keys never are.
The `{VALIDATION_CHECK}` job needs a self-hosted runner labelled `nomiarch-validation` inside the
boundary with Python 3.11+ and the admitted checkout action; it must hold no deployment credentials.
'''
    manifest = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}
    files['validation/manifest.json'] = json_text({'schema': 1, 'recipe_version': value['recipe_version'], 'files': manifest})
    return files


def render(value, source):
    value = validate_project(value)
    if is_infrastructure(value):
        return render_infrastructure(value, source)
    config = value["configuration"]
    target = config["target"]
    env = "environments/" + value["environment"]
    files = {
        "foundation.json": json_text(value),
        "runtime.json": json_text(value['runtime']),
        ".gitignore": "# Secrets and state stay outside this repository.\n.terraform/\n*.tfstate*\n*.tfplan\n*.pem\n*.key\n*.age\n*.log\nprivate/\n",
        "policies/approvals.json": json_text({"schema": 1, "require_human": True, "allow_agent_apply": False,
                                             "changes_require_new_plan": True, "approval_lifetime_minutes": 60}),
        "policies/network.json": json_text({"schema": 1, "mode": value["isolation"], "default_outbound": "deny",
                                            "administration": "explicitly approved connections only",
                                            "physical_air_gap": "site validation required"}),
    }
    if target == "azure":
        azure_root_files(value, source, files)
    else:
        # No shell provisioners or state-only Terraform resource masquerading as VM
        # lifecycle management. Multipass is an explicit controller adapter.
        files[env + "/local.json"] = json_text({"driver": "multipass", "capacity": config["capacity"],
                                              "image_sha256": config["local"]["image_sha256"],
                                              "isolation": value["isolation"]})
    repo_mode = value["repository"]["mode"]
    if repo_mode in REVIEW_MODES:
        files[".github/CODEOWNERS"] = "* " + " ".join("@" + a for a in value["repository"]["approvers"]) + "\n"
    if repo_mode == 'github-enterprise' and 'ca_certificate' in value['repository']:
        files['trust/repository-ca.crt'] = value['repository']['ca_certificate']
    if 'reconciliation' in value:
        files['requests/reconcile.json'] = json_text(value['reconciliation'])
    if 'maintenance' in value:
        files['requests/maintenance.json'] = json_text(value['maintenance'])
    files["README.md"] = f'''# {value['organisation']} / {value['environment']}

This is your Nomiarch foundation configuration. You own this repository and the
environment. Recipe {value['recipe_version']}; destination: {target}; mode: {value['isolation']}.

Open Nomiarch Setup to review the configuration and prepare a deployment plan.
The plan creates no infrastructure. Approve the displayed plan to deploy it.
An edit to configuration, artifacts or the saved plan invalidates its approval.
The controller stores keys, plans, locks and state in its private installation
directory outside this repository. Protect and back up that directory separately.

`foundation.json` contains the supported customer choices. `runtime.json` pins the
admitted Nomiarch release. Versioned modules and approval/network policies are included.
Use the wizard to regenerate supported settings and review the complete diff.
The preview refuses unrecognised executable template edits; custom modules require
a separately reviewed recipe. Changing a policy file cannot grant an AI deployment rights.

{'Azure uses the vendored Terraform/OpenTofu module. The desktop controller supplies private runtime inputs.' if target == 'azure' else 'Local VM lifecycle uses the Multipass controller and local.json. This preview does not claim Terraform manages local VMs.'}

Repository mode: {repo_mode}. Organisation deployments require a human-reviewed
change and a deployment-plan approval. An offline export must be admitted into your
internal Git/review system before organisation deployment can proceed.

Network isolation is distinct from physical disconnection. Public GitHub is unavailable
inside a full air gap. Keep internal Git, providers, modules, models and release files
inside the approved boundary, and admit updates through your transfer process.

This scaffold is not a claim of certification, production qualification or automatic
cloud disaster recovery. Test your actual host, network, permissions and recovery path.
'''
    if repo_mode == 'github-enterprise':
        files['README.md'] += f'''\nInternal GitHub Enterprise Server: {value['repository']['server_url']}\n
The controller connects directly over verified HTTPS to private network addresses.
An optional public CA certificate is part of the reviewed configuration; tokens
and private keys remain outside Git. The same protected-main, final-commit human
review and separate deployment-plan approval apply to every operation.
The site must provide the server, internal DNS and the disconnected boundary.
'''
    return files


def create(destination, value, source):
    destination = Path(destination).expanduser().absolute()
    require(not destination.exists(), "Choose a new folder; existing customer files will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".nomiarch-scaffold-", dir=destination.parent))
    try:
        for name, content in render(value, source).items():
            p = staged / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="\n")
        os.rename(staged, destination)
    finally:
        if staged.exists(): shutil.rmtree(staged)
    return destination


def snapshot(directory):
    root = Path(directory).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts: continue
        require(not path.is_symlink(), "Scaffold symlinks are not accepted")
        if path.is_file():
            require(path.stat().st_size < 2 * 1024 * 1024, "Unexpected large file in customer scaffold")
            files[relative.as_posix()] = digest(path)
    require(0 < len(files) <= 100, "Invalid scaffold file count")
    return {"files": files, "sha256": hashlib.sha256(canonical(files)).hexdigest()}


def load_supported(directory, source):
    root = Path(directory).resolve()
    value = validate_project(read_json(root / "foundation.json"))
    expected = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in render(value, source).items()}
    actual = snapshot(root)
    require(actual["files"] == expected, "Scaffold contents differ from this approved recipe. Regenerate supported settings and review the changes.")
    return value, actual
