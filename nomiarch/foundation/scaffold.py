"""Deterministic, bounded scaffolds. Customer data never becomes executable code."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from nomiarch.bootstrap.config import keys, require, validate
from nomiarch.common import NomiarchError, canonical, digest, read_json
from nomiarch.desktop.catalog import CORE_VERSION, PINS
from . import RECIPE_VERSION, SUPPORTED_RECIPE_VERSIONS
from .repository_transport import GHES_MODES, GHES_MANAGED, REVIEW_MODES, certificate_context, server_origin


def slug(value, label):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,39}", value),
            label + " must be 2–40 lowercase letters, digits or hyphens")
    return value


def validate_project(value):
    value = copy.deepcopy(value)
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
    if value['repository']['mode'] == GHES_MANAGED:
        require(config['target'] == 'azure', 'Nomiarch-managed GitHub Enterprise Server currently requires Azure')
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
    keys(repository, {"mode", "owner", "name", "approvers", "server_url", "ca_certificate", "enterprise"}, "repository")
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
        if mode in GHES_MODES:
            require(recipe_version != '0.2.0', 'Internal repository support requires recipe 0.3.0 or later')
            require(repository.get('server_url') == server_origin(repository.get('server_url')), 'Use the canonical internal HTTPS server address')
            if 'ca_certificate' in repository:
                certificate_context(repository['ca_certificate'])
            if mode == GHES_MANAGED:
                enterprise = repository.get('enterprise', {})
                keys(enterprise, {'hostname', 'sizing_profile', 'actions_enabled', 'image_urn', 'vm_size', 'subnet_id'}, 'repository.enterprise')
                require(server_origin('https://' + enterprise.get('hostname', '')) == repository['server_url'],
                        'Managed GHES hostname must match the internal repository address')
                require(enterprise.get('sizing_profile') in {'evaluation', 'small', 'custom'}, 'Choose a supported GHES sizing profile')
                require(type(enterprise.get('actions_enabled')) is bool, 'repository.enterprise.actions_enabled must be a boolean')
                require(isinstance(enterprise.get('image_urn'), str) and enterprise['image_urn'].startswith('GitHub:'),
                        'Select an admitted GitHub Enterprise Server Azure image')
                require(bool(re.fullmatch(r"Standard_[A-Za-z0-9_]+", enterprise.get('vm_size', ''))), 'Select an explicit GHES Azure VM size')
                require(isinstance(enterprise.get('subnet_id'), str) and enterprise['subnet_id'].startswith('/subscriptions/'),
                        'Managed GHES requires an existing private Azure subnet')
            else:
                require('enterprise' not in repository, 'Existing GHES does not use managed deployment settings')
        else:
            require(not {'server_url', 'ca_certificate', 'enterprise'} & set(repository), 'Public GitHub uses its fixed server and system certificate trust')
    else:
        require(set(repository) == {'mode'}, 'Repository account settings require a supported repository mode')
    return repository


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


def render(value, source):
    value = validate_project(value)
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
    else:
        # No shell provisioners or state-only Terraform resource masquerading as VM
        # lifecycle management. Multipass is an explicit controller adapter.
        files[env + "/local.json"] = json_text({"driver": "multipass", "capacity": config["capacity"],
                                              "image_sha256": config["local"]["image_sha256"],
                                              "isolation": value["isolation"]})
    repo_mode = value["repository"]["mode"]
    if repo_mode in REVIEW_MODES:
        files[".github/CODEOWNERS"] = "* " + " ".join("@" + a for a in value["repository"]["approvers"]) + "\n"
    if repo_mode in GHES_MODES and 'ca_certificate' in value['repository']:
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
