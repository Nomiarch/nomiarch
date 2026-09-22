"""Validate the generated customer root, not only the source Azure module."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nomiarch.common import Runner
from nomiarch.foundation.scaffold import create, project

config = {'api_version': 'nomiarch.io/v1alpha1', 'name': 'validation', 'target': 'azure', 'architecture': 'amd64',
          'capacity': {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40}, 'lifecycle': {'destroy_after': False, 'max_runtime_minutes': 120},
          'azure': {'subscription_id': '00000000-0000-0000-0000-000000000000', 'location': 'canadacentral',
                    'vm_size': 'Standard_D2s_v5', 'ssh_user': 'nomiarch', 'ssh_key': '@controller', 'ssh_public_key': '@controller',
                    'admin_cidr': '10.20.30.0/24', 'image': {'publisher': 'Canonical', 'offer': 'ubuntu-24_04-lts', 'sku': 'server', 'version': '24.04.202609010'}}}
with tempfile.TemporaryDirectory() as folder:
    destination = Path(folder) / 'customer'
    create(destination, project(config, 'customer', 'evaluation'), Path(__file__).resolve().parents[1])
    runner = Runner()
    runner.run(['tofu', 'fmt', '-check', '-recursive'], cwd=destination)
    environment = destination / 'environments/evaluation'
    runner.run(['tofu', 'init', '-backend=false', '-input=false', '-lockfile=readonly'], cwd=environment)
    print(runner.run(['tofu', 'validate', '-no-color'], cwd=environment))
