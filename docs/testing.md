# Test locally and on Azure

Start with the local development test, then Azure foundation creation/deletion, then the full
offline bundle. These are different test scopes and produce different evidence.

## 1. Quick local test

Use Python 3.11+ on macOS/Linux, or WSL2 on Windows. Run from the repository root:

```bash
python3 -m nomiarch dev demo
python3 -m unittest discover -s tests -v
```

Expected demo result: task `completed`, two failing sample controls, one passing control,
an evidence checkpoint, unauthenticated access `denied`, and forbidden tool `denied`.
The services stop when the demo finishes; `.nomiarch/dev/data/core.db` and
`.nomiarch/dev/demo-report.json` remain. Run the command again to exercise the same durable store.
Tests for encrypted recovery require `age` and `age-keygen`; they report a skip if those tools are absent.

To leave the local development services running:

```bash
python3 -m nomiarch dev up
```

The API listens at `http://127.0.0.1:8787`. Ctrl-C stops the services and preserves data.
This harness is a development convenience. Its policy adapter is in process with the gateway,
and its default explanation is deterministic. It does not provide VM or container isolation.

To use a real locally installed llama.cpp server, start it with an admitted small GGUF model:

```bash
llama-server --model /absolute/path/model.gguf --alias local --host 127.0.0.1 --port 8080 --ctx-size 2048
python3 -m nomiarch dev demo --model-url http://127.0.0.1:8080
```

The report must now say `local-model`. No hosted inference fallback exists. Model download
and inference are separate steps; take local copies before disconnecting the machine.

## 2. Prepare a signed offline release

On a connected staging machine install Python 3.11+, Git, OpenSSL, and Docker with the
daemon running. Docker must support the selected Linux CPU architecture. This is the
only release-building step that downloads dependencies. The guest installer does not
invoke a package repository, registry, or model hub.

Generate a release signing key in a private directory:

```bash
python3 -m nomiarch bundle keygen \
  --private .nomiarch/keys/release-private.pem \
  --public .nomiarch/keys/release-public.pem
```

Keep the private key on the trusted signing machine. Enroll the public key inside the
destination using a separately trusted channel. A public key delivered alongside an
untrusted bundle is not, by itself, a trust anchor.

Obtain a small CPU GGUF model and record its publisher revision, SHA-256, and license.
The reference sizing is for a small model such as Qwen2.5 0.5B Q4_K_M, not an arbitrary
large model. The model container is capped at 2 GiB RAM. The
[publisher's model repository](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF)
provides GGUF files and licensing information.

For the pinned reference model, the connected helper downloads and verifies its exact
publisher revision (about 491 MB). Model weights are not stored in Git:

```bash
python3 scripts/fetch_model.py .nomiarch/model.gguf
```

```bash
python3 -m nomiarch bundle prepare \
  --arch amd64 \
  --model .nomiarch/model.gguf \
  --model-sha256 74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db \
  --signing-key .nomiarch/keys/release-private.pem \
  --output .nomiarch/nomiarch-amd64.tar

python3 -m nomiarch bundle verify \
  --bundle .nomiarch/nomiarch-amd64.tar \
  --trusted-key .nomiarch/keys/release-public.pem
```

Preparation downloads the pinned K3s/OPA/age inputs, resolves the Python and llama.cpp
image tags to content digests, builds the core image, and records all resolved inputs in
`components.lock.json` inside the signed archive. For reproducible subsequent builds,
pass the admitted `--python-image NAME@sha256:DIGEST` and
`--model-image NAME@sha256:DIGEST` from that inventory. Never treat a moving tag as a
production admission decision. Image architecture mismatches stop preparation.

An Apple Silicon guest requires a separately prepared `--arch arm64` release and Ubuntu
arm64 image. Azure uses the amd64 release. Architecture-specific paths exist in code;
the compatibility combinations remain unqualified until tested on the actual hosts.

## 3. Full local VM

Install Multipass on the host. Obtain an Ubuntu 24.04 cloud image for the chosen architecture,
verify it against the publisher's signed checksum information, and retain it locally.
See [Ubuntu's cloud image distribution](https://cloud-images.ubuntu.com/noble/) and
[Multipass local image support](https://canonical.com/multipass/docs/latest/reference/command-line-interface/launch/).
The local adapter uses `file://`; it does not silently replace a missing local image with a download.

```bash
python3 -m nomiarch bootstrap init --target local --output env.local.json
python3 -m nomiarch bootstrap plan -f env.local.json
python3 -m nomiarch bootstrap apply -f env.local.json \
  --bundle .nomiarch/nomiarch-amd64.tar \
  --trusted-key .nomiarch/keys/release-public.pem
```

The wizard asks for the image path, architecture, and environment name. It records the
local file hash; independently verify publisher authenticity before admitting that hash.
Default capacity is 2 vCPU, 8 GiB memory, and 40 GiB disk. Provisioning creates one
uniquely named VM. The full run installs K3s and the same core services used in Azure,
runs a real-model task, and probes blocked worker Internet/metadata/DNS access.

Use the run directory printed by `apply` for later operations. Add `--destroy-after`
to the apply command if this VM is only a disposable test. To test VM creation without
building a release first, use `--scope foundation --destroy-after` instead.

## Azure

### Prerequisites and exact image selection

Use a dedicated lab subscription or authorized lab scope. The controller needs Azure CLI,
OpenTofu **1.12.6** (reference engine), Python, OpenSSL, and OpenSSH. The recipe pins
AzureRM **4.49.0** and carries its provider lock. No Azure credentials are embedded in
the repository, release bundle, guest, or model containers.

The operator needs authority to create/delete the new test resource group and its
compute/network/disk resources. `Microsoft.Compute` and `Microsoft.Network` must already
be registered. Adoption of an existing subnet additionally needs the appropriate read/join
permission. Nomiarch does not register providers, grant roles, or modify a shared subnet.

```bash
az login
az account show --output table
```

List available Ubuntu 24.04 image versions in your selected subscription/region:

```bash
az vm image list \
  --subscription YOUR_SUBSCRIPTION_UUID \
  --location canadacentral \
  --publisher Canonical \
  --offer ubuntu-24_04-lts \
  --sku server \
  --all --query "[].{version:version,urn:urn}" --output table

python3 -m nomiarch bootstrap init --target azure --output env.azure.json
```

Enter an exact listed image version in the wizard; `latest` is rejected. The starting VM
size is `Standard_B2ms`, subject to your region's availability, quota, and image compatibility.
The controller checks the declared CPU/memory against the selected SKU. This SCSI-disk
recipe does not establish support for every Azure VM family.

### No-resource validation

```bash
python3 -m nomiarch bootstrap plan -f env.azure.json
python3 -m nomiarch bootstrap plan -f env.azure.json --provider-plan
```

The first command validates the environment and prints the intended shape. The second
authenticates to Azure, checks prerequisites, initializes the provider, and saves an actual
OpenTofu plan. Neither provisions a VM or resource group. State, generated SSH host keys,
and plan logs are written to the private run directory; do not commit or share these files.
To inspect the saved detailed plan, use `tofu -chdir=RUN_DIRECTORY/infra show apply.tfplan`.

### Actual creation test with automatic cleanup

```bash
python3 -m nomiarch bootstrap apply -f env.azure.json \
  --scope foundation --destroy-after
```

This creates a new resource group, VM, NSG, NIC, OS disk, and persistent data disk.
With no `azure.subnet_id`, it also creates a dedicated VNet/subnet. No public IP, NAT gateway,
Bastion, AKS, managed database, or hosted AI endpoint is created. It checks the VM's state
through Azure's management API, exports the result to the controller, then requests scoped
resource-group deletion and waits for observed absence.

Expected output has **both** `validation.status: passed` and `cleanup.status: deleted`.
This proves foundation creation/deletion only; it does not claim that the core was installed.
Cloud usage can be charged until deletion completes, and deletion does not reverse charges
already incurred. Microsoft's [VM billing guidance](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing)
explains the distinction between compute state and charges for retained resources.

Keep your terminal and computer running for an attended test. Ctrl-C/SIGTERM, handled
failures, and command deadlines enter cleanup. Abrupt power loss, SIGKILL, lost Azure
access, deletion locks, or an Azure outage can leave resources behind. The 30-minute
default is a cleanup trigger, not a hard billing ceiling.

```bash
python3 -m nomiarch bootstrap status --run RUN_DIRECTORY
python3 -m nomiarch bootstrap destroy --run RUN_DIRECTORY
```

`destroy` retries the exact recorded run. It does not sweep similarly named/tagged groups,
remove locks, or delete an adopted subnet. An uncertain/foreign resource blocks group deletion.
Retain the run directory until actual deletion has been confirmed.

### Full Azure core test

Use a controller on an existing private administration route, such as an authorized
management VM or a workstation connected by VPN. Put that route's existing subnet ID
in `azure.subnet_id`, and its source range in `azure.admin_cidr`. The subnet must have
space for the new NIC, and existing routing/NSGs must permit the scoped SSH connection.
The installer does not create a VPN or bridge the laptop into a private VNet.

```bash
python3 -m nomiarch bootstrap apply -f env.azure.json \
  --scope core \
  --bundle .nomiarch/nomiarch-amd64.tar \
  --trusted-key .nomiarch/keys/release-public.pem \
  --destroy-after
```

This runs the same guest installer and verification as the local VM. The SSH host identity
is generated by the controller and injected through cloud-init; subsequent SSH uses a
per-run `known_hosts` file with strict checking. No unverified `ssh-keyscan` enrollment or
automatic host-key acceptance is used. Sensitive host key material remains in the
protected bootstrap/IaC state and Azure provisioning payload.

For a persistent installation set `lifecycle.destroy_after` to `false` in the configuration
and omit the command-line flag. Omitting the flag alone does not override a `true` config value.

## Existing on-premises guest

Use a dedicated Ubuntu 24.04 VM/server, systemd, matching CPU architecture, the required
prepositioned OS tools, private SSH administration, and authorized noninteractive sudo.
Enroll its SSH host key through your existing trusted process. Existing Nomiarch data is
preserved; an unrelated K3s installation is rejected. A site may also run the bundled
installer directly as root from approved media, without a remote controller.

```bash
python3 -m nomiarch bootstrap init --target existing --output env.onprem.json
python3 -m nomiarch bootstrap apply -f env.onprem.json \
  --bundle .nomiarch/nomiarch-amd64.tar \
  --trusted-key .nomiarch/keys/release-public.pem
```

The bootstrap owns its new K3s installation and Nomiarch files on this dedicated host.
It is not a coexistence installer for a shared enterprise Kubernetes node. `--destroy-after`
and host destruction are rejected for this target. Physical air-gap qualification still
requires isolated host/management networking and a fresh-cache, disconnected install test.

## Upgrade, repair, and recovery

Create an age recovery key on a separate trusted administration machine:

```bash
age-keygen -o /secure/location/nomiarch-recovery-key.txt
age-keygen -y /secure/location/nomiarch-recovery-key.txt
```

The second command prints the recipient public key, starting with `age1`. Keep the
private recovery identity separately from the encrypted archive. It is independent of
the release signing key.

```bash
python3 -m nomiarch bootstrap backup --run RUN_DIRECTORY \
  --backup-recipient YOUR_AGE_RECIPIENT

python3 -m nomiarch bootstrap upgrade --run RUN_DIRECTORY \
  --bundle /absolute/path/new-signed-release.tar \
  --trusted-key .nomiarch/keys/release-public.pem \
  --backup-recipient YOUR_AGE_RECIPIENT

python3 -m nomiarch bootstrap repair --run RUN_DIRECTORY \
  --bundle /absolute/path/current-signed-release.tar \
  --trusted-key .nomiarch/keys/release-public.pem
```

Backup copies are retained in the controller's run directory with SHA-256 receipts.
Upgrade first obtains a controller backup, then quiesces guest writers for another snapshot
before replacing the core. It preserves identities and the private database. This preview
supports the existing K3s version and database schema; incompatible K3s migrations are refused.
Downtime is expected. An interrupted/failed upgrade remains an incomplete operation: inspect
the journal/logs and restore the snapshot or repair the current release; no automatic successful
rollback is claimed. A failed post-upgrade backup transfer is reported separately and exits nonzero.

Restore a development backup into a new directory, using the checksum from your separately
retained receipt:

```bash
python3 -m nomiarch backup restore \
  --archive /absolute/path/backup.zip.age \
  --identity /secure/location/nomiarch-recovery-key.txt \
  --sha256 RECEIPT_SHA256 \
  --destination .nomiarch/restored-dev

python3 -m nomiarch dev up --data-dir .nomiarch/restored-dev
```

For an appliance, restore into a **fresh dedicated supported host's** empty
`/var/lib/nomiarch` location (mount an intended existing recovery disk explicitly first).
Then use the included `releases/RELEASE_HASH.tar` to reinstall K3s/application services with
the separately trusted release public key. The original controller repository can invoke:

```bash
sudo python3 -m nomiarch host install \
  --bundle /var/lib/nomiarch/releases/RELEASE_HASH.tar \
  --trusted-key /approved-media/release-public.pem
```

The archive contains the active signed release, model/runtime artifacts, identities, and
consistent core database. It excludes cloud provisioning state, SSH administrator keys,
OS/K3s internals, and unrelated workloads. Keep the controller run directory separately
if you need to manage or remove the original cloud foundation. Restore is an explicit
operator operation; encryption alone does not identify the archive's sender, hence the
independent checksum and release trust requirements.

## Independent expiry controller

For unattended Azure tests, use a second management host with independent Azure access and
a protected shared state directory outside every disposable resource group. Both machines
need synchronized clocks and a filesystem that correctly supports cross-host advisory locks
and atomic renames. Different hostnames are checked; that check cannot prove physical fault
independence. Assign the cleanup worker only the authorized temporary-test scope.

On the second host, run this under your existing service supervisor:

```bash
python3 -m nomiarch bootstrap reap \
  --state-dir /protected/shared/nomiarch \
  --worker-id lab-cleaner --watch
```

Set `lifecycle.cleanup_worker_id` to `lab-cleaner`, then on the initiating controller:

```bash
python3 -m nomiarch bootstrap apply -f env.azure.json \
  --state-dir /protected/shared/nomiarch \
  --scope foundation --destroy-after --unattended
```

An absent, stale, or same-host heartbeat stops the unattended run before creation. The worker
uses recorded run identities and ownership checks; TTL tags alone do nothing. The watcher
handles runs sequentially, so size the worker capacity and test concurrency accordingly.
The watcher is implemented, but a real independent-controller failure drill remains a release
gate. No cloud scheduler, recurring paid service, or auto-cleanup account is created for you.
