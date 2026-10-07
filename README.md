# H5 Viewer

A general-purpose, read-only Windows HDF5 desktop explorer. Inspect file structure,
fields, values, attributes, datatypes, references, and source relationships. Tables,
plots, and images are alternative views of selected data.

See the [user guide](docs/USER_GUIDE.md) for fields, slices, metadata, source links,
plots, and image-stack navigation. See [Windows delivery](docs/WINDOWS.md) for the
portable application and per-user installer.

## Development

Use Python 3.13 and `uv sync --locked` to install the development environment.
Run `uv run h5-viewer [file.h5]`. Run `uv run pytest` for tests; on a headless
Linux host use `QT_QPA_PLATFORM=offscreen uv run pytest`.

Check style with `uv run ruff check .` and `uv run ruff format --check .`.
For bounded comparisons against local data, use:

```bash
uv run python scripts/verify_real_files.py /path/to/file-or-folder --report artifacts/check.json
```

This compares selected source values and metadata; it is not a whole-file audit.

The application is independent of the neighboring ATR repositories. Real data,
generated fixtures, builds, local settings, and run output must not be committed.
Only generated test definitions belong in this repository.

## Architecture

See [architecture and reader protocol](docs/ARCHITECTURE.md). Sources are always
opened read-only. Expensive HDF5 work runs in a separate bounded reader process.
The initial view exposes file contents; datasets default to tables, not images.

## Release approval

Local builds and verification are allowed. Pushes, pull requests, shared-branch
merges, deployment, publication, and external messages require the user's separate
final approval after a reviewable local result. Installation and default-app changes
must be reported distinctly from portable-build verification.
