"""Diagnostics must exercise real widgets and fail on a broken production view."""

import hashlib
import json
import multiprocessing
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

from h5_viewer import __version__
from h5_viewer.app import _check


def test_release_fixture_exercises_all_views_without_source_writes(tmp_path):
    source = tmp_path / "café data" / "fixture sample.h5"
    fixture_script = Path(__file__).resolve().parents[1] / "scripts" / "create_smoke_fixture.py"
    subprocess.run([sys.executable, str(fixture_script), str(source)], check=True, timeout=30)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = tmp_path / "result.json"
    run = subprocess.run(
        [sys.executable, "-m", "h5_viewer", "--check", str(source), "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    result = json.loads(report.read_text())
    assert run.returncode == 0, (result, run.stderr)
    assert result["ok"]
    assert result["application_version"] == __version__
    assert {item["path"] for item in result["reads"]} == {
        "/gzip",
        "/lzf",
        "/complex",
        "/text",
        "/compound",
    }
    assert result["gui"]["window_created"] and result["gui"]["closed"]
    assert result["gui"]["contents"]["rendered"]
    for view in result["gui"]["views"].values():
        assert view["status"] == "passed" and view["rendered"]
        assert view["width"] > 0 and view["height"] > 0
    for reader in (result["reader"], result["gui"]["reader"]):
        assert reader["enforced"] and reader["memory_limit"] == 1024**3
        assert not reader["qt_loaded"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("kind", ["text", "vector", "empty", "scalar"])
def test_unsupported_views_are_skipped(qapp, tmp_path, kind):
    source = tmp_path / "input.h5"
    with h5py.File(source, "w") as file:
        if kind == "text":
            file["data"] = [b"one", b"two"]
        elif kind == "vector":
            file["data"] = np.arange(8)
        elif kind == "scalar":
            file["data"] = 42
    report = tmp_path / "report.json"
    assert _check(str(source), str(report)) == 0
    result = json.loads(report.read_text())
    assert result["gui"]["views"]["image"]["status"] == "skipped"
    assert result["gui"]["views"]["table"]["status"] == ("skipped" if kind == "empty" else "passed")


def test_broken_plot_fails_report_and_closes_worker(qapp, tmp_path, monkeypatch):
    from h5_viewer.ui_data import PlotPanel

    source = tmp_path / "input.h5"
    with h5py.File(source, "w") as file:
        file["values"] = np.arange(8)

    def fail(*args):
        raise ValueError("Deliberately broken production plot")

    monkeypatch.setattr(PlotPanel, "set_result", fail)
    report = tmp_path / "report.json"
    assert _check(str(source), str(report)) == 1
    result = json.loads(report.read_text())
    assert not result["ok"] and "broken production plot" in result["error"]
    assert result["gui"]["closed"]
    assert not multiprocessing.active_children()


def test_invalid_source_writes_failure_report(qapp, tmp_path):
    report = tmp_path / "report.json"
    assert _check(str(tmp_path / "missing.h5"), str(report)) == 1
    result = json.loads(report.read_text())
    assert not result["ok"] and result["error"]


def test_gui_deadline_cancels_worker(qapp, tmp_path):
    import time

    from h5_viewer.diagnostics import exercise_gui

    source = tmp_path / "input.h5"
    with h5py.File(source, "w"):
        pass
    outcome = {}
    with pytest.raises(TimeoutError, match="deadline"):
        exercise_gui(qapp, str(source), {}, outcome, time.monotonic() - 1)
    assert outcome["closed"]
    assert not multiprocessing.active_children()
