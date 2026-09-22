# Customer-owned setup and human approval

Desktop 0.1.0.dev4 introduces customer scaffolds and uses Core 0.1.0.dev3 after
the release files pass admission. Use the downloads linked from the
[customer guide](https://nomiarch.com/docs/running/).

## First local installation

1. Open the app for Windows, Apple silicon, Intel Mac or Ubuntu.
2. Choose **Set up this computer**. Enter your organisation/project and environment
   using lowercase names, for example `example` and `evaluation`.
3. Choose **Personal evaluation** for your first end-to-end test. Choose
   **Organisation** when a second person will review the customer repository.
4. Choose **Isolated VM**, or **Disconnected site** when the computer is already
   inside your approved offline boundary.
5. Choose a new configuration folder. Personal evaluation can use local records.
   Organisation setup requires a private GitHub repository or an internal-repository
   export. Internal repository deployment integration is not included yet.
6. Let the wizard check VM support. On Windows/macOS, **Install VM support** opens
   the verified Canonical installer. On supported Windows editions, **Enable Hyper-V**
   requests Windows administrator approval; restart when asked. Linux users install
   Multipass through their approved software manager.
7. Download and verify the files, or select a previously admitted offline kit.
8. Keep the default 2 CPUs, 8 GB memory and 40 GB disk for the included small model.
   Choose **Create my configuration** and inspect the folder.
9. Choose **Prepare deployment plan**. This checks the proposed installation without
   creating a VM. Review the system, capacity, network mode and resource implications.
10. Choose **Approve this plan and create the system**. Keep the window open.
    The app installs Core, configures the local guest egress boundary and tests it.
11. Open **Manage my installation → Verify**. Sample configuration failures are
    intentional acceptance checks. The host egress check is a separate live check.
12. Choose **Observe and review foundation changes → Check this environment with Core**
    for a live comparison of the configured VM capacity and outbound policy.

## Azure installation

Choose **Set up in the cloud — Azure**. Azure is the first cloud recipe; AWS and
Google Cloud are not offered as working destinations.

Complete the same customer/repository choices, then choose **Sign in to Microsoft
Azure**. The official Microsoft CLI component is included in the desktop package.
The app displays Microsoft's device code and opens Microsoft's sign-in page. Enter
your credentials and complete MFA there. The app does not collect an account password.

Choose your subscription, region and a privately reachable subnet. Your cloud
administrator must provide a VPN/private route and its management CIDR. The preview
does not create that route, a VPN gateway or Bastion. A new isolated VNet recipe is
available in the source Terraform, but full Core installation needs the existing
administration route. Missing connectivity cannot be solved by opening public SSH.

Choose a VM size and matching CPU/memory capacity. Image discovery resolves an exact
Ubuntu 24.04 image version before it enters the scaffold. The wizard generates SSH
keys privately, validates the generated Terraform root, and saves a real OpenTofu
plan. Review resource actions and approve that saved plan to deploy. Azure resources
incur charges until explicitly removed. No prices are estimated by this preview.

The VM has a private IP, scoped SSH administration and a restricted outbound policy.
Azure platform services and the management plane remain dependencies. This is not a
physical air gap. Actual subscription permissions, private routing and cloud lifecycle
testing must be validated in the customer's environment.

## Customer repositories

The generated folder contains `foundation.json`, pinned `runtime.json`, policies and
an environment folder. Azure includes a vendored, versioned Terraform/OpenTofu module
and provider lock files. Local lifecycle uses `local.json` and the explicit Multipass
controller adapter. **This release does not supply a native Terraform provider for
local VMs.** Both paths use the same configuration-review and plan-approval rules.

For GitHub, supply the customer owner, dedicated repository name and designated human
reviewer logins. The initial proposer and reviewer must be different people/accounts.
Use **Create a NEW private customer repository**, then **Open a configuration pull
request**. Repository creation requires customer administration permissions. GitHub
must support branch protection for the selected private repository.

An existing repository must be dedicated to this scaffold and already have the required
protection. The app refuses to overwrite unrelated customer files. Main requires human
review, CODEOWNERS, stale-review dismissal, last-push approval and protection for admins;
force pushes and deletion are disallowed. The first PR establishes CODEOWNERS, and the
controller also independently checks the configured reviewer list.

Have a designated person approve the final PR commit and merge it. Enter the merged PR
number in the wizard. Before planning and again before deployment, the controller checks
the exact main commit, file contents, current protection and final-commit human reviews.
Bot approvals, self-approval, stale reviews and a changed main branch do not satisfy this
gate. An organisation cannot fall back to a personal local confirmation.

Keep credentials outside Git. The GitHub token lives in the desktop process's memory.
Creation needs administration, Contents and Pull requests permissions. For subsequent
proposals, use a separate account/token with Contents and Pull requests write access and
Administration read access to inspect protection; do not grant it protection bypass or
cloud deployment rights. Approval verification only needs the corresponding read access.
Use customer-owned deployment credentials independently of the proposal account.

The Azure profile, generated keys, saved plans and state stay in the controller's private
application directory. Core receives none of those credentials. Use the operating system's
disk encryption/access controls and back up that directory separately from Git. This preview
uses controller-local Terraform state and locks; it does not implement a shared remote state
backend or a distributed deployment lock. Use one deployment controller for an environment.

## Observe, propose, review, apply

The desktop observer reads the approved local scaffold and actual Multipass capacity or
Azure VM/disk/NSG settings. With a GitHub read token, it also checks protected main against
the last deployed approved commit. Core reports its running release version and evaluates the
measurements, produces a local-model explanation and records evidence. A controller bridge
can export the bounded repair request or open a customer PR. Core has no repository token,
cloud credentials, approval key, merge operation or deployment tool.

In Manage, open **Observe and review foundation changes**. A manual check runs once.
The optional checkbox checks every minute while that screen and the app stay open and
opens a repair PR on detected drift. It stops when you leave the screen or close the app.
This is not an always-running Core/cloud service. A pending proposal suppresses repeated
automatic PRs; inspect it before requesting another proposal.

The current repair recipe restores the existing approved capacity/outbound policy.
It cannot modify approval policies or execute arbitrary Terraform from Core output.
Azure plans that create, replace or delete resources are refused in this repair path.
Local capacity repairs may stop/start the VM; disk shrinking is prohibited.
Changed repository commits or running release versions require review before an automatic
infrastructure repair can be proposed; the observer does not silently adopt new desired state.

After review and merge, choose **Review a proposed configuration folder**, prepare a
fresh plan, inspect the actions/downtime, then approve it. The controller rechecks the
environment, configuration, repository review, plan integrity and expiration before
applying. A changed plan needs new approval. Plans are usable once and expire in an hour.
The HMAC approval seal belongs to the trusted controller OS account; it is not a remote
attestation or protection against that computer's administrator.

The observer checks a bounded set of settings. Azure NSG observations are configuration
checks, not exhaustive packet-flow tests. Missing permissions or an unreachable controller
do not establish a healthy environment. Custom infrastructure changes, autonomous scanning
of arbitrary repositories, and general-purpose remediation are outside this recipe.

## Offline and air-gap operation

On a connected computer, choose **Download an offline kit**. Windows/macOS kits also
include the matching pinned Multipass installer. Copy the app and complete kit to a
computer with the same operating system and CPU architecture through your approved
transfer process. Linux VM support must be admitted through your software-management process.

Choose **Disconnected site** on the destination. File admission and VM-support setup
use local files and fail if a required file is missing; they do not silently download it.
Private GitHub is still an online service: a real air gap needs internal Git/reviews and
admitted software updates inside the boundary. Internal Git approval integration is currently
an export path, so organisation deployment stops after scaffold export. Personal local
evaluation can proceed with local human approval.

The local guest boundary rejects new outbound IPv4/IPv6 traffic except loopback, DHCP and
the fixed K3s pod/service ranges. Replies to permitted incoming administration sessions
remain possible. It is installed after VM creation and reasserted after Core installation;
it does not claim to isolate the VM's earlier boot interval. A systemd unit restores rules
at boot. Verify them after a real reboot and test the site's actual network paths.

The wizard does not disconnect the host's Ethernet, Wi-Fi or other radios. A connected
host, its hypervisor and any permitted management/transfer channel remain trusted. The
customer must establish and validate physical disconnection for a physical air gap.

## Upgrade, repair, removal and recovery

Use the same wizard's **Manage an installation** screen. For an upgrade, first get the
desktop release that admits the new Core version and download its kit. Select **Upgrade**,
choose that kit's signed bundle and public key, and save a new private recovery key.
The wizard validates both files against its admitted catalog and records their hashes in
the proposed runtime configuration. Older configuration folders keep their original runtime
pins when opened in a newer desktop app.

Every repair, upgrade and removal creates its own operation request and one-use plan.
Organisation customers must have a designated person approve and merge that specific PR;
the earlier installation approval does not authorize later replacement or deletion. Personal
evaluation records the explicit local plan approval. Review the selected VM, Core versions,
downtime and destructive effects before applying. Release downgrades are prohibited.

An upgrade exports an encrypted pre-change backup before starting the release change. If
the backup fails, the upgrade stops. Keep the private recovery key separate from the backup.
Repair uses the existing approved release; removal deletes only the recorded VM/resources
after another ownership check. The older direct terminal maintenance path refuses customer
foundation runs, so it cannot silently bypass their operation review.

For a pending request, open **Observe and review foundation changes → Review a proposed
configuration folder**. The controller keeps the private admitted files, run records and any
partial state needed for recovery. Bundle compatibility and restore procedures remain as
described in [testing.md](testing.md); automatic rollback is not implemented.

If provisioning or reconciliation fails, keep the run/change directory and any partial
Terraform state. Read the recorded error before retrying. A failed plan is not a successful
installation, and starting another installation creates another set of resources.

## Validation scope

Tests cover template admission, tamper/staleness rejection, human-review validation,
Core observation evaluation, generated Terraform schema and IPv4/IPv6 rules in disposable
Linux network namespaces. Native package tests cover Windows, Intel Mac, Apple silicon and
Linux screens and the packaged Microsoft helper. These gates do not substitute for an
end-to-end installation on a customer hypervisor or a funded, privately routed Azure account.
