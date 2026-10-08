"""Validate an exact release candidate and optionally create a new GitHub draft.

Only the ``draft`` command writes to GitHub. Existing tags and releases are never
reused. A failed upload leaves its tag/draft for explicit maintainer recovery.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import time
import tomllib
from pathlib import Path


def run(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def api(endpoint: str, *, missing_ok: bool = False):
    result = subprocess.run(["gh", "api", endpoint], text=True, capture_output=True, check=False)
    if result.returncode:
        if missing_ok and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError(f"GitHub API failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def api_items(endpoint: str, key: str) -> list[dict]:
    """Read all pages; endpoints passed here must already include per_page=100."""
    items = []
    page = 1
    while True:
        batch = api(f"{endpoint}&page={page}")[key]
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def validate_source(root: Path, version: str, sha: str) -> None:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Version must be a stable MAJOR.MINOR.PATCH without a v prefix")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Commit must be a full lowercase Git SHA")
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tree = ast.parse((root / "src/h5_viewer/__init__.py").read_text(encoding="utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    if metadata["project"]["version"] != version or versions != [version]:
        raise ValueError("Requested version must match project and application versions")
    if run("git", "-C", str(root), "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout does not match the requested commit")


def ensure_new(repo: str, version: str) -> None:
    tag = f"v{version}"
    if api(f"repos/{repo}/git/ref/tags/{tag}", missing_ok=True) is not None:
        raise ValueError(f"Tag {tag} already exists; refusing to overwrite")
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases"))
    if any(release["tag_name"] == tag for page in pages for release in page):
        raise ValueError(f"Release or draft {tag} already exists; refusing to overwrite")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_assets(directory: Path, version: str, sha: str) -> list[Path]:
    manifest_path = directory / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("version") != version
        or manifest.get("commit_sha") != sha
        or manifest.get("platform") != "windows-x64"
        or manifest.get("dirty") is not False
    ):
        raise ValueError(
            "Release manifest identity does not match version, commit, platform, or clean source state"
        )
    artifacts = manifest["artifacts"]
    names = [item["name"] for item in artifacts]
    if len(names) != len(set(names)) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) for name in names
    ):
        raise ValueError("Duplicate or unsafe asset name")
    required = {
        f"H5Viewer-{version}-windows-x64-portable.zip",
        f"H5Viewer-{version}-windows-x64-setup.exe",
        "source-binary-map.json",
        "SOURCE_REBUILD.md",
    }
    if not required.issubset(names) or len([n for n in names if n.endswith(".tar.xz")]) != 4:
        raise ValueError("Required Windows packages or corresponding sources are missing")
    if set(names) - required != {n for n in names if n.endswith(".tar.xz")}:
        raise ValueError("Unexpected release asset type")
    expected = set(names) | {"release-manifest.json", "SHA256SUMS.txt"}
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError("Release directory does not match the exact manifest inventory")
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Release assets must be regular files")
    for item in artifacts:
        path = directory / item["name"]
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"Asset size or hash mismatch: {path.name}")
    checksums = {}
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not match or match[2] in checksums:
            raise ValueError("Invalid or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if set(checksums) != expected - {"SHA256SUMS.txt"}:
        raise ValueError("Checksum inventory does not match release assets")
    for name, checksum in checksums.items():
        if digest(directory / name) != checksum:
            raise ValueError(f"Checksum mismatch: {name}")
    return [directory / name for name in sorted(expected)]


def wait_codeql(repo: str, sha: str, timeout: int) -> None:
    workflows = api_items(f"repos/{repo}/actions/workflows?per_page=100", "workflows")
    # GitHub default setup owns this generated workflow. Its path and Python
    # check identity were verified through fastapi/fastapi's public Actions API.
    # Resolve the workflow, then its exact-SHA run and suite, rather than trusting
    # an unrelated check with a similar name on the commit.
    workflow = next(
        (w for w in workflows if w["path"] == "dynamic/github-code-scanning/codeql"), None
    )
    if workflow is None:
        raise ValueError("Enable CodeQL default setup before requesting a release")
    deadline = time.monotonic() + timeout
    while True:
        data = api_items(
            f"repos/{repo}/actions/workflows/{workflow['id']}/runs?head_sha={sha}&per_page=100",
            "workflow_runs",
        )
        runs = [r for r in data if r["head_sha"] == sha]
        if runs:
            latest = max(runs, key=lambda r: (r["id"], r["run_attempt"]))
            if latest["status"] == "completed":
                if latest["conclusion"] != "success":
                    raise ValueError(f"CodeQL did not succeed: {latest['conclusion']}")
                checks = api_items(
                    f"repos/{repo}/check-suites/{latest['check_suite_id']}/check-runs"
                    "?per_page=100&filter=latest",
                    "check_runs",
                )
                if any(
                    c["name"] == "Analyze (python)"
                    and c["head_sha"] == sha
                    and c["app"]["slug"] == "github-actions"
                    and c["status"] == "completed"
                    and c["conclusion"] == "success"
                    for c in checks
                ):
                    return
                raise ValueError("CodeQL run has no successful Python analysis for this commit")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"No successful CodeQL check for {sha} within the wait limit")
        time.sleep(min(15, max(0, deadline - time.monotonic())))


def create_draft(repo: str, version: str, sha: str, assets: list[Path]) -> None:
    # Check once more immediately before the first mutation. GitHub's ref creation
    # also fails atomically if another actor has created the tag in the meantime.
    ensure_new(repo, version)
    tag = f"v{version}"
    run("gh", "api", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}")
    run(
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--verify-tag",
        "--draft",
        "--title",
        f"H5 Viewer {version}",
        "--notes",
        (
            f"Windows x64 packages built from commit {sha}.\n\n"
            "Unsigned portable ZIP and per-user installer. Verify SHA256SUMS.txt. "
            "Corresponding Qt/PySide sources and rebuild instructions are attached.\n\n"
            "Maintainer review and separate final approval are required before publication."
        ),
        *(str(path) for path in assets),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "verify", "wait-codeql", "draft"])
    parser.add_argument("--version")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repo", default="mastash3ff/h5-viewer")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("Invalid repository name")
    if args.command == "wait-codeql":
        if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
            parser.error("Commit must be a full lowercase Git SHA")
        wait_codeql(args.repo, args.sha, args.timeout)
        return
    if args.version is None:
        parser.error("--version is required")
    validate_source(args.root, args.version, args.sha)
    if args.command == "preflight":
        ensure_new(args.repo, args.version)
    else:
        if args.assets is None:
            parser.error("--assets is required")
        assets = validate_assets(args.assets.resolve(), args.version, args.sha)
        if args.command == "draft":
            create_draft(args.repo, args.version, args.sha, assets)
    print(f"{args.command} succeeded for v{args.version} at {args.sha}")


if __name__ == "__main__":
    main()
