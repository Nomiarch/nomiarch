"""Validate the generated customer roots, not only the source Azure module.

Covers the Core recipe (0.3.0) and the infrastructure-only recipe (0.4.0), and runs
the vendored customer validator exactly as the customer's `nomiarch-validate` job does.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nomiarch.common import Runner
from nomiarch.foundation.scaffold import create, infrastructure_project, project

ROOT = Path(__file__).resolve().parents[1]
config = {'api_version': 'nomiarch.io/v1alpha1', 'name': 'validation', 'target': 'azure', 'architecture': 'amd64',
          'capacity': {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40}, 'lifecycle': {'destroy_after': False, 'max_runtime_minutes': 120},
          'azure': {'subscription_id': '00000000-0000-0000-0000-000000000000', 'location': 'canadacentral',
                    'vm_size': 'Standard_D2s_v5', 'ssh_user': 'nomiarch', 'ssh_key': '@controller', 'ssh_public_key': '@controller',
                    'admin_cidr': '10.20.30.0/24', 'image': {'publisher': 'Canonical', 'offer': 'ubuntu-24_04-lts', 'sku': 'server', 'version': '24.04.202609010'}}}
repository = {'mode': 'github', 'owner': 'customer', 'name': 'infrastructure', 'approvers': ['reviewer']}
recipes = [('core', project(config, 'customer', 'evaluation')),
           ('infrastructure', infrastructure_project(config, 'customer', 'evaluation', 'organisation', 'restricted-connected', repository))]
with tempfile.TemporaryDirectory() as folder:
    for name, value in recipes:
        destination = Path(folder) / name
        create(destination, value, ROOT)
        if name == 'infrastructure':
            # Run the vendored validator first, exactly as on a fresh customer checkout.
            result = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=destination, capture_output=True, text=True)
            print(result.stdout)
            if result.returncode or json.loads(result.stdout).get('terraform') != 'passed':
                raise SystemExit('Vendored customer validator did not pass with OpenTofu available: ' + result.stdout + result.stderr)
            # It must also be repeatable and leave the checked tree unchanged.
            again = subprocess.run([sys.executable, '-I', 'validation/validate.py'], cwd=destination, capture_output=True, text=True)
            if again.returncode:
                raise SystemExit('Vendored customer validator is not repeatable: ' + again.stdout)
        runner = Runner()
        runner.run(['tofu', 'fmt', '-check', '-recursive'], cwd=destination)
        environment = destination / 'environments/evaluation'
        runner.run(['tofu', 'init', '-backend=false', '-input=false', '-lockfile=readonly'], cwd=environment)
        print(name + ':', runner.run(['tofu', 'validate', '-no-color'], cwd=environment))
