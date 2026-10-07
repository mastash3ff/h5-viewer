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
from the checked-in `uv.lock`, and writes these artifacts under `dist`:

- `H5Viewer-<version>-windows-x64-portable.zip`
- `H5Viewer-<version>-windows-x64-setup.exe`

`README.md`, `USER_GUIDE.md`, `THIRD_PARTY_NOTICES.txt`, and collected license
texts are included in both.
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

## Release checks

Before release, verify the portable executable offline with ordinary, Unicode,
and space-containing paths. Confirm a spawned reader starts under the Windows
Job Object limit and can read gzip/DEFLATE and LZF datasets. Installer compilation
alone does not establish Explorer integration: installing, selecting a default,
double-click testing, upgrade testing, and uninstall testing require the separate
release approval described in the repository README.
