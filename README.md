# H5 Viewer

[![CI](https://github.com/mastash3ff/h5-viewer/actions/workflows/ci.yml/badge.svg)](https://github.com/mastash3ff/h5-viewer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A general-purpose, read-only Windows HDF5 desktop explorer. Inspect file structure,
fields, values, attributes, datatypes, references, and source relationships. Tables,
plots, and images are alternative views of selected data.

See the [user guide](docs/USER_GUIDE.md) for fields, slices, metadata, source links,
plots, and image-stack navigation. See [Windows delivery](docs/WINDOWS.md) for the
portable application and per-user installer.

## Install on Windows

Download the portable ZIP or per-user installer from
[Releases](https://github.com/mastash3ff/h5-viewer/releases). Windows x64 packages
include the runtime and do not require a separate Python installation.

Extract the portable ZIP and open `H5Viewer.exe`, or run the installer. The
installer adds **Open with** entries for `.h5` and `.hdf5`; selecting a default app
remains your choice. Packages are currently unsigned. Compare the download's
SHA-256 hash with the release's `SHA256SUMS.txt`:

```powershell
Get-FileHash .\H5Viewer-0.1.0-windows-x64-setup.exe -Algorithm SHA256
```

## Development

Use Python 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
uv run --locked h5-viewer [file.h5]
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```

On a headless Linux host, use `QT_QPA_PLATFORM=offscreen uv run --locked pytest`.
Source development and automated tests cover Linux and Windows; downloadable
desktop packages target Windows x64. macOS is not supported by the reader's
resource-enforcement implementation.

For bounded comparisons against local data:

```bash
uv run --locked python scripts/verify_real_files.py /path/to/file-or-folder --report artifacts/check.json
```

This compares selected source values and metadata; it is not a whole-file audit.

Real data, generated fixtures, builds, local settings, and run output must not be
committed. Tests generate their own synthetic data. See [contribution guidance](CONTRIBUTING.md)
and [security reporting](SECURITY.md).

## Architecture

See [architecture and reader protocol](docs/ARCHITECTURE.md). Sources are always
opened read-only. Expensive HDF5 work runs in a separate bounded reader process.
The initial view exposes file contents; datasets default to tables, not images.

## License and releases

H5 Viewer is [MIT licensed](LICENSE). Bundled dependencies retain their own
licenses; Windows packages include notices and library replacement instructions.
Matching dependency sources accompany binary releases.

Maintainers follow the [release procedure](docs/RELEASING.md) and the
[publication approval requirement](CONTRIBUTING.md#repository-working-agreement).
