# Nomiarch Desktop Setup

The desktop preview replaces terminal steps with a native window. Desktop version
0.1.0.dev4 uses Core 0.1.0.dev3 once its architecture-specific artifacts pass admission.
See [customer scaffolds and human approval](foundation.md) for the new setup flow,
Azure prerequisites, live observations and the precise limits of this preview.

Use the [customer installation guide](https://nomiarch.com/docs/running/) for separate
Windows, macOS and Linux tabs with the appropriate downloads and opening steps.

## Open the app on your computer

### macOS

1. Open **Apple menu → About This Mac**. Choose the Apple silicon download if it lists
   an Apple **Chip**, or the Intel download if it lists an Intel **Processor**.
2. Download from the [macOS tab](https://nomiarch.com/docs/running/#macos), extract the
   ZIP, move **NomiarchSetup** to Applications and double-click it.
3. If Apple shows **“NomiarchSetup” Not Opened**, the preview is unsigned and not notarized.
   If you downloaded it from the official page and trust it for testing, click **Done**.
4. Open **Apple menu → System Settings → Privacy & Security**. Scroll to **Security**,
   find the NomiarchSetup message and click **Open Anyway**.
5. Authenticate if requested, then click **Open**. macOS saves an exception for this app.

See [Apple's official instructions](https://support.apple.com/en-ca/102445). If **Open Anyway**
is missing, try opening the app again and return to the settings page. On a managed Mac,
ask your IT administrator if the option is restricted. Keep macOS security protections enabled.

### Windows

Use the [Windows tab](https://nomiarch.com/docs/running/#windows). Download and open
**NomiarchSetup-windows-x64.exe**. Windows 11 Pro, Enterprise or Education on Intel/AMD
with Hyper-V is required; Home and ARM Windows are not supported. This unsigned preview may
trigger a warning. On a managed computer, ask IT whether evaluation is permitted.

### Linux

Use the [Linux tab](https://nomiarch.com/docs/running/#linux). The reference desktop is
Ubuntu 24.04 on Intel/AMD. Download **NomiarchSetup-linux-x64**, use your file manager's
properties to allow execution as a program, then open it. If VM support is missing, install
Canonical Multipass through your approved software manager and choose **Check again**.

## Create and manage your system

1. Follow the opening steps for your computer above.
2. Open Nomiarch Setup and choose **Set up this computer**.
   Enter your organisation/environment, select personal or organisation use, choose
   the isolation mode and a customer configuration folder/repository.
3. On Windows or macOS, choose **Install VM support** if requested. Finish Canonical's installer. On Windows,
   **Enable Hyper-V** opens Windows administrator approval and a restart may be required.
4. Choose automatic downloads, or select a previously prepared offline kit.
5. Review the system name, CPU, memory and disk settings, then select **Create my configuration**.
   Review the folder and any required customer PR. Choose **Prepare deployment plan**, then
   **Approve this plan and create the system** after reviewing its details.
6. Return to **Manage an installation** for verification, backups, repair, upgrades or removal.

The app bundles its Python runtime and signature-verification dependency. No Python, Git,
Docker or OpenSSL installation is required on the desktop. Downloads use pinned versions,
SHA-256 checksums and signed Core bundles. Automatic downloading is a staging operation;
**Use files I brought into this environment** performs no downloads.

The offline-kit option downloads files for the staging computer's CPU architecture, with
the Windows/macOS VM-support installer. Bring that kit and the app to a matching isolated
computer. The application configures guest egress rules, but does not establish the physical
network boundary or disconnect the host.

Windows preview requires **Windows 11 Pro, Enterprise or Education, x64 and Hyper-V**.
Home and ARM Windows are not yet supported. macOS Intel/ARM and Linux x64 builds are
provided. Native file, process and GUI package tests are distinct from real VM qualification;
end-to-end customer hypervisor installation and physical air-gap tests remain outstanding.

If setup stops, it retains the run records. Use Manage to inspect or repair the retained VM;
starting a new installation creates a separate VM. Downloads can be cancelled and completed
verified files are reused. Installation and maintenance are not interrupted by closing the
window; wait for completion so the final result can be recorded.

Upgrades ask for the new desktop catalog's signed bundle and approved key. Customer
foundations create operation-specific plans for repair, upgrade and removal. Organisation
customers also require a new human-reviewed PR for that operation. A new recovery identity is generated
and saved to a location you choose; the private key is never sent to the VM. Keep it separate
from encrypted backups. Existing key files are never overwritten. The current Core upgrade
compatibility limits and recovery procedure are documented in testing.md.

These preview applications are **unsigned**. Windows/macOS may warn or refuse to open them.
Production code signing and macOS notarization require publisher credentials that are not
configured. The Mac steps above describe Apple's exception for one trusted app. Do not
disable operating-system protections to run this preview.
