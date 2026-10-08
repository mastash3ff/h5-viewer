# Windows build and installation

H5 Viewer is built on 64-bit Windows with Python 3.13, `uv`, PyInstaller, and
Inno Setup 6. The portable distribution includes Python, Qt, NumPy, h5py/HDF5,
gzip/DEFLATE and LZF filter support, and pyqtgraph, so the target computer does
not need Python or internet access.

## Build

From Windows PowerShell in the repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
.\scripts\build_windows.ps1
```

The bootstrap installs current Python 3.13, `uv`, and Inno Setup packages for
the current user through `winget` when they are absent. It does not replace an
existing Python 3.10 installation or change a shared environment. The build
copies source into `%LOCALAPPDATA%\H5ViewerBuild`, creates an isolated `.venv`
from the checked-in `uv.lock`, and writes release assets under `dist/release`:

- `H5Viewer-<version>-windows-x64-portable.zip`
- `H5Viewer-<version>-windows-x64-setup.exe`
- `SHA256SUMS.txt` and release/dependency manifests
- Matching dependency source archives and rebuild instructions

`README.md`, `LICENSE`, the `docs/` tree, `THIRD_PARTY_NOTICES.txt`, and collected
license texts are included in both. The source archives are separate release
downloads; they are not required to run the viewer.
The portable archive can be extracted anywhere and launched with
`H5Viewer.exe [file.h5]`.

## Install and file types

The installer uses `%LOCALAPPDATA%\Programs\H5Viewer`, needs no administrator
rights, and creates Start menu entries. It registers H5 Viewer for `.h5` and
`.hdf5` in the current user's **Open with** list and Windows **Default apps**.
It does not replace the user's existing default application or write the
protected `UserChoice` registry key.

To select H5 Viewer as the default, open **Settings > Apps > Default apps** and
choose H5 Viewer for `.h5` and `.hdf5`, or right-click a file and use
**Open with > Choose another app > Always**. Uninstall removes the application's
owned registration and Open With values; Windows retains the user's protected
default-selection history.

## Diagnostics and release checks

The runtime diagnostic writes a JSON report and leaves the source unchanged:

```powershell
$diagnostic = Start-Process .\H5Viewer.exe -ArgumentList '--check "C:\data\sample.h5" --report "C:\checks\report.json"' -Wait -PassThru
if ($diagnostic.ExitCode -ne 0) { throw "Diagnostic failed: $($diagnostic.ExitCode)" }
Get-Content C:\checks\report.json
```

Check both the process result and the report. CI uses synthetic datasets and
exercises table, plot, and image rendering in the packaged application, including
Unicode and space-containing paths. It checks Windows Job Object enforcement and
gzip/DEFLATE and LZF reads.

Installer lifecycle checks run only on disposable GitHub-hosted Windows runners.
They cover installation, same-version reinstall, owned registry entries, and
uninstall. They do not establish cross-version upgrade behavior, offline operation,
physical Explorer double-clicks, or a user's manual default-app selection.
Installation or default-app changes on a personal workstation require separate
approval. See [releasing](RELEASING.md) and the historical
[verification record](VERIFICATION.md).
