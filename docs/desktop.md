# Nomiarch Desktop Setup

The desktop preview replaces terminal steps with a native window. Desktop version
0.1.0.dev3 installs the already validated Core 0.1.0.dev2.

1. Download the application for your computer from the desktop preview release.
2. Open Nomiarch Setup and choose **Set up this computer**.
3. Choose **Install VM support** if requested. Finish Canonical's installer. On Windows,
   **Enable Hyper-V** opens Windows administrator approval and a restart may be required.
4. Choose automatic downloads, or select a previously prepared offline kit.
5. Review the system name, CPU, memory and disk settings, then select **Create my Nomiarch system**.
6. Return to **Manage an installation** for verification, backups, repair, upgrades or removal.

The app bundles its Python runtime and signature-verification dependency. No Python, Git,
Docker or OpenSSL installation is required on the desktop. Downloads use pinned versions,
SHA-256 checksums and signed Core bundles. Automatic downloading is a staging operation;
**Use files I brought into this environment** performs no downloads.

The offline-kit option downloads files for the staging computer's CPU architecture. Bring
that kit and the app to a matching isolated computer; install the VM prerequisite before
isolation. The application does not establish the physical network boundary.

Windows preview requires **Windows 11 Pro, Enterprise or Education, x64 and Hyper-V**.
Home and ARM Windows are not yet supported. macOS Intel/ARM and Linux x64 builds are
provided. Native file, process and GUI package tests are distinct from real VM qualification;
end-to-end customer hypervisor installation and physical air-gap tests remain outstanding.

If setup stops, it retains the run records. Use Manage to inspect or repair the retained VM;
starting a new installation creates a separate VM. Downloads can be cancelled and completed
verified files are reused. Installation and maintenance are not interrupted by closing the
window; wait for completion so the final result can be recorded.

Upgrades ask for a signed new bundle and an approved key. A new recovery identity is generated
and saved to a location you choose; the private key is never sent to the VM. Keep it separate
from encrypted backups. Existing key files are never overwritten. The current Core upgrade
compatibility limits and recovery procedure are documented in testing.md.

These preview applications are **unsigned**. Windows/macOS may warn or refuse to open them.
Production code signing and macOS notarization require publisher credentials that are not
configured. Do not disable operating-system protections to run this preview.
