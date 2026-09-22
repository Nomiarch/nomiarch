import ipaddress
import os
from pathlib import Path
import re
import uuid

from nomiarch.common import NomiarchError, digest, read_json


def require(condition, message):
    if not condition:
        raise NomiarchError(message)


def keys(value, allowed, label):
    require(isinstance(value, dict), label + " must be an object")
    require(not set(value) - set(allowed), label + " has unknown fields: " + ", ".join(sorted(set(value) - set(allowed))))


def validate(config):
    keys(config, {"api_version", "name", "target", "architecture", "capacity", "local", "azure", "existing", "lifecycle"}, "environment")
    require(config.get("api_version") == "nomiarch.io/v1alpha1", "Unsupported api_version")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{1,19}", config.get("name", ""))), "name must be 2–20 lowercase letters/digits/hyphens")
    target = config.get("target")
    require(target in {"local", "azure", "existing"}, "target must be local, azure, or existing")
    require(config.get("architecture") in {"amd64", "arm64"}, "architecture must be amd64 or arm64")
    for other in {"local", "azure", "existing"} - {target}:
        require(other not in config, "Only the selected target's settings are allowed")
    capacity = config.setdefault("capacity", {"cpus": 4, "memory_gib": 8, "disk_gib": 40})
    keys(capacity, {"cpus", "memory_gib", "disk_gib"}, "capacity")
    for key, minimum, maximum in [("cpus", 2, 64), ("memory_gib", 4, 512), ("disk_gib", 30, 4096)]:
        require(type(capacity.get(key)) is int and minimum <= capacity[key] <= maximum,
                f"capacity.{key} must be an integer from {minimum} to {maximum}")
    lifecycle = config.setdefault("lifecycle", {})
    keys(lifecycle, {"destroy_after", "max_runtime_minutes", "cleanup_worker_id"}, "lifecycle")
    lifecycle.setdefault("destroy_after", False)
    lifecycle.setdefault("max_runtime_minutes", 30)
    require(type(lifecycle["destroy_after"]) is bool, "destroy_after must be a boolean")
    require(type(lifecycle["max_runtime_minutes"]) is int and 5 <= lifecycle["max_runtime_minutes"] <= 240,
            "max_runtime_minutes must be an integer from 5 to 240")
    if "cleanup_worker_id" in lifecycle:
        require(bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", lifecycle["cleanup_worker_id"])), "Invalid cleanup_worker_id")
    if target == "local":
        local = config.get("local", {})
        keys(local, {"image", "image_sha256"}, "local")
        require(isinstance(local.get("image"), str) and local["image"], "local.image must be an admitted Ubuntu 24.04 image path")
        require(bool(re.fullmatch(r"[0-9a-f]{64}", local.get("image_sha256", ""))), "local.image_sha256 is required")
    if target in {"azure", "existing"}:
        details = config.get(target, {})
        if target == "existing":
            keys(details, {"host", "ssh_user", "ssh_key", "known_hosts"}, "existing")
            require(not lifecycle["destroy_after"], "destroy-after is prohibited for adopted/existing hosts")
            require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", details.get("host", ""))), "Invalid existing.host")
            require(isinstance(details.get("known_hosts"), str), "existing.known_hosts is required; enroll the host out of band")
        else:
            keys(details, {"subscription_id", "location", "vm_size", "ssh_user", "ssh_key", "ssh_public_key", "admin_cidr", "subnet_id", "image", "vnet_cidr", "subnet_cidr"}, "azure")
            try:
                uuid.UUID(details.get("subscription_id", ""))
            except (ValueError, AttributeError):
                raise NomiarchError("A valid Azure subscription UUID is required")
            require(config["architecture"] == "amd64", "Azure reference recipe currently supports amd64 only")
            require(bool(re.fullmatch(r"[a-z0-9]+", details.get("location", ""))), "Azure location is required")
            require(bool(re.fullmatch(r"Standard_[A-Za-z0-9_]+", details.get("vm_size", ""))), "An explicit Azure vm_size is required")
            require(isinstance(details.get("ssh_public_key"), str), "Azure ssh_public_key file is required")
            admin = ipaddress.ip_network(details.get("admin_cidr", ""), strict=True)
            require(admin.version == 4 and admin.prefixlen >= 8, "admin_cidr must be a scoped IPv4 management range; /0 is prohibited")
            image = details.get("image", {})
            keys(image, {"publisher", "offer", "sku", "version"}, "azure.image")
            require(all(isinstance(image.get(k), str) and re.fullmatch(r"[A-Za-z0-9._-]+", image[k]) for k in ("publisher", "offer", "sku", "version")), "Azure image publisher/offer/sku/exact version are required")
            require(bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", image["version"])), "Resolve an exact numeric Azure image version before applying")
            if details.get("subnet_id"):
                require(bool(re.fullmatch(r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/providers/Microsoft.Network/virtualNetworks/[^/]+/subnets/[^/]+", details["subnet_id"])), "Invalid adopted subnet resource ID")
            else:
                network = ipaddress.ip_network(details.setdefault("vnet_cidr", "10.88.0.0/16"))
                subnet = ipaddress.ip_network(details.setdefault("subnet_cidr", "10.88.1.0/24"))
                require(network.version == 4 and subnet.version == 4 and subnet.subnet_of(network), "subnet_cidr must fit inside vnet_cidr")
        require(bool(re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", details.get("ssh_user", ""))), "Invalid ssh_user")
        require(isinstance(details.get("ssh_key"), str) and details["ssh_key"], "ssh_key file reference is required")
    return config


def load(path):
    path = Path(path).expanduser().resolve()
    config = validate(read_json(path))
    details = config[config["target"]]
    for key in ("image",) if config["target"] == "local" else ("ssh_key", "ssh_public_key", "known_hosts"):
        if key in details:
            p = Path(details[key]).expanduser()
            details[key] = str(p.resolve() if p.is_absolute() else (path.parent / p).resolve())
    return config


def files_preflight(config):
    details = config[config["target"]]
    if config["target"] == "local":
        require(Path(details["image"]).is_file(), "Local image is missing")
        require(digest(details["image"]) == details["image_sha256"], "Local image checksum mismatch")
    else:
        for name in ("ssh_key", "ssh_public_key", "known_hosts"):
            if name in details:
                require(Path(details[name]).is_file(), f"Missing {name} file: {details[name]}")
        if os.name != "nt":
            require(Path(details["ssh_key"]).stat().st_mode & 0o077 == 0, "SSH private key permissions must be 0600 or stricter")
