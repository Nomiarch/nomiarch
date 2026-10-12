# Desktop 0.1.0.dev7 acceptance

Download from https://nomiarch.com/docs/running/. Confirm the header says Setup
0.1.0.dev7. Core 0.1.0.dev3 in the header refers to the separate legacy preview;
infrastructure setup does not install Core, models or agents.

## What changed

HTTPS uses the operating system's certificate store via truststore, including the
macOS Keychain, instead of depending on CA paths from the Python build machine.
Certificate and hostname verification remain mandatory. Explicit internal Git CA
files retain their isolated trust policy. Local infrastructure now shows the VM
support check and installation controls before downloads. Switching to a Core
preview resets infrastructure mode.

## Automated release gates

- Behavior and controller tests, including invalid certificates, hostname mismatch,
  tampered downloads, offline missing files, review gates, plan expiry/resume,
  verification and refusal to delete adopted hosts.
- Windows x64, macOS ARM64/Intel and Linux x64 packaged executables must download
  and hash-check both admitted release public keys, follow GitHub release redirects,
  and establish verified HTTPS to GitHub API, Ubuntu images and Multipass installers.
  These tests do not download entire multi-GB images or provision infrastructure.
- Each executable generates and validates 14 infrastructure scaffolds: every valid
  combination below, with AMD64 and ARM64 for Local/On-premises, AMD64 for Azure.
- Azure provider schema and guest integration tests run separately in CI.

## Live end-to-end matrix

Run each row with a fresh environment/repository name and retain its evidence.
These are acceptance procedures, not claims that live environments were tested.

| Destination | Git service | Network | Preparation |
| --- | --- | --- | --- |
| Local | GitHub SaaS | Restricted connected | Multipass, supported workstation |
| Local | GitHub Enterprise Server | Restricted connected | Multipass, private route to GHES |
| Local | GitHub Enterprise Server | Fully disconnected | Admitted image/support installer, internal validation runner |
| On-premises | GitHub SaaS | Restricted connected | Dedicated Ubuntu 24.04 host and trusted SSH |
| On-premises | GitHub Enterprise Server | Restricted connected | Same host, private route to GHES |
| On-premises | GitHub Enterprise Server | Fully disconnected | Existing host, internal GHES/runner, admitted files |
| Azure | GitHub SaaS | Restricted connected | Subscription, private subnet, SSH route, OpenTofu |
| Azure | GitHub Enterprise Server | Restricted connected | Same Azure inputs plus private route to GHES |

Azure with fully disconnected mode and SaaS GitHub with fully disconnected mode
must be refused before a configuration is generated. Windows Home and Windows ARM
are unsupported. On-premises adoption does not create a VM or install a hypervisor.

## Procedure and expected results

1. Open a fresh dev7 app and choose **Set up infrastructure**. Select a matrix row.
   Enter Git owner/repository, a different human reviewer's login and a short-lived
   bootstrap token. For GHES, supply its private HTTPS address and optional public
   CA certificate. Tokens must never appear in generated files or logs.
2. Enter capacity and destination settings. Local: **Check your computer** must
   detect Multipass; **Install VM support** opens the verified installer on macOS
   or Windows. Complete OS installation and choose **Check again**. Linux uses
   approved package management. Continue is disabled until checks succeed.
3. Local connected: prepare files. Expect an Ubuntu image and applicable VM-support
   installer, verified against pinned hashes; no Core or model download. Retry
   should reuse completed verified files. Disconnected: admit the files before
   entering the boundary. A missing or modified file must stop with no download.
4. Review the generated configuration and create its repository/PR. Expect a private
   repository, protected main and the trusted validation check. Have the separate
   reviewer approve the current commit, pass checks and merge. Stale, self-authored,
   unmerged or failed validation must prevent planning.
5. Prepare the deployment plan. Check resource names, capacity and destination.
   Nothing should be provisioned yet. Close/reopen and resume the saved plan; an
   unused expired plan must be retired before preparing a replacement.
6. Approve the exact saved plan. Expect Local/Azure resources to be created once,
   or the existing host to be adopted. Verify requested CPU/RAM/disk, OS, SSH and
   guest network policy. Result should be **Infrastructure ready**. No models,
   agents or application containers should have been installed.
7. Reopen **Manage an installation** and run verification. Export configuration and
   records. Keep the private controller directory, Terraform state and keys separately:
   the desktop export alone is NOT complete disaster recovery. A clean-host restore
   drill is still required before relying on recovery.
8. Test removal only on disposable Local/Azure resources, through the separately
   reviewed and approved removal flow. Confirm those resources disappear and Azure
   billing resources are removed. Adopted on-premises host removal must be refused.

Also test the legacy Local/Azure Core paths separately if you intend to use them.
Switch from infrastructure back to Home, then choose Core; Core downloads must be
selected rather than the infrastructure-only kit. Legacy flows are outside the
infrastructure milestone and have not gained live qualification from these fixes.

## Evidence and release limits

Record OS/architecture, selected matrix row, installer version, PR/check URLs,
plan digest, verification result and removal result. Never include tokens, private
keys or sensitive Terraform state in a bug report. Live customer GitHub/GHES,
Azure, on-premises, physical air-gap and clean-host recovery qualification require
those environments; fixture and scaffold tests do not substitute for them.

If dev7 has a blocker, retain the controller directory and stop provisioning.
Previous dev6 assets remain immutable, but have the known macOS HTTPS issue; do
not bypass TLS or use them as a certificate workaround.
