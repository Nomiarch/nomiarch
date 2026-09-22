# Validation status and release gates

This is `0.1.0.dev1`, an engineering preview. “Implemented” describes code paths;
“passed” describes an observed test. No stable `v0.1.0` tag has been published.

## Evidence available for this initial implementation

| Check | Status |
| --- | --- |
| Python compilation and CLI invocation | Passed in the development workspace |
| Separate local API/gateway/worker processes and sample workflow | Passed with the deterministic development adapter |
| Real local GGUF inference through the gateway | Passed with Qwen2.5 0.5B Q4_K_M and llama.cpp v0.4.1; [captured result](evidence/local-model.json) |
| Authentication, task persistence/restart, idempotency, expired leases | Passed in behavioral tests |
| Denied foundation tool and fail-closed policy/evidence failures | Passed in API/policy tests |
| RSA bundle admission, untrusted signer, modified/missing/unsafe artifacts | Passed in artifact tests using synthetic payloads |
| Encrypted age ZIP snapshot and restore of real SQLite state | Passed with age 1.3.2 |
| Success/failure/cancellation/timeout cleanup and retained reports | Passed with injected provider outcomes; no real Azure resource created |
| Foreign resources, subscription mismatch, adopted-host deletion | Passed in ownership/scope tests |
| Independent reaper decision path and stale/same-host rejection | Passed with simulated controller state; real fault-domain drill still required |
| Rego policy | Passed using OPA 1.20.2 |
| Azure HCL formatting | Passed using OpenTofu 1.12.6 |
| Azure provider initialization | Passed; AzureRM 4.49.0 signature verified and lock recorded |
| Azure provider schema validation | Passed in GitHub Actions with AzureRM 4.49.0; [run](https://github.com/Nomiarch/nomiarch/actions/runs/35756385285) |
| Complete signed Docker release build | Passed in the Ubuntu integration job |
| Ubuntu/K3s guest installation | Passed in the Ubuntu integration job with real model and worker egress probes |
| Repair, upgrade, and appliance recovery payload | Integration test in progress; repair exposed packaged DNS reconciliation on restart, now addressed using K3s skip-file support |
| Actual Multipass create/install/reboot/destroy | Requires target validation |
| Actual Azure create/install/destroy and failure cleanup | Requires an authorized Azure lab; not executed by the initial implementation session |
| Physical air-gap installation and clean-host appliance restore | Requires target validation |

The workflow in `.github/workflows/ci.yml` runs behavioral tests, real encrypted recovery,
local process integration, OPA tests, and Azure provider validation on Ubuntu 24.04.
The separate guest workflow builds the complete bundle and tests install/repair on a
disposable Ubuntu runner. That runner remains a connected administration host.
Inspect the workflow result for the exact commit you test. A CI pass does not prove a
physical air gap or live Azure provisioning.

## v0.1.0 qualification matrix

| Gate | Required evidence before a stable release |
| --- | --- |
| B01 | Environment schema and credential-reference rejection cases on both controllers |
| B02 | Azure creates private foundation and returns inventory for the common installer |
| B03 | Local image admission/creation and existing-host installation on each advertised host/driver combination |
| B04 | Same release passes identity, policy, API, local-model, and persistence tests on both targets |
| B05 | First install and real inference with external networking unavailable and caches empty |
| B06 | Included task succeeds; unauthorized tools fail; bootstrap authority never enters worker/model identities |
| B07 | Permitted administration and denied worker Internet/metadata/DNS paths verified on each target |
| B08 | Reboot and interrupted-install recovery preserve identity, jobs, and evidence |
| B09 | Encrypted appliance backup restores the declared complete scope on a clean admitted host |
| B10 | Offline core update, tamper rejection, incompatibility rejection, and failed-update recovery |
| B11 | Local and Azure installations operate concurrently with independent identity/state |
| B12 | Publish exact tested host OS/driver/guest architecture/image/runtime/model versions |
| B13 | Real destroy-after confirms both validation outcome and actual resource absence |
| B14 | Partial provisioning, timeout, cancellation, and initiating-controller loss recovered with an independent worker |
| B15 | Real cloud locks, foreign children/dependencies, uncertain ownership, and adopted/shared resources handled safely |
| B16 | Reports survive target deletion; validation/cleanup status and possible incurred charges remain explicit |

Also review the selected model/runtime dependencies, source licensing, third-party notices,
vulnerability results, signing-key custody, and operational support process before distributing
a customer release. The bootstrap preview does not assert NIST/Canadian-government compliance,
tenant isolation, high availability, or production readiness.
