import subprocess
import sys

import pytest

from h5_viewer.app import _check


def test_cli_help_does_not_require_display():
    result = subprocess.run(
        [sys.executable, "-m", "h5_viewer", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "Read-only HDF5" in result.stdout
    assert "--check" in result.stdout


def test_diagnostic_report_cannot_overwrite_source(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(b"preserve these bytes")
    with pytest.raises(ValueError, match="overwrite"):
        _check(str(source), str(source))
    assert source.read_bytes() == b"preserve these bytes"
