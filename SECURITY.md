# Security

Security fixes target the latest released version. Use GitHub's
[private vulnerability reporting](https://github.com/mastash3ff/h5-viewer/security/advisories/new)
for suspected vulnerabilities. Include the application version, operating system,
steps to reproduce, and a minimal synthetic fixture or generator. Do not include
credentials, restricted data, or private filesystem paths.

Ordinary bugs and feature requests belong in
[Issues](https://github.com/mastash3ff/h5-viewer/issues).

H5 Viewer opens data read-only and runs HDF5 operations in a separate process
with resource limits. These controls are not a security sandbox for untrusted
native libraries or custom filter plugins. External links and virtual datasets
can reference other local files; inspect the reported source paths before
following them. See [resource limits](docs/ARCHITECTURE.md).

Download binaries from this repository's Releases and verify their SHA-256
checksums. Windows packages are currently unsigned; checksums establish integrity
against the published release, not a Windows publisher certificate.
