# Install and maintain the Nomiarch preview

For the graphical local/Azure setup, customer repositories and human approvals, use
[the desktop guide](desktop.md) and [customer foundation guide](foundation.md). The
website has separate Windows, macOS and Linux opening instructions, including Apple's
per-app **Open Anyway** step. The rest of this page describes the advanced terminal path.

Version: **0.1.0.dev3**. This is a technical evaluation, with a terminal wizard and an
API-based core. There is no browser dashboard yet. The included task checks sample
configuration, explains findings with a local model, and records evidence; it does not
automatically remediate live infrastructure. Core also evaluates bounded foundation
observations from the desktop controller; any repair still requires human approval.

## 1. Choose a machine

Use macOS or Linux with Python 3.11+, OpenSSL and Multipass installed. Choose **amd64**
for Intel/AMD machines, **arm64** for Apple Silicon or ARM Linux. Reserve **2 CPUs,
8 GiB RAM and 40 GiB disk for the guest**, plus room for the downloaded bundle and host OS.
The guest is Ubuntu 24.04. Allow additional disk for staging and encrypted backups.
Actual Multipass host combinations and physical air-gap tests remain evaluation gates.
Windows/WSL integration with the host hypervisor is not yet qualified.

Install prerequisites while connected, using Python's and Canonical Multipass's official
instructions. Run `python3 --version`, `openssl version`, and `multipass list` before
transferring the installation kit. Docker and Git are not required for this customer path.

## 2. Download your installation kit

Open https://github.com/Nomiarch/nomiarch/releases/tag/v0.1.0.dev3 and download the four
assets for your architecture (replace `amd64` with `arm64` where needed):

- `nomiarch-controller-amd64.zip` — the bootstrap application and offline documentation.
- `nomiarch-amd64.tar` — the signed core, policies, runtime and local model.
- `nomiarch-amd64-public.pem` — release verification public key.
- `SHA256SUMS-amd64.txt` — download checksums.

If the release or an asset is missing, the build has not passed its publication gates;
do not substitute a GitHub source archive for the offline bundle.

Separately obtain an **Ubuntu 24.04 cloud image** for the matching architecture from
https://cloud-images.ubuntu.com/noble/ and its publisher-signed checksum information.
Use Canonical's verification procedure before accepting the image SHA-256. Multipass
must support the selected local image on your host. The wizard never downloads an image.

Verify downloaded file hashes using `shasum -a 256 FILE` on macOS or `sha256sum FILE`
on Linux, and compare with the release checksums. Preview signing keys are generated
per architecture/build and their private halves are discarded. Obtain the public PEM's
SHA-256 approval through a separately trusted channel before admission. A PEM and hash
from the same untrusted media do not establish publisher identity. New preview versions
may use a different key, which must be approved separately.

## 3. Transfer and start

For an air-gapped test, transfer the verified kit and Ubuntu image through your approved
process to the isolated host. The host, hypervisor and administration network must
already enforce the intended boundary. Disconnecting only guest applications does not
create a physical air gap. Keep local Multipass management functioning.

Extract the controller ZIP into a permanent folder. Open a terminal in its `nomiarch`
folder and run:

```bash
python3 -m nomiarch bootstrap wizard
```

Choose **install**, then provide:

1. Signed bundle path and approved public key path.
2. Ubuntu image path and its trusted SHA-256.
3. Environment name and VM CPU, RAM and disk settings.
4. Whether to delete the VM after testing: choose **no** to keep it for upgrade testing.
5. Installation deadline; the default is 60 minutes.
6. Review the plan and answer **yes** to create and install.

The wizard verifies the bundle before provisioning, creates the VM, installs the services,
and runs checks with the real local model. It saves `env.local.json` and prints a run
directory under `.nomiarch/runs/`. Keep this folder; it identifies the VM for maintenance.
Do not publish the state or private logs.

## 4. Check your first result

Look for `validation.status: passed`. Core checks should show a completed task, local-model
explanation, two failing sample controls and one passing control. Those sample failures
are expected. Unauthenticated access and the forbidden tool must be denied. Worker
connectivity probes cover selected endpoints; they are not a physical isolation certificate.

Run the same wizard, choose **verify**, and select your installation to repeat the task
and save `verification.json`. Choose **status** to see saved controller state; it is not
a live health check. These are the first customer interactions; there is no web login URL.
If you chose deletion, also require `cleanup.status: deleted`.

## 5. Upgrade through the same wizard

On a connected staging machine, obtain a newer published controller ZIP and signed bundle
for the same architecture. Verify them and approve any new release key before transfer.
Extract the new controller into a separate folder; retain your original controller state.
Start it with an absolute reference to the original state directory:

```bash
python3 -m nomiarch bootstrap wizard --state-dir /absolute/path/original/nomiarch/.nomiarch
```

Choose **upgrade**, select the installation, and supply the new bundle and trusted public
key. Supply an age recovery **public recipient**, generated on your trusted recovery
machine with `age-keygen -o recovery-key.txt` followed by
`age-keygen -y recovery-key.txt`. Store the private key securely and separately. Bring the
public recipient into the isolated environment; never type the private key into the wizard.

The controller saves an encrypted backup before the upgrade. The guest checks runtime
compatibility, stops writers, takes another snapshot, installs and verifies the new bundle.
Expect downtime. This preview refuses K3s version migrations and does not provide automatic
rollback. Check the outcome and retain the exported backup and checksum receipt. A failed
backup export is reported as a failure even if the services started.

To exercise lifecycle handling before another version exists, you can select the same
signed bundle for an upgrade rehearsal. That tests backup and reinstallation, **not** a
migration between software versions. Use **repair** with the original bundle for repairs.
See `docs/testing.md` for encrypted recovery and restoration to a fresh dedicated host.

## 6. Clean up when finished

Use the exact printed run directory:

```bash
python3 -m nomiarch bootstrap destroy --run /absolute/path/.nomiarch/runs/RUN_ID
```

This permanently removes that test VM and its disks. Export any needed backup first.
Require `cleanup.status: deleted`; keep the controller state until deletion is confirmed.
The wizard itself does not offer a one-click destructive maintenance action.

## Troubleshooting

- **Python too old:** use Python 3.11 or newer (`python3.12` may be your command).
- **Multipass unavailable:** install/start it and confirm `multipass list` works before disconnecting.
- **Missing bundle/image:** bring verified local files; no download fallback is attempted.
- **Wrong architecture:** select the matching bundle and image; the wizard rejects mismatches.
- **Signature/checksum failure:** stop and replace the artifact through your trusted process.
- **Configuration already exists:** maintain the existing installation, or choose a new
  `--output env.second.json` when deliberately creating a separate VM.
- **Interrupted installation:** keep its run directory, inspect status and logs, then use
  repair on the retained VM or scoped destroy before starting a replacement. Do not blindly
  rerun installation; it creates a new run.

## Azure and existing hosts

The guided new-VM path currently uses local Multipass. Azure and existing-host provisioning
use the separate commands in `docs/testing.md`. Once provisioned, their retained run records
can be selected by the maintenance wizard. Azure is a private cloud profile, not a physical
air gap. Live Azure creation remains unqualified and usage before deletion can be billed.
