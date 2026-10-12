# Infrastructure-only bootstrap (milestone 1)

Status: **implemented and verified with automated tests and fake providers**. Real
deployments per destination/Git combination, a deployed customer GitHub Enterprise
Server and a physically disconnected site remain acceptance gates (see the end of this
page). Nothing here claims certification, unhackability or production qualification.

This path establishes the repository and the infrastructure that will later run Nomi.
It needs **no model, model hardware, model download, Core release bundle or inference
service**. Models, Nomi Core, agents and the AI request dashboard are later milestones
and enter through the same transfer and review process. "Infrastructure ready" and
"application ready" are separate recorded states; this path never reports the second.

## What the user chooses

| Step | Choices | Notes |
| --- | --- | --- |
| Deployment location | **Local** · **On-premises** · **Cloud** | Local creates a dedicated Multipass VM on this computer. On-premises adopts a dedicated Ubuntu 24.04 VM/host your organisation already created, over trusted SSH — it does not create that VM. Cloud shows a provider selector with **Azure** as the only implemented provider; nothing else is shown as working. |
| Git service | **GitHub SaaS** (github.com, including Enterprise Cloud) · **GitHub Enterprise Server** (HTTPS address, optional private CA) | Both use the same protected-main, required-check, independent-review and separate-approval model. Choosing Enterprise Server installs or licenses nothing. |
| Network mode | **Restricted connected** · **Fully disconnected** | Fully disconnected requires Local or On-premises infrastructure and GitHub Enterprise Server inside the boundary. Azure and GitHub SaaS are refused for it with an explanation; the mode is never silently downgraded. |
| Infrastructure settings | CPU architecture, vCPUs, memory, storage; destination-specific administration settings; Git owner, repository and independent reviewer logins | Only the selected destination's settings are shown. Prerequisites are listed before anything is created. |

Provider adapters are modular (`nomiarch/foundation/destinations.py` registers locations
and providers; `nomiarch/bootstrap/providers.py` implements them). A new cloud or
on-premises hypervisor adapter is added there and stays hidden until implemented.

## Recipe 0.4.0

New infrastructure configurations use recipe **0.4.0** (`scope: infrastructure`). It has no
`runtime` block and no Core/model pins. Existing **0.2.0** and **0.3.0** folders keep their
original file contents and the Core installation behaviour; nothing migrates them in place,
and the Core setup paths remain available as previews. A 0.4.0 folder is refused by the
Core-era checks and a Core-era folder cannot be relabelled as 0.4.0.

The generated customer repository contains:

- `foundation.json` — destination, network mode, capacity, repository and reviewers.
- `policies/approvals.json` — fixed policy: human review required, agent apply/merge/policy
  changes prohibited, executor is a separate human-operated controller.
- `policies/network.json`, `policies/validation.json` — declared network mode and the required
  trusted check.
- `validation/validate.py` and `validation/manifest.json` — the vendored, standard-library
  validator and the file manifest it checks.
- `.github/workflows/nomiarch-validate.yml` and `.github/CODEOWNERS` — the required check and
  the designated reviewers.
- `inventory/repositories.json` — the repositories this foundation owns. New repositories are
  proposed by pull request to this file and created by an approved provisioning workflow;
  general-purpose provisioning is explicitly deferred.
- `environments/<env>/` — `local.json`, `host.json` or the vendored Azure module root.
- `trust/repository-ca.crt` — only for an internal server with a private CA.

Private keys, tokens, plans, state and logs never enter the repository.

## Change control and enforcement

```
request → proposal branch and PR → nomiarch-validate (no deployment credentials)
→ independent human review → authorized merge → separate human-operated controller
plans → explicit plan approval → execution → verification and audit record
```

| Boundary | How it is enforced |
| --- | --- |
| A PR never deploys | Only the controller (`deployment.apply`) executes, after `validate_plan` and an explicit approval of the displayed plan digest. |
| Validation has no deployment credentials | The workflow runs with `permissions: contents: read`; the validator reads files only. |
| Trusted check bound to the reviewed commit | `GitHub.approved` lists every `nomiarch-validate` check run for the exact PR head SHA (`filter=all`, all pages; an incomplete listing fails closed), keeps only runs created by the GitHub Actions app, and decides on the **newest by check-run id** (GitHub assigns ids in creation order; reruns get new ids). That run must be `completed` with `success`; a newer queued, in-progress, failed, cancelled or otherwise unsuccessful run is never bypassed by an older success. Main requires the check with `strict: true`. |
| A fake check cannot substitute | Only check runs from the GitHub Actions app count (app id 15368 on github.com; app slug on Enterprise Server). The merged tree is independently compared with the admitted recipe, so a PR that edits the workflow or validator fails the controller comparison. |
| Independent reviewer | A designated human, not the proposer, must approve the final commit; bot, stale and dismissed reviews do not count. |
| Policy weakening | Edited `policies/*` fail the validator and the controller's recipe comparison; weakened branch protection fails `assert_protection` before planning and before applying. |
| Plan safeguards | HMAC seal, digest, one-hour expiry, single use, exact configuration/snapshot/input binding, no re-planning during apply, provider `prepare()` is read-only (Azure saves a plan; the adopted host is probed over SSH; nothing is created). |
| Duplicate resources | Planning holds a per-foundation lock and is refused while a retained run exists; two setup windows cannot both create an executable plan. Reopening setup shows that run instead. |
| Saved and expired plans | Reopening a configuration offers **Review the saved plan** while it is valid (approval still needs the current repository check and is single use). An unused plan that expired or no longer matches the configuration can be **retired**: its plan and run records stay, `retired.json` and the foundation audit log record why, and it can never be executed. Retirement takes both the foundation and run locks, so it cannot race an apply, and it refuses any run that was approved, started, provisioned or has an inventory. A replacement plan needs fresh validation, the repository check and explicit approval. |
| Failure recovery | Failed runs keep `run.json`, `plan.json`, `approval.json`, logs and any state; nothing is deleted automatically. |
| Removal | Separate reviewed `destroy` request with its own PR and plan. Adopted on-premises hosts are never deleted. |
| Future Nomi identities | Not created in this milestone. The bootstrap admin token is used once for repository creation/protection and should be removed afterwards; executor material stays in the controller's private directory. |

**Executor.** The initial executor is the desktop controller operated by a human. It is
not an automated pipeline, and the UI and records say so (`executor:
separate-human-operated-controller`). Automating execution later must keep this boundary:
the executor identity verifies review, check, plan integrity and expiry itself.

## Running setup

1. Open Nomiarch Setup and choose **Set up infrastructure — Local, On-premises or Cloud**.
2. Enter the organisation/environment names and choose the location (and Azure for Cloud).
3. Choose the Git service, owner, repository, independent reviewer logins and the one-time
   bootstrap administration token. For Enterprise Server also enter the HTTPS address and,
   if needed, the public CA certificate.
4. Choose the network mode. Incompatible combinations are refused with the reason.
5. Enter capacity and the destination settings:
   - Local: the admitted Ubuntu image is verified from the kit folder (downloaded when
     connected; never downloaded in fully disconnected mode).
   - On-premises: host, SSH user, private key file and the `known_hosts` file whose host key
     was enrolled out of band. Hashed `known_hosts` entries are not accepted.
   - Cloud/Azure: sign in, choose subscription, privately routed subnet, management CIDR
     and VM size.
6. Review and **Create my configuration**. Then **Create a NEW private customer repository**
   (protected main with required check and reviews) and **Open a configuration pull request**.
7. The reviewer approves the final commit after `nomiarch-validate` succeeds and merges it.
   Enter the PR number, **Prepare deployment plan** (creates nothing), review the proposed
   resources, charges and downtime, and **Approve this plan and establish the infrastructure**.
8. The result screen reports **Infrastructure ready** with inventory, observed capacity,
   network check, repository commit and the retained run folder. Application status stays
   `not-installed`.

Operational output in the UI: repository and PR links, check/review requirements, proposed
resources (Azure resource actions with sensitive values omitted), charges and downtime notes,
progress log, failures with retained-state instructions, inventory and verification, and
**Manage → Infrastructure status and backup**.

## Verification

Provisioning verifies, over the administration channel: Ubuntu 24.04, CPU architecture,
CPU count, memory, storage, administrative access and the network boundary. Storage is
measured per destination and the result says what was measured:

- **Local VM and adopted host:** the root filesystem against the declared storage.
- **Azure:** the root filesystem of the fixed 32 GiB OS disk, plus the declared **data disk**:
  the recorded `/dev/disk/azure/scsi1/lun0` device must resolve to a whole disk attached at
  LUN 0 whose size meets the approved `disk_gib`, and the Azure control plane must report the
  recorded managed disk as attached to the approved VM with at least that size. A missing,
  differently attached or undersized disk fails verification. The disk is read with `lsblk`
  only; verification never formats, partitions or mounts it, and never installs an application. Local VMs get the persistent egress boundary installed and
verified. Adopted hosts in fully disconnected mode must fail a direct IPv4/IPv6 internet
probe from inside the host. These are **software observations**; a private IP, a firewall or
a blocked probe does not prove a physical air gap. Site qualification is separate.

**Manage → Verify** re-runs the checks read-only and records `verification.json`.

## Packaged desktop resources

The vendored validator (`nomiarch/foundation/customer_validator.py`) is loaded from the
installed package (`importlib.resources`, falling back to the PyInstaller bundle directory),
never from a source checkout or the network. The desktop build adds it explicitly as data.
The packaged `--self-test` on Windows, macOS (Intel and Apple silicon) and Linux generates a
Local, On-premises and Azure infrastructure scaffold plus a Core-recipe scaffold from the
bundle, round-trips each through `load_supported`, and runs the packaged validator on them;
desktop CI fails if that acceptance line is missing.

## Disconnected operation and the infrastructure kit

The kit (see **View kit inventory** in setup, or `catalog.infrastructure_inventory`) lists:

- the Ubuntu 24.04 cloud image per architecture, with SHA-256;
- the Multipass installer per operating system, with SHA-256;
- OpenTofu 1.12.6 and the Azure CLI (restricted connected Azure only);
- the vendored AzureRM 4.49.0 provider lock;
- workflow dependencies: `actions/checkout` pinned by commit and Python 3.11+ for the
  validation runner.

Process: collect exact versions on a connected staging computer, record hashes and review
results, sign the inventory, transfer through the site's controlled process, verify against
separately enrolled trust, then admit. Inside the boundary setup uses **Use files I brought
into this environment**; a missing or altered file fails closed and no download is attempted.
Updates follow the same path. Models and AI runtime artifacts are not part of this kit.

For GitHub Enterprise Server the `nomiarch-validate` job needs a self-hosted runner labelled
`nomiarch-validation` with Python 3.11+, the admitted `actions/checkout` action and no
deployment credentials. If OpenTofu is admitted on that runner with a provider mirror,
the validator also runs `tofu validate`; otherwise it reports the step as skipped and the
controller validates the plan before approval.

## Backup and recovery

**Manage → Back up** (or the button on the ready screen) exports a foundation backup:

- repository export (the reviewed configuration folder);
- controller records: `run.json`, `plan.json`, `approval.json`, `report.json`,
  `verification.json`, inventory, public SSH host material, `known_hosts`;
- trust configuration (policies, CA certificate from the repository).

Kept separately and listed in `recovery.json`: the controller approval key and private SSH
keys. Infrastructure state (`terraform.tfstate`) and command logs are exported only in an
encrypted backup (age recipient). The existing Core database backup covers application
state only and is **not** infrastructure recovery.

Recovery: restore the repository export into the Git service, re-enrol trust roots and
reviewer accounts, place the controller records under a new private state directory,
restore private keys from their separate copy, then run **Verify** before any new plan.
Test clean-host recovery as part of site qualification.

## What is implemented, simulated and outstanding

| Capability | Status |
| --- | --- |
| Destination, Git and network choices with rejection of incompatible combinations | Implemented; verified by tests |
| Recipe 0.4.0 generation, validator, manifest, workflow, inventory; 0.2.0/0.3.0 compatibility | Implemented; verified by tests (byte-for-byte fixtures) |
| Required trusted check, exact-commit binding, rejection of missing/failed/stale/untrusted checks | Implemented; verified with REST fixtures over the existing TLS test harness and mocks |
| Infrastructure-only plan/apply for Local, On-premises and Azure; no application install | Implemented; simulated with fake providers |
| Plan tamper/expiry/replay, configuration/input changes, duplicate prevention, failure retention | Implemented; verified by tests |
| Adopted-host adoption and never-delete rule | Implemented; simulated with fake providers |
| Offline kit fail-closed behaviour | Implemented; verified by tests (no network call) |
| Desktop screens | Implemented; flow logic verified headlessly with a tkinter stand-in on all desktop CI platforms; the packaged self-test renders the screens and generates and validates scaffolds from the bundle on all four platforms |
| Azure data-disk verification | Implemented; simulated with a fake VM disk layout and control-plane response; the probe script itself was executed on a real Linux host |
| Saved-plan reopen and retirement | Implemented; verified by tests including concurrent planning/apply/retirement with threads |
| Real Multipass VM creation, real adopted host, real Azure provisioning, real GitHub/GHES check and review flow, disconnected-site qualification, clean-host recovery | **Outstanding acceptance**; not demonstrated by this implementation |
