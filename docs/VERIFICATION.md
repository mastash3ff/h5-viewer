# Verification record

Verification performed on 2026-10-07 for the initial 0.1.0 implementation.

## Automated and source-data checks

- Linux: 57 tests passed with Qt's offscreen platform. Tests cover reader values,
  source resolution, generic field workflows, resource enforcement, cancellation,
  recovery, UI state, entrypoint protection, and packaging configuration.
- Native Windows: 50 tests passed and seven Linux-specific checks were skipped.
  Windows-specific regressions verify rooted-path virtual-source fallback and
  viewport hover/click routing. Automated event injection does not establish
  physical mouse operation in a normal interactive desktop session.
- Generated cases include nested records, variable-length values, references,
  attributes, virtual datasets, external storage, exact large integers, and
  big-endian bitfields. Image previews are bounded; native probes remain exact.
- Read-only integration checks sampled 19 local HDF5 files from the representative
  sonar, snippet (including one backup), and synthetic surrogate release locations.
  All 1,168 selected comparisons matched independent h5py reads. File sizes and
  modification times remained unchanged. This is bounded sampling, not a complete
  audit or a byte-for-byte hash comparison of each source file.
- An independent implementation review examined the reader, process boundary,
  UI, and packaging. Confirmed findings were corrected and regression-tested;
  no material findings remained open at the final source review.

The native full-suite runner emitted a transient access-violation diagnostic while
continuing to successful completion. Targeted reruns and eight plain-process
source-resolution reads completed without that diagnostic. CPython's Windows
fault handler logs exceptions before subsequent native exception handlers run, so
the text alone does not establish process termination. No terminating crash was
observed; the original native exception's cause remains unconfirmed. See
[CPython's handler](https://github.com/python/cpython/blob/3.13/Modules/faulthandler.c#L348).

Generated fixtures, diagnostic reports, screenshots, and distribution binaries
remain outside Git under ignored `artifacts/` and `dist/` directories.

## Windows artifacts

The final frozen executable opened a Unicode/space-containing filename and read
gzip, LZF, complex, UTF-8 text, and nested compound datasets successfully. Its
diagnostic reported the Windows Qt platform and enforced 1 GiB commit and hard
working-set limits. The packaged GUI's root view and native Qt field-table
screenshots were visually inspected. Runtime dependencies are bundled; a
disconnected-network test was not performed.

SHA-256 for the locally built distributions:

```text
H5Viewer-0.1.0-windows-x64-portable.zip
814291a0d1a4f2479ffa38820ed03b76104b22544afd28dfcb8ab787717af6c2

H5Viewer-0.1.0-windows-x64-setup.exe
33021f2d6b4e88f7e47e1b15ba96d392c5444f42719141486073a218c29f60aa
```

## Installation boundary

Installer compilation and portable execution do not establish installed Explorer
integration. Installation, default-app selection, double-click, upgrade, and
uninstall checks require the separate release approval recorded in the workspace
working agreement. These checks have not been performed.

## Known scope limits

This is a generic HDF5 viewer, including non-image data and source relationships.
It does not include HDF4 or arbitrary vendor filter plugins. Oversized chunks or
requests are rejected with an explanation; some variable-length attributes require
an explicit read. Search covers names rather than every stored value. See the
[user guide](USER_GUIDE.md) and [resource limits](ARCHITECTURE.md).
