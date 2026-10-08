"""Release trust-boundary tests use synthetic local assets and mocked GitHub calls."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).resolve().parents[1] / "scripts/release.py"
)
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)
SHA = "a" * 40
VERSION = "0.1.0"


@pytest.fixture
def candidate(tmp_path):
    names = [
        "H5Viewer-0.1.0-windows-x64-portable.zip",
        "H5Viewer-0.1.0-windows-x64-setup.exe",
        "SOURCE_REBUILD.md",
        "source-binary-map.json",
        *(f"source-{index}.tar.xz" for index in range(4)),
    ]
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "version": VERSION,
        "commit_sha": SHA,
        "platform": "windows-x64",
        "dirty": False,
        "artifacts": [
            {
                "name": n,
                "size": (tmp_path / n).stat().st_size,
                "sha256": release.digest(tmp_path / n),
            }
            for n in names
        ],
    }
    (tmp_path / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksums = "".join(
        f"{release.digest(tmp_path / name)}  {name}\n" for name in [*names, "release-manifest.json"]
    )
    (tmp_path / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    return tmp_path


def test_complete_candidate(candidate):
    assert len(release.validate_assets(candidate, VERSION, SHA)) == 10


@pytest.mark.parametrize(
    "field,value", [("commit_sha", "b" * 40), ("version", "0.2.0"), ("dirty", True)]
)
def test_manifest_identity_mismatch(candidate, field, value):
    path = candidate / "release-manifest.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="identity"):
        release.validate_assets(candidate, VERSION, SHA)


def test_tampered_package(candidate):
    (candidate / "H5Viewer-0.1.0-windows-x64-setup.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="size or hash mismatch"):
        release.validate_assets(candidate, VERSION, SHA)


def test_unexpected_data_is_never_uploaded(candidate):
    (candidate / "private.h5").write_bytes(b"test only")
    with pytest.raises(ValueError, match="exact manifest inventory"):
        release.validate_assets(candidate, VERSION, SHA)


def test_missing_sources(candidate):
    path = candidate / "release-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"].pop()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="corresponding sources"):
        release.validate_assets(candidate, VERSION, SHA)


def test_manifest_is_also_checksummed(candidate):
    path = candidate / "release-manifest.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="Checksum mismatch: release-manifest"):
        release.validate_assets(candidate, VERSION, SHA)


def test_duplicate_checksums_rejected(candidate):
    path = candidate / "SHA256SUMS.txt"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="duplicate checksum"):
        release.validate_assets(candidate, VERSION, SHA)


@pytest.mark.parametrize("existing", ["tag", "draft"])
def test_existing_tag_or_draft_blocks_all_writes(monkeypatch, existing):
    commands = []
    monkeypatch.setattr(release, "api", lambda *a, **kw: {} if existing == "tag" else None)

    def fake_run(*args):
        commands.append(args)
        assert args[:5] == ("gh", "api", "--paginate", "--slurp", "repos/owner/repo/releases")
        return json.dumps([[{"tag_name": "v0.1.0", "draft": True}]])

    monkeypatch.setattr(release, "run", fake_run)
    with pytest.raises(ValueError, match="already exists"):
        release.create_draft("owner/repo", VERSION, SHA, [])
    assert len(commands) == (1 if existing == "draft" else 0)


def test_api_failure_is_not_mistaken_for_absence(monkeypatch):
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr="API rate limit (HTTP 403)"),
    )
    with pytest.raises(RuntimeError, match="HTTP 403"):
        release.api("repos/owner/repo/git/ref/tags/v0.1.0", missing_ok=True)


@pytest.mark.parametrize("version", ["v0.1.0", "01.1.0", "0.1.0rc1", "0.1.0; echo unsafe"])
def test_version_validation_precedes_file_access(tmp_path, version):
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        release.validate_source(tmp_path, version, SHA)


def test_source_version_and_sha_must_match(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "src/h5_viewer").mkdir(parents=True)
    app = tmp_path / "src/h5_viewer/__init__.py"
    app.write_text('__version__ = "0.2.0"\n')
    monkeypatch.setattr(release, "run", lambda *a: SHA)
    with pytest.raises(ValueError, match="application versions"):
        release.validate_source(tmp_path, VERSION, SHA)
    app.write_text('__version__ = "0.1.0"\n')
    release.validate_source(tmp_path, VERSION, SHA)
    monkeypatch.setattr(release, "run", lambda *a: "b" * 40)
    with pytest.raises(ValueError, match="Checkout"):
        release.validate_source(tmp_path, VERSION, SHA)


@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped"])
def test_unsuccessful_codeql_blocks_release(monkeypatch, status):
    check = {
        "name": "CodeQL (Python)",
        "head_sha": SHA,
        "app": {"slug": "github-actions"},
        "id": 1,
        "status": "completed",
        "conclusion": status,
    }
    _mock_codeql(monkeypatch, check, conclusion=status)
    with pytest.raises(ValueError, match="CodeQL did not succeed"):
        release.wait_codeql("owner/repo", SHA, 0)


def test_codeql_requires_exact_sha(monkeypatch):
    check = {
        "name": "CodeQL (Python)",
        "head_sha": "b" * 40,
        "app": {"slug": "github-actions"},
        "id": 1,
        "status": "completed",
        "conclusion": "success",
    }
    _mock_codeql(monkeypatch, check)
    with pytest.raises(ValueError, match="Python analysis"):
        release.wait_codeql("owner/repo", SHA, 0)
    check["head_sha"] = SHA
    check["name"] = "Analyze (python)"
    release.wait_codeql("owner/repo", SHA, 0)


def test_missing_default_setup_fails_clearly(monkeypatch):
    monkeypatch.setattr(release, "api", lambda *a: {"workflows": []})
    with pytest.raises(ValueError, match="Enable CodeQL default setup"):
        release.wait_codeql("owner/repo", SHA, 0)


def test_codeql_lookup_reads_all_pages(monkeypatch):
    endpoints = []

    def fake_api(endpoint):
        endpoints.append(endpoint)
        return {"workflows": [{"id": 1}] * 100 if endpoint.endswith("page=1") else [{"id": 2}]}

    monkeypatch.setattr(release, "api", fake_api)
    items = release.api_items("repos/owner/repo/actions/workflows?per_page=100", "workflows")
    assert len(items) == 101 and items[-1]["id"] == 2
    assert endpoints[-1].endswith("&page=2")


def _mock_codeql(monkeypatch, check, conclusion="success"):
    def fake_api(endpoint):
        if "/actions/workflows?" in endpoint:
            return {"workflows": [{"path": "dynamic/github-code-scanning/codeql", "id": 1}]}
        if "/actions/workflows/1/runs?" in endpoint:
            assert f"head_sha={SHA}" in endpoint
            return {
                "workflow_runs": [
                    {
                        "head_sha": SHA,
                        "id": 2,
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": conclusion,
                        "check_suite_id": 3,
                    }
                ]
            }
        assert "/check-suites/3/check-runs?" in endpoint
        return {"check_runs": [check]}

    monkeypatch.setattr(release, "api", fake_api)


def test_draft_creation_pins_tag_and_never_publishes(monkeypatch, candidate):
    commands = []
    monkeypatch.setattr(release, "ensure_new", lambda *a: None)
    monkeypatch.setattr(release, "run", lambda *a: commands.append(a))
    assets = release.validate_assets(candidate, VERSION, SHA)
    release.create_draft("owner/repo", VERSION, SHA, assets)
    assert f"sha={SHA}" in commands[0]
    assert "ref=refs/tags/v0.1.0" in commands[0]
    assert "--draft" in commands[1] and "--verify-tag" in commands[1]
    assert list(commands[1][-len(assets) :]) == [str(path) for path in assets]
