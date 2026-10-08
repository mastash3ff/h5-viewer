# Contributing

Use Python 3.13 and `uv sync --locked`. Work in a branch and open a pull request
with a short description of the user-visible change and the checks you ran.

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

On a headless Linux machine, prefix the test command with
`QT_QPA_PLATFORM=offscreen`. CI also exercises native Windows and frozen packages.
See [architecture](docs/ARCHITECTURE.md) before changing the reader process,
resource limits, or file-access behavior.

Use small generated HDF5 fixtures in tests. Do not commit or attach user datasets,
credentials, machine-specific reports, or generated distributions. Bug reports
should include a minimal synthetic reproducer and redact private paths.

Dependency changes must update `uv.lock` and pass both platforms. Changes to
bundled dependencies also require updating source archives and license notices;
the Windows build validates these against its distribution inventory.

## Releases

Maintainers use the [release procedure](docs/RELEASING.md). Publishing requires
maintainer approval of the tested commit and release contents. CI creates drafts;
it does not automatically publish releases or merge dependency updates.

## Repository working agreement

Local builds and verification are allowed. Pushes, pull requests, shared-branch
merges, deployment, publication, and external messages require the user's separate
final approval after a reviewable local result. Installation and default-app changes
must be reported distinctly from portable-build verification.
