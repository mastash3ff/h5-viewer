"""Release checks that protect the shipped artifact and license boundaries."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_licenses", ROOT / "scripts/package_licenses.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def test_filters_hook_collected_restricted_modules_and_qml() -> None:
    for path in (
        "PySide6/Qt6VirtualKeyboard.dll",
        "PySide6/Qt6Pdf.dll",
        "PySide6/Qt6QmlModels.dll",
        "PySide6/Qt6Quick.dll",
        "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
        "PySide6/plugins/imageformats/qpdf.dll",
        "PySide6/qml/QtQuick/Foo.qml",
    ):
        assert not helper.include_qt_entry(path)
        assert not helper.include_qt_entry(path.replace("/", "\\"))
    for path in ("PySide6/Qt6Widgets.dll", "PySide6/plugins/platforms/qwindows.dll"):
        assert helper.include_qt_entry(path)


def make_archive(path: Path, files: dict[str, str]) -> None:
    with tarfile.open(path, "w:xz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo("module/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_notices_preserve_referenced_paths_and_full_texts(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.xz"
    make_archive(
        archive,
        {
            "LICENSES/LGPL-3.0-only.txt": "full LGPL test text",
            "LICENSES/GPL-3.0-only.txt": "full GPL test text",
            "LICENSES/LicenseRef-Qt-Commercial.txt": "commercial invitation",
            "src/a.cpp": "// Copyright (C) Example Author\n",
            "src/lib/qt_attribution.json": json.dumps({"LicenseFile": "../COPYING"}),
            "src/COPYING": "Actual dependency license",
        },
    )
    out = tmp_path / "notices"
    helper.archive_notices(archive, out)
    assert (out / "src/COPYING").read_text() == "Actual dependency license"
    assert (out / "LICENSES/LGPL-3.0-only.txt").read_text() == "full LGPL test text"
    assert "Example Author" in (out / "SOURCE-COPYRIGHTS.txt").read_text()
    assert not (out / "LICENSES/LicenseRef-Qt-Commercial.txt").exists()


def test_notices_reject_missing_upstream_license_reference(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.xz"
    make_archive(archive, {"src/qt_attribution.json": '{"LicenseFile": "missing"}'})
    with pytest.raises(ValueError, match="Unresolved upstream LicenseFile"):
        helper.archive_notices(archive, tmp_path / "notices")


def test_manifest_hashes_exact_output_and_records_dirty_source(tmp_path: Path) -> None:
    mapping = {"dependencies": {"python": "3.13"}, "corresponding_sources": []}
    (tmp_path / "source-binary-map.json").write_text(json.dumps(mapping))
    (tmp_path / "portable.zip").write_bytes(b"candidate")
    helper.finalize(tmp_path, "0.1.0", "a" * 40, True)
    manifest = json.loads((tmp_path / "release-manifest.json").read_text())
    assert manifest["dirty"] is True
    assert manifest["commit_sha"] == "a" * 40
    assert {x["name"] for x in manifest["artifacts"]} == {"portable.zip", "source-binary-map.json"}
    for line in (tmp_path / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    assert "release-manifest.json" in (tmp_path / "SHA256SUMS.txt").read_text()


@pytest.mark.parametrize("missing", [None, "second", "copyright"])
def test_notices_include_plural_licenses_and_copyright_files(tmp_path: Path, missing) -> None:
    archive = tmp_path / "source.tar.xz"
    files = {
        "LICENSES/LGPL-3.0-only.txt": "LGPL",
        "LICENSES/GPL-3.0-only.txt": "GPL",
        "src/a.cpp": "// Copyright (C) Example Author",
        "src/qt_attribution.json": json.dumps(
            {"LicenseFiles": ["first", "second"], "CopyrightFile": "copyright"}
        ),
        "src/first": "first license",
        "src/second": "second license",
        "src/copyright": "dependency copyright",
    }
    if missing:
        del files["src/" + missing]
    make_archive(archive, files)
    out = tmp_path / "notices"
    if missing:
        with pytest.raises(ValueError, match="Unresolved upstream"):
            helper.archive_notices(archive, out)
    else:
        helper.archive_notices(archive, out)
        assert (out / "src/second").read_text() == "second license"
        assert (out / "src/copyright").read_text() == "dependency copyright"
