# Corresponding sources and replacing libraries

H5 Viewer uses unmodified dynamically loaded Qt, PySide6 and Shiboken6 libraries
under LGPL version 3. The complete LGPL v3 and GPL v3 texts, copyright notices,
and upstream third-party notices are in `licenses/`. H5 Viewer's own code is MIT.
You may modify these libraries and reverse engineer this application to debug
those modifications; no H5 Viewer license term restricts that activity.

The release supplies these exact, unmodified upstream source archives alongside
the portable ZIP and installer. Download and retain them with the binaries.
`source-binary-map.json` identifies their URLs, versions, SHA-256 checksums and
individual bundled binary paths. `SHA256SUMS.txt` covers the release assets.

| Archive | Rebuilds |
| --- | --- |
| qtbase-everywhere-src-6.11.2.tar.xz | Qt Core, Gui, Widgets, Network, OpenGL, OpenGLWidgets and base plugins |
| qtsvg-everywhere-src-6.11.2.tar.xz | Qt Svg and SVG plugins |
| qtimageformats-everywhere-src-6.11.2.tar.xz | Additional image format plugins |
| pyside-setup-everywhere-src-6.11.2.tar.xz | PySide6 and Shiboken6 Python bindings |

Use Windows x64, Python 3.13, MSVC 2022 with a compatible Windows SDK, CMake and
Ninja. The archives contain the upstream build scripts and complete sources.
Qt's source README and configuration help describe optional system dependencies.
For example, in a Visual Studio x64 developer shell, after extracting archives:

```powershell
mkdir qt-build
cd qt-build
..\qtbase-everywhere-src-6.11.2\configure.bat -opensource -confirm-license -release -shared -prefix C:\qt-local -nomake examples -nomake tests
cmake --build . --parallel
cmake --install .
cd ..
mkdir qtsvg-build
cd qtsvg-build
C:\qt-local\bin\qt-configure-module.bat ..\qtsvg-everywhere-src-6.11.2
cmake --build . --parallel
cmake --install .
cd ..
mkdir qtimageformats-build
cd qtimageformats-build
C:\qt-local\bin\qt-configure-module.bat ..\qtimageformats-everywhere-src-6.11.2
cmake --build . --parallel
cmake --install .
```

For authoritative Qt build details see
<https://doc.qt.io/qt-6/windows-building.html>. For PySide/Shiboken use the
instructions in `pyside-setup` and
<https://doc.qt.io/qtforpython-6/building_from_source/index.html> with the rebuilt
Qt prefix (`--qtpaths=C:\qt-local\bin\qtpaths.exe`) and Python 3.13. Build the
Core, Gui, Widgets, Network, OpenGL, OpenGLWidgets and Svg bindings; deploy both
PySide6 and Shiboken6 outputs together. These are replacement instructions,
not a claim of byte-for-byte reproducibility of upstream wheels.

Extract the portable ZIP to a writable directory and close H5 Viewer. Back up
`_internal`, then replace the mapped Qt DLLs/plugins in `_internal/PySide6/`
and corresponding PySide/Shiboken DLLs and `.pyd` files in their existing
locations. Preserve compatible x64 ABI, Python version, plugin paths and dependent
DLLs. No signature or checksum gate prevents running modified libraries.
Run `H5Viewer.exe --check`, then open a generated test HDF5 file and exercise
tables, plots and images. Reinstalling/upgrading replaces the application-owned
libraries, so retain your modified distribution separately.

To rebuild the application from its release Git commit, install Python 3.13,
uv and Inno Setup 6; run `uv sync --locked` and
`powershell -File scripts/build_windows.ps1` from the checkout. The build uses
`uv.lock`, checks exact Qt source versions/hashes, filters unused Qt modules,
and records PE binary hashes and Qt imports. To package your rebuilt libraries,
replace the matching packages in the build environment and adapt the source
pins and notices to your modifications before freezing.

The binary inventory also contains Python, Microsoft runtime, NumPy/OpenBLAS,
h5py/HDF5/zlib, PyInstaller and Mesa software. Their notices are preserved under
`licenses/`; the Qt source archives are not presented as those projects' source.
