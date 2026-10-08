# Releasing H5 Viewer

Releases contain Windows x64 portable and per-user installer packages. They are
built from a specific Git commit using Python 3.13, the checked-in `uv.lock`,
PyInstaller, and Inno Setup 6. Windows binaries are currently unsigned. This
procedure does not publish to PyPI.

## Prepare a release

1. Update the project and displayed application versions together. Refresh
   `uv.lock` when the project version or dependencies change. Keep the versioned
   dependency-source inventory and corresponding notices aligned with the lock.
2. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md), build on Windows, and
   verify the extracted portable application with generated data. Review the
   distribution inventory, notices, source archives, and checksums.
3. Review the candidate commit and release notes. Obtain the final publication
   approval required by the repository working agreement before pushing or
   publishing. Approval for the specified commit and release carries through
   the following verification steps unless the scope changes.

Never copy local `artifacts/` reports or an old `dist/` directory into a release.
Build each candidate into a clean output directory; reports may contain local
file paths. Publish only the files listed in the new release manifest.

## Build the draft

After the approved commit is on `main`, run the **Release** workflow from `main`
with the package version, for example `0.1.0`. The workflow captures the commit
SHA, runs the reusable CI checks, and requires a successful CodeQL result for
that commit from GitHub's default-setup workflow. Test and build jobs have
read-only repository permissions. For example:

```bash
gh workflow run release.yml --repo mastash3ff/h5-viewer --ref main -f version=0.1.0
```

The separate draft-creation job consumes assets from that workflow invocation;
it does not execute the application. It creates `v<version>` at the tested commit
and a draft containing the portable ZIP, installer, manifests, checksums,
corresponding dependency sources, and rebuild instructions. It never publishes
the draft automatically.

Dispatches for a version are serialized. Existing tags, drafts, or releases are
rejected rather than overwritten. If a run fails, inspect the remote state before
retrying. A maintainer must explicitly resolve an incomplete draft or tag; never
delete or replace a published release to retry a build.

## Verify and publish

Before publication, verify all of the following against the reviewed candidate:

- The release tag resolves to the tested commit; all required CI and CodeQL
  checks succeeded for that commit.
- The asset list is complete and has no unexpected files. Download the draft
  assets and compare sizes and SHA-256 hashes with the manifest and
  `SHA256SUMS.txt`.
- The application version, package filenames, tag, and release notes agree.
- Notices and matching dependency sources are included. The draft's binary
  packages are the checked assets, not a later rebuild.

Publish the reviewed draft with release immutability enabled. Immutable releases
lock their assets and associated tags; assemble and verify every asset while the
release is still a draft. See [GitHub's immutable release guidance](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).

After publishing, confirm the repository and downloads are publicly accessible,
download the published assets again, and verify their checksums. Record the
release URL and source commit.

## Repository configuration

Enable GitHub Actions, CodeQL default setup for Python, dependency alerts and
security updates, secret scanning with push protection, and private vulnerability
reporting. Use CodeQL's default setup, not a second custom CodeQL workflow.
Dependabot opens weekly grouped minor/patch update pull requests for `uv` and
GitHub Actions; major updates remain separate and updates do not merge automatically.

Protect `main` with pull requests, successful CI checks, resolved conversations,
and blocked deletion/force pushes. The solo-maintainer configuration does not
require another reviewer. Configure required check names only after their first
successful runs are visible. Keep release immutability enabled.

| Required CI check | Purpose |
|---|---|
| `Quality and distributions` | Locked dependencies, lint/format, Python distributions |
| `Tests (ubuntu-24.04)` | Linux reader and GUI tests |
| `Tests (windows-2025)` | Windows reader and GUI tests |
| `Windows packages and smoke` | Frozen packages and disposable installer lifecycle |

The release gate separately resolves GitHub's generated default CodeQL workflow
and requires its successful Python analysis for the exact release commit.

## What automated checks establish

CI uses synthetic data to test the reader, production GUI, and packaged runtime.
Windows installer checks run only on disposable GitHub-hosted machines and cover
installation, same-version reinstall, owned registrations, and uninstall. A
same-version reinstall is not a cross-version upgrade test. Tests record whether
Windows default-app selections existed before checking their preservation.

These checks do not establish offline operation, physical mouse double-clicks,
or a Windows publisher signature. Distinguish those checks from automated
installation and GUI diagnostics when writing release notes.
