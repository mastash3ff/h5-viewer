"""Assemble redistribution evidence from the actual Windows build; fail closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import shutil
import sys
import tarfile
import urllib.request
from importlib import metadata
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
from bundle_policy import QT_DLLS, include_qt_entry


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def get_sources(cache: Path, release: Path) -> list[dict]:
    config = json.loads((ROOT / "packaging/qt_sources.json").read_text())
    for package in ("PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6"):
        if metadata.version(package) != config["version"]:
            raise ValueError(f"Update corresponding-source pins for {package}")
    from PySide6.QtCore import qVersion

    if qVersion() != config["version"]:
        raise ValueError("Loaded Qt runtime differs from corresponding-source pins")
    cache.mkdir(parents=True, exist_ok=True)
    result = []
    for record in config["sources"]:
        url = record["url"]
        name = url.rsplit("/", 1)[1]
        target = cache / name
        # Revalidate the publisher's digest as well as our reviewed pin.
        with urllib.request.urlopen(url + ".sha256", timeout=120) as response:
            upstream = response.read().decode().split()[0]
        if upstream != record["sha256"]:
            raise ValueError(f"Upstream checksum changed for {name}")
        if not target.exists():
            temporary = target.with_suffix(".partial")
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as out:
                shutil.copyfileobj(response, out)
            temporary.replace(target)
        if sha256(target) != record["sha256"]:
            raise ValueError(f"Source checksum mismatch: {target}")
        shutil.copy2(target, release / name)
        result.append(
            dict(record, name=name, version=config["version"], checksum_url=url + ".sha256")
        )
    return result


def archive_notices(archive: Path, destination: Path) -> None:
    """Copy source license texts and attribution files without extracting executable paths.

    Include the module's complete source notice set, a conservative superset of the
    Windows runtime, retaining relative paths for upstream LicenseFile references.
    """
    with tarfile.open(archive) as source:
        members = {m.name: m for m in source if m.isfile()}
        selected = set()
        copyrights = set()
        for name, member in members.items():
            relative = PurePosixPath(name).parts[1:]
            if not relative:
                continue
            if relative[0] == "LICENSES" and "Commercial" not in name:
                selected.add(name)
            if name.endswith("qt_attribution.json"):
                selected.add(name)
                records = json.loads(source.extractfile(member).read(), strict=False)
                for record in records if isinstance(records, list) else [records]:
                    references = []
                    for key in ("LicenseFile", "CopyrightFile"):
                        if record.get(key):
                            references.append((key, record[key]))
                    for key in ("LicenseFiles", "CopyrightFiles"):
                        if key in record:
                            if not isinstance(record[key], list):
                                raise ValueError(f"Invalid upstream {key}: {name}")
                            references.extend((key, value) for value in record[key])
                    for key, filename in references:
                        ref = posixpath.normpath(posixpath.join(posixpath.dirname(name), filename))
                        if ref not in members:
                            raise ValueError(f"Unresolved upstream {key}: {name}: {filename}")
                        selected.add(ref)
            if PurePosixPath(name).suffix in {".cpp", ".h", ".py", ".c"}:
                header = source.extractfile(member).read(4096).decode("utf-8", errors="replace")
                for line in header.splitlines():
                    if (
                        "SPDX-FileCopyrightText:" in line
                        or "Copyright (C)" in line
                        or "Copyright (c)" in line
                    ):
                        copyrights.add(line.strip(" /*#"))
        if not copyrights:
            raise ValueError(f"No source copyright notices: {archive}")
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "SOURCE-COPYRIGHTS.txt").write_text(
            "Copyright notices from module source headers (includes unbundled source):\n"
            + "\n".join(sorted(copyrights))
            + "\n",
            encoding="utf-8",
        )
        for name in sorted(selected):
            relative = PurePosixPath(name).parts[1:]
            if ".." in relative:
                raise ValueError(f"Unsafe archive path: {name}")
            target = destination.joinpath(*relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.extractfile(members[name]).read())
        for license_name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
            if not (destination / "LICENSES" / license_name).is_file():
                raise ValueError(f"Missing full license: {archive}: {license_name}")


def collect_python_licenses(destination: Path, portable: Path) -> dict[str, str]:
    packages = ["numpy", "h5py", "pyqtgraph", "pyinstaller"]
    # Colorama is collected by pyqtgraph on Windows (check PyInstaller's module archive).
    from PyInstaller.archive.readers import ZlibArchiveReader

    archive = ROOT / "build/h5_viewer/PYZ-00.pyz"
    bundled_modules = ZlibArchiveReader(str(archive)).toc
    if "colorama" in bundled_modules:
        packages.append("colorama")
    versions = {}
    for name in packages:
        dist = metadata.distribution(name)
        versions[name] = dist.version
        matches = 0
        for entry in dist.files or ():
            path = PurePosixPath(str(entry).replace("\\", "/"))
            if not any(
                token in path.name.lower() for token in ("license", "licence", "copying", "notice")
            ):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file() or ".." in path.parts:
                continue
            target = destination / name / Path(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            matches += 1
        if not matches:
            raise ValueError(f"No licenses found for {name}")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise ValueError(f"Missing Python runtime license: {python_license}")
    shutil.copy2(python_license, destination / "PYTHON-LICENSE.txt")
    versions["python"] = sys.version.split()[0]
    for name in ("PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6"):
        versions[name] = metadata.version(name)
    import h5py

    versions["HDF5"] = h5py.version.hdf5_version
    return versions


def validate_inventory(portable: Path) -> list[dict]:
    import pefile

    binaries = sorted(
        p for p in portable.rglob("*") if p.suffix.lower() in {".exe", ".dll", ".pyd"}
    )
    names = {p.name.lower() for p in binaries}
    actual_qt = {name for name in names if name.startswith("qt6") and name.endswith(".dll")}
    if actual_qt != QT_DLLS:
        raise ValueError(f"Unexpected Qt DLL inventory: {sorted(actual_qt ^ QT_DLLS)}")
    result = []
    for binary in binaries:
        relative = binary.relative_to(portable).as_posix()
        if not include_qt_entry(relative):
            raise ValueError(f"Forbidden Qt binary: {relative}")
        pe = pefile.PE(str(binary), fast_load=True)
        try:
            pe.parse_data_directories(directories=[1, 13])
            imports = []
            for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
                for entry in getattr(pe, attribute, []):
                    dependency = entry.dll.decode("ascii").lower()
                    if dependency.startswith("qt6"):
                        imports.append(dependency)
                        if dependency not in names:
                            raise ValueError(f"Missing Qt dependency: {relative}: {dependency}")
        finally:
            pe.close()
        result.append(
            {"path": relative, "sha256": sha256(binary), "qt_imports": sorted(set(imports))}
        )
    for path in portable.rglob("*"):
        if not include_qt_entry(path.relative_to(portable).as_posix()):
            raise ValueError(f"Forbidden QML data: {path}")
    return result


def prepare(portable: Path, release: Path, cache: Path) -> None:
    release.mkdir(parents=True, exist_ok=True)
    inventory = validate_inventory(portable)
    sources = get_sources(cache, release)
    licenses = portable / "licenses"
    shutil.copytree(ROOT / "packaging/licenses", licenses, dirs_exist_ok=True)
    versions = collect_python_licenses(licenses, portable)
    for source in sources:
        archive_notices(cache / source["name"], licenses / source["module"])
    # PySide's binary wheels contain a commercial-license invitation; source LGPL
    # texts above are the redistribution texts used by this release.
    shutil.copy2(ROOT / "packaging/THIRD_PARTY_NOTICES.txt", portable)
    shutil.copy2(ROOT / "packaging/SOURCE_REBUILD.md", portable)
    for name in ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md"):
        shutil.copy2(ROOT / name, portable)
    shutil.copytree(ROOT / "docs", portable / "docs", dirs_exist_ok=True)
    mapped = []
    for binary in inventory:
        name = Path(binary["path"]).name.lower()
        module = None
        if name.startswith("qt6"):
            module = "qtsvg" if name == "qt6svg.dll" else "qtbase"
        elif "plugins/" in binary["path"].lower():
            if name in {"qsvg.dll", "qsvgicon.dll"}:
                module = "qtsvg"
            elif name in {"qicns.dll", "qtga.dll", "qtiff.dll", "qwbmp.dll", "qwebp.dll"}:
                module = "qtimageformats"
            else:
                module = "qtbase"
        elif name in {"pyside6.abi3.dll", "shiboken6.abi3.dll", "shiboken.pyd"} or (
            name.startswith("qt") and name.endswith(".pyd")
        ):
            module = "pyside-setup"
        binary["corresponding_source"] = next(
            (s["name"] for s in sources if s["module"] == module), None
        )
        mapped.append(binary)
    write_json(
        portable / "source-binary-map.json",
        {
            "schema_version": 1,
            "dependencies": versions,
            "corresponding_sources": sources,
            "binaries": mapped,
            "scope": "Qt/PySide LGPL sources; other binary notices are under licenses. "
            "Source notice collections include a conservative superset of Windows runtime notices.",
        },
    )
    for name in ("source-binary-map.json", "SOURCE_REBUILD.md"):
        shutil.copy2(portable / name, release / name)


def finalize(release: Path, version: str, commit: str, dirty: bool = False) -> None:
    mapping = json.loads((release / "source-binary-map.json").read_text())
    artifacts = []
    for path in sorted(release.iterdir()):
        if not path.is_file():
            raise ValueError(f"Unexpected release directory: {path}")
        if path.name in {"release-manifest.json", "SHA256SUMS.txt"}:
            continue
        kind = (
            "source"
            if path.name.endswith(".tar.xz")
            else "binary"
            if path.suffix in {".zip", ".exe"}
            else "documentation"
        )
        artifacts.append(
            {"name": path.name, "size": path.stat().st_size, "sha256": sha256(path), "kind": kind}
        )
    write_json(
        release / "release-manifest.json",
        {
            "schema_version": 1,
            "version": version,
            "platform": "windows-x64",
            "commit_sha": commit,
            "dirty": dirty,
            "artifacts": artifacts,
            "dependencies": mapping["dependencies"],
            "corresponding_sources": mapping["corresponding_sources"],
        },
    )
    (release / "SHA256SUMS.txt").write_text(
        "".join(
            f"{sha256(path)}  {path.name}\n"
            for path in sorted(release.iterdir())
            if path.name != "SHA256SUMS.txt"
        ),
        encoding="ascii",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--portable", type=Path, required=True)
    prep.add_argument("--release", type=Path, required=True)
    prep.add_argument("--cache", type=Path, required=True)
    finish = sub.add_parser("finalize")
    finish.add_argument("--release", type=Path, required=True)
    finish.add_argument("--version", required=True)
    finish.add_argument("--commit", required=True)
    finish.add_argument("--dirty", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.portable, args.release, args.cache)
    else:
        finalize(args.release, args.version, args.commit, args.dirty)


if __name__ == "__main__":
    main()
