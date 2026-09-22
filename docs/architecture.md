# v0.1.0 bootstrap and core architecture

## Product contract

Nomiarch is intended to establish a customer-controlled environment for private AI and
infrastructure workflows. Bootstrap creates or adopts the foundation, installs the portable
core, maintains it, and provides a separate recovery route. The core's intelligence is not
the authority that grants infrastructure access.

The first two placement targets are local/on-premises and Azure. Each is independently
operable. Reusing an environment template or signing key does not synchronize their data,
service credentials, cloud states, or failures. AWS, GCP, and named Canadian providers will
be additional provisioning adapters, not core forks.

## Decisions implemented in this preview

| Decision | Reason and consequence |
| --- | --- |
| One monorepo, separate service processes | Shared contracts and release versions; independent service deployment boundaries |
| One dedicated Ubuntu 24.04 guest | Small initial operational footprint; one failure and host-administrator trust boundary |
| K3s with direct offline image import | Supplies orchestration before the core exists; no existing registry or Kubernetes required |
| Signed tar bundle with OpenSSL RSA/SHA-256 | Uses a small prepositioned trust dependency; validates all artifacts before execution; key enrollment remains an operator responsibility |
| Python standard library controller/runtime | Development demo and guest installer do not need an online Python package installer |
| JSON environment declarations | Strict, explicit types without a dependency bootstrap loop; same schema for every target; YAML input may be added later |
| OpenTofu 1.12.6 reference runner and AzureRM 4.49.0 | Versioned Azure recipe with a committed provider lock; state remains on the management controller |
| Dedicated private Azure VM and data disk | No public endpoint; existing private subnet can supply management reachability; new VNet supports foundation-only testing |
| Core-owned SQLite | Durable jobs and evidence without exposing a shared writable database to agents |
| OPA sidecar in the gateway pod | Only the gateway can call the policy service over its loopback interface; workers cannot change policy through the API |
| Local llama.cpp service | Small admitted GGUF model, no cloud inference dependency, no tool authority in the model |
| No arbitrary execution tool | First worker only evaluates submitted configuration and summarizes findings |
| Encrypted ZIP backup using age | Includes a consistent database snapshot, identities, and complete active release; recovery secrets are separate |

Zarf remains a packaging candidate. The first implementation uses the documented K3s
air-gap path directly; adding another packaging layer requires demonstrating a concrete
operational benefit and testing its offline/upgrade behavior.

## Control and data flow

1. An administrator prepares and admits release artifacts on a connected staging machine.
2. The independent bootstrap validates configuration and inventories the intended resources.
3. A provider creates a disposable/retained foundation or adopts a dedicated existing host.
4. The installer verifies the bundle, configures local identities/storage, imports K3s and
   workload images, and applies the common runtime with network policies.
5. An operator submits a small configuration object to the core API with an operator identity.
6. The worker leases a durable task, calls the gateway's approved read-only tools, and returns
   findings plus a local-model explanation. Policy and audit persistence precede tool execution.
7. The core records completion atomically with an evidence event. Expired leases recover after
   worker loss; stale workers cannot replace a later completion.

The sample checks `public_access`, `https_only`, and `encryption_enabled` on **submitted data**.
It does not claim to discover a real cloud vulnerability or modify a resource. The larger
observe/authorize/remediate/verify workflow is the next product increment after the foundation
and offline release gates pass.

## Identities and exposure

The operator, worker, and evidence gateway have distinct generated bearer credentials.
The worker cannot submit operator tasks, read the operator evidence endpoint, acquire a
Kubernetes service-account token, access the cloud controller's credentials, or call a
foundation lifecycle endpoint. There is no runtime foundation-destroy API.

The guest API is exposed only on guest loopback through a systemd-managed Kubernetes
port forward. An administrator may use an authenticated SSH tunnel to it. HTTP between
services is restricted to the single guest/cluster boundary in this preview. Distributed
workers will require authenticated encrypted service transport and stronger isolation;
the current token/network scheme is not a multi-tenant or multi-host security claim.

The chained evidence log detects changes relative to a trusted retained checkpoint. A host
administrator can replace the database and regenerate the chain, so it is not independently
immutable evidence. Export checkpoints outside the host when evaluating stronger assurance.
The local database uses ordinary filesystem storage. Host/volume encryption must be
provided by the surrounding platform before customer data is admitted; this preview does
not configure local full-disk encryption.

## Lifecycle and cleanup

Run directories are created outside the guest before resource creation. They hold the exact
subscription, run ID, unique names, expiry, IaC state, generated host identity, phase, inventory,
and separate validation/cleanup results. Files are private and excluded from Git.

Destroy-after applies only to newly created local/Azure foundations. The Azure implementation
checks the exact subscription/group identity, recorded run tag, allowed resource IDs, and
ownership of the automatically created OS disk before requesting whole-group deletion.
Foreign resources, added VNet peerings/subnets, or uncertain ownership stop deletion. Azure
deletion is asynchronous: an accepted request does not count as success until the group is
observed absent. In-flight creation and cloud-control-plane failures remain operational risks.

An existing-host adapter has no destruction operation. A local purge is scoped to one exact
recorded Multipass VM. No global purge, tag-only sweep, lock removal, or adopted-subnet
destruction is performed. Backup/evidence errors cannot intentionally defer teardown forever.

The independent reaper reads the same protected run records. Advisory locks prevent the
controller and reaper from managing the same run concurrently. Shared-storage semantics,
clock synchronization, worker authority, independent failure domains, and controller-loss
recovery must be validated on the actual deployment. A hostname/heartbeat check alone is
not proof of independence or a guarantee against charges.

## Offline and recovery boundaries

The signed release contains the model, container images, K3s binary/image archive/install
script, OPA in the application image, installer code, policy, age, and resolved inventory.
The supported OS, hypervisor, root authority, OpenSSL/Python/systemd, and basic OS tools
are prerequisites or admitted guest-image contents. Azure's management API and platform
agent remain provider dependencies; Azure is not described as physically air-gapped.

Application network policies deny new connections except named service and internal DNS
paths. CoreDNS has no external forwarding, and its boot configuration does not inherit a
connected host's external resolver. Workload images use `imagePullPolicy: Never`. A wildcard
registry mirror points to an unavailable loopback endpoint, with default registry fallback
disabled, so missing system images cannot trigger upstream downloads. Installation must
fail on missing bytes rather than reconnect to finish downloading.
After initial DNS admission, a K3s `coredns.yaml.skip` file preserves these existing addon
resources and prevents restart from restoring the packaged forwarding configuration.
CoreDNS/K3s version changes therefore require a separately qualified migration.

Backups preserve the active application release and core state, not arbitrary VM/OS state.
The reference restore reconstructs the runtime on a fresh admitted guest. The installed
core's schema is currently version 1; automatic K3s migrations are deliberately refused.
Production release still requires reboot, offline first-install, failed-update recovery,
clean-host restore, and dual-target parity evidence.

## Sources used for the implementation

- [K3s offline installation and upgrade](https://docs.k3s.io/installation/airgap)
- [K3s registry mirrors and default-endpoint behavior](https://docs.k3s.io/installation/private-registry)
- [K3s packaged-component ownership and skip files](https://docs.k3s.io/installation/packaged-components)
- [Multipass image launch](https://canonical.com/multipass/docs/latest/reference/command-line-interface/launch/)
- [AzureRM 4.49.0 Linux VM documentation](https://github.com/hashicorp/terraform-provider-azurerm/blob/v4.49.0/website/docs/r/linux_virtual_machine.html.markdown)
- [AzureRM 4.49.0 subnet documentation](https://github.com/hashicorp/terraform-provider-azurerm/blob/v4.49.0/website/docs/r/subnet.html.markdown)
- [Azure resource-group deletion behavior](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/delete-resource-group)
- [cloud-init SSH key configuration](https://docs.cloud-init.io/en/latest/reference/modules.html#ssh)
- [age encryption format and tooling](https://age-encryption.org/)
