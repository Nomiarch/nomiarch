# Customer-owned setup and human approval

Desktop 0.1.0.dev5 adds internal GitHub Enterprise Server approval support and uses
the admitted Core 0.1.0.dev3 artifacts. Use the downloads linked from the
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
   Organisation setup requires a private public-GitHub or internal GitHub Enterprise
   Server repository. Other repository products support configuration export only.
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

### GitHub Enterprise Server

Nomiarch supports two internal GitHub Enterprise Server paths:

- **Existing GHES** — connect to a customer-operated server already inside the approved network.
- **Managed GHES bootstrap (Azure)** — generate a private Azure GHES appliance foundation first, then hand control to the customer for the GitHub licence, Management Console password, TLS private material, first administrator and internal DNS.

The managed path deliberately separates bootstrap from repository authority: the new Git server cannot approve the Terraform that creates itself. After the appliance is configured, Nomiarch reconnects to it using the same protected-main and human-review controls as an existing GHES installation. The server must support the REST API and branch protections above.
This preview targets the documented GHES 3.21 REST contract; qualify it against your
site's server before relying on it for a customer deployment.


For the managed Azure path, the generated customer scaffold includes `bootstrap/ghes` and a vendored `modules/ghes` recipe. It creates a private GHES VM on the selected customer subnet, premium root/data storage, restrictive NSG rules, and (when selected) an Azure Blob storage/private-endpoint foundation for GitHub Actions. The GitHub licence file, management password, TLS private key and repository tokens are never rendered into the scaffold. GitHub currently recommends at least 4 vCPU, 32 GB RAM, 400 GB root storage and 500 GB attached data storage for trial/demo or up to 10 light users; choose a memory-optimized premium-storage-capable VM size that satisfies those requirements.

For an existing GHES installation:

1. Ask your administrator for the HTTPS server address, customer owner, reviewer
   logins and a token issued by that server. If the server uses a private CA, also
   get the public CA certificate chain in PEM format. No private key is needed.
2. For an offline installation, choose **Disconnected site**, then **Organisation**.
   Select **Internal GitHub Enterprise Server with human review**.
3. Enter the server's root address, such as `https://git.company.internal`.
   Do not add `/api/v3`; the wizard adds the API path. An explicit HTTPS port is allowed.
4. Use **Choose public CA certificate…** if needed. Compare the displayed file SHA-256
   with the value provided by your administrator. The public certificates become part
   of the reviewed configuration; private keys and the repository token never do.
   Without a selected file, the Python TLS runtime uses this computer's available
   trusted certificates. If that trust cannot verify your server, supply its public CA.
5. Enter the owner, dedicated repository name, comma-separated human reviewer logins
   and token. Changing the address, certificate or account settings clears the token;
   enter it again after correcting those settings.
6. Complete VM prerequisites and select the admitted offline kit. Create the
   configuration, create the private repository and open the configuration PR.
7. A different designated person reviews and merges the final commit on the internal
   server. Enter its PR number, prepare the deployment plan, and approve the plan.

The controller requires verified TLS with the original hostname. It connects only to
RFC 1918 IPv4, IPv6 unique-local or loopback addresses, rejects mixed private/public DNS
answers, and uses the checked numeric address without resolving a second time. Public,
link-local, shared-address and IPv6 global-unicast destinations are not supported by
this internal recipe. Configure internal DNS and a direct private route; environment
proxies and redirects are not used. A public GitHub service cannot be selected here.

Installation, observation, repair, upgrade and removal use the same internal server
and final-commit human review checks. Evidence includes the server origin, repository,
PR, commit and reviewer logins. Tokens remain in memory and are scoped to the selected
repository and certificate settings. They are never sent to Core. Import new release
kits through the site's approved transfer process before upgrading.

New folders use recipe **0.3.0**. Existing **0.2.0** folders remain supported with their
original file contents and runtime pins. Changing an existing foundation's repository
authority is not a supported in-place migration; create a new configuration for an
internal deployment. Other internal Git products remain configuration-export only.

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
Public GitHub needs Internet access. Organisation installations inside the boundary can
use the internal GitHub Enterprise Server steps above, including new PRs for later
operations. Keep the controller, Git server, DNS, identity services and reviewers'
computers inside the admitted network. Personal evaluation can use local human approval.
Selecting **Export for another review system** stops at the configuration folder.

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
Internal repository tests use a real local HTTPS server with generated CA certificates
and REST fixtures. They cover hostname/CA failures, redirect/proxy isolation, private DNS,
exact file/review matching and operation approval gates. They do not establish qualification
against a deployed customer GitHub Enterprise Server or a physically disconnected site.
