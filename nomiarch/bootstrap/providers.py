"""Provisioning identities and OpenTofu state stay on the controller."""
import json
from pathlib import Path
import shlex
import shutil
import time

from nomiarch.common import NomiarchError, atomic_write, read_json, write_json


class Provider:
    def __init__(self, config, run, directory, runner, repo):
        self.config, self.run, self.directory, self.runner = config, run, Path(directory), runner
        self.repo = Path(repo)

    def command(self, args, **kwargs):
        return self.runner.run(args, **kwargs)

    def prepare(self):
        pass


class Local(Provider):
    def names(self):
        return json.loads(self.command(["multipass", "list", "--format", "json"]))["list"]

    def prepare(self):
        if any(v["name"] == self.run["prefix"] for v in self.names()):
            raise NomiarchError("Disposable VM name already exists; refusing adoption")

    def provision(self):
        capacity = self.config["capacity"]
        self.command(["multipass", "launch", Path(self.config["local"]["image"]).as_uri(),
                      "--name", self.run["prefix"], "--cpus", capacity["cpus"],
                      "--memory", str(capacity["memory_gib"]) + "G", "--disk", str(capacity["disk_gib"]) + "G"], timeout=1800)
        return {"host": self.run["prefix"], "mode": "local-restricted", "data_device": None}

    def verify(self):
        data = json.loads(self.command(["multipass", "info", self.run["prefix"], "--format", "json"]))
        if data["info"][self.run["prefix"]]["state"] != "Running":
            raise NomiarchError("Multipass VM is not running")
        self.remote(["cloud-init", "status", "--wait"], timeout=300)
        return {"vm": "running", "cloud_init": "complete"}

    def remote(self, args, **kwargs):
        return self.command(["multipass", "exec", self.run["prefix"], "--", *args], **kwargs)

    def transfer(self, source, destination):
        self.command(["multipass", "transfer", source, self.run["prefix"] + ":" + destination], timeout=1800)

    def fetch(self, source, destination):
        self.command(["multipass", "transfer", self.run["prefix"] + ":" + source, destination], timeout=1800)

    def destroy(self):
        if self.run["prefix"] not in {v["name"] for v in self.names()}:
            return {"status": "deleted", "verified_absent_at": time.time()}
        # Purge is scoped to this exact run's name, never `multipass purge`.
        self.command(["multipass", "delete", "--purge", self.run["prefix"]], timeout=300)
        if self.run["prefix"] in {v["name"] for v in self.names()}:
            raise NomiarchError("Multipass still lists the disposable VM")
        return {"status": "deleted", "verified_absent_at": time.time()}


class SSH(Provider):
    def ssh_args(self):
        details = self.config[self.config["target"]]
        hosts = details.get("known_hosts", str(self.directory / "known_hosts"))
        return ["-i", details["ssh_key"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + hosts,
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]

    def endpoint(self):
        return self.config[self.config["target"]]["ssh_user"] + "@" + self.run["inventory"]["host"]

    def remote(self, args, **kwargs):
        # OpenSSH's remote command is a shell string; quote every argument.
        return self.command(["ssh", *self.ssh_args(), self.endpoint(), shlex.join([str(a) for a in args])], **kwargs)

    def transfer(self, source, destination):
        self.command(["scp", *self.ssh_args(), str(source), self.endpoint() + ":" + destination], timeout=1800)

    def fetch(self, source, destination):
        self.command(["scp", *self.ssh_args(), self.endpoint() + ":" + source, destination], timeout=1800)


class Existing(SSH):
    def provision(self):
        return {"host": self.config["existing"]["host"], "mode": "existing-site-boundary", "data_device": None}

    def verify(self):
        self.remote(["sudo", "-n", "true"])
        return {"management": "authenticated SSH and authorized sudo"}

    def destroy(self):
        raise NomiarchError("Existing hosts cannot be destroyed by Nomiarch")


class Azure(SSH):
    def az(self, args, timeout=600):
        return json.loads(self.command(["az", *args, "--subscription", self.config["azure"]["subscription_id"],
                                        "--output", "json", "--only-show-errors"], timeout=timeout) or "null")

    def group_exists(self):
        value = self.az(["group", "exists", "--name", self.run["prefix"] + "-rg"])
        if type(value) is not bool:
            raise NomiarchError("Azure did not return a conclusive existence result")
        return value

    def prepare(self):
        details = self.config["azure"]
        account = self.az(["account", "show"])
        if account["id"].lower() != details["subscription_id"].lower():
            raise NomiarchError("Azure subscription selection mismatch")
        if self.group_exists():
            raise NomiarchError("Run resource group already exists; refusing adoption")
        for namespace in ["Microsoft.Compute", "Microsoft.Network"]:
            status = self.az(["provider", "show", "--namespace", namespace])
            if status.get("registrationState") != "Registered":
                raise NomiarchError(namespace + " must already be registered in the selected subscription")
        skus = self.az(["vm", "list-skus", "--location", details["location"], "--size", details["vm_size"], "--resource-type", "virtualMachines", "--all"])
        exact = [s for s in skus if s["name"] == details["vm_size"]]
        if not exact:
            raise NomiarchError("Selected Azure VM size was not found in the region")
        caps = {c["name"]: c["value"] for c in exact[0].get("capabilities", [])}
        if float(caps.get("vCPUs", 0)) < self.config["capacity"]["cpus"] or float(caps.get("MemoryGB", 0)) < self.config["capacity"]["memory_gib"]:
            raise NomiarchError("Azure VM size does not meet the declared CPU/memory capacity")
        work = self.directory / "infra"
        shutil.copytree(self.repo / "infra/azure", work, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "*.tfplan", "*.auto.tfvars.json"))
        key = self.directory / "ssh-host"
        self.command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
        variables = {k: details[k] for k in ("subscription_id", "location", "vm_size", "ssh_user", "admin_cidr", "image")}
        variables.update({"prefix": self.run["prefix"], "run_id": self.run["id"],
                          "expires_at": str(self.run["expires_at"]) if self.run["destroy_after"] else "retained",
                          "ssh_public_key": Path(details["ssh_public_key"]).read_text().strip(),
                          "ssh_host_private_key": key.read_text(), "ssh_host_public_key": key.with_suffix(".pub").read_text().strip(),
                          "disk_gib": self.config["capacity"]["disk_gib"]})
        variables.update({k: details[k] for k in ("subnet_id", "vnet_cidr", "subnet_cidr") if k in details})
        write_json(work / "environment.auto.tfvars.json", variables)
        self.command(["tofu", "init", "-input=false"], cwd=work, timeout=600)
        self.command(["tofu", "validate"], cwd=work)
        self.command(["tofu", "plan", "-input=false", "-out=apply.tfplan"], cwd=work, timeout=600)

    def provision(self):
        work = self.directory / "infra"
        self.command(["tofu", "apply", "-input=false", "apply.tfplan"], cwd=work, timeout=1800)
        inventory = json.loads(self.command(["tofu", "output", "-json", "inventory"], cwd=work))
        hostkey = (self.directory / "ssh-host.pub").read_text().strip()
        atomic_write(self.directory / "known_hosts", inventory["host"] + " " + hostkey + "\n")
        return inventory

    def verify(self):
        vm_id = self.run["inventory"]["vm_id"]
        instance = self.az(["vm", "get-instance-view", "--ids", vm_id])
        states = {s["code"].lower() for s in instance.get("instanceView", instance).get("statuses", [])}
        if "powerstate/running" not in states:
            raise NomiarchError("Azure VM is not observed running")
        return {"vm": "running", "private_ip": self.run["inventory"]["host"],
                "mode": "azure-restricted; Azure platform dependencies remain"}

    def allowed_ids(self):
        prefix = self.run["prefix"]
        base = f"/subscriptions/{self.config['azure']['subscription_id']}/resourceGroups/{prefix}-rg/providers/"
        resources = [("Microsoft.Compute/virtualMachines", "-vm"), ("Microsoft.Compute/disks", "-os"),
                     ("Microsoft.Compute/disks", "-data"), ("Microsoft.Network/networkInterfaces", "-nic"),
                     ("Microsoft.Network/networkSecurityGroups", "-nsg")]
        if not self.config["azure"].get("subnet_id"):
            resources += [("Microsoft.Network/virtualNetworks", "-vnet"),
                          ("Microsoft.Network/virtualNetworks", "-vnet/subnets/core")]
        return {(base + kind + "/" + prefix + suffix).lower() for kind, suffix in resources}

    def assert_cleanup_scope(self, group, inventory):
        prefix = self.run["prefix"]
        expected = f"/subscriptions/{self.config['azure']['subscription_id']}/resourceGroups/{prefix}-rg".lower()
        if group.get("id", "").lower() != expected or group.get("tags", {}).get("nomiarch-run") != self.run["id"]:
            raise NomiarchError("Resource group identity/ownership does not match the recorded run")
        allowed = self.allowed_ids()
        vm_id = expected + "/providers/microsoft.compute/virtualmachines/" + prefix + "-vm"
        for resource in inventory:
            rid = resource.get("id", "").lower()
            if rid not in allowed:
                raise NomiarchError("Cleanup blocked by an unowned resource: " + resource.get("id", "unknown"))
            if resource.get("tags", {}).get("nomiarch-run") == self.run["id"]:
                if rid.endswith("/" + prefix + "-vnet"):
                    network = self.az(["network", "vnet", "show", "--ids", resource["id"]])
                    if network.get("virtualNetworkPeerings") or any(s["name"] != "core" for s in network.get("subnets", [])):
                        raise NomiarchError("Cleanup blocked by unowned VNet peerings or subnets")
                continue
            if rid.endswith("-vnet/subnets/core"):
                continue  # Explicit child of this run's dedicated VNet.
            if rid.endswith("/" + prefix + "-os"):
                disk = self.az(["disk", "show", "--ids", resource["id"]])
                if (disk.get("managedBy") or "").lower() == vm_id:
                    continue  # Azure creates this named disk as part of the owned VM.
            raise NomiarchError("Cleanup blocked: resource ownership cannot be established: " + resource["id"])

    def destroy(self):
        group_name = self.run["prefix"] + "-rg"
        if not self.group_exists():
            # Reconcile accepted/in-flight resource operations after cancellation.
            time.sleep(5)
            if not self.group_exists():
                return {"status": "deleted", "verified_absent_at": time.time()}
        group = self.az(["group", "show", "--name", group_name])
        inventory = self.az(["resource", "list", "--resource-group", group_name])
        self.assert_cleanup_scope(group, inventory)
        # This is an explicitly new, run-owned group. Shared subnets are outside it.
        # No lock removal, forced deletion, or prefix/tag-only resource sweep.
        self.command(["az", "group", "delete", "--name", group_name, "--subscription",
                      self.config["azure"]["subscription_id"], "--yes", "--no-wait", "--only-show-errors"], timeout=120)
        self.command(["az", "group", "wait", "--name", group_name, "--subscription",
                      self.config["azure"]["subscription_id"], "--deleted", "--timeout", "1800", "--interval", "10"], timeout=1850)
        if self.group_exists():
            raise NomiarchError("Azure still reports the resource group after deletion")
        return {"status": "deleted", "verified_absent_at": time.time(), "owned_group": group["id"]}


def get_provider(config, run, directory, runner, repo):
    return {"local": Local, "azure": Azure, "existing": Existing}[config["target"]](config, run, directory, runner, repo)
