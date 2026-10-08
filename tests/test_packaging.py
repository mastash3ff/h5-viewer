from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "packaging" / "h5_viewer.iss").read_text(encoding="utf-8")
SPEC = (ROOT / "packaging" / "h5_viewer.spec").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "packaging" / "entrypoint.py").read_text(encoding="utf-8")


def test_frozen_entrypoint_initializes_multiprocessing_before_gui() -> None:
    assert ENTRYPOINT.index("multiprocessing.freeze_support()") < ENTRYPOINT.index(
        "from h5_viewer.app import main"
    )
    assert "console=False" in SPEC
    assert 'name="H5Viewer"' in SPEC


def test_installer_is_per_user_and_does_not_hijack_defaults() -> None:
    assert "PrivilegesRequired=lowest" in ISS
    assert "PrivilegesRequiredOverridesAllowed" not in ISS
    assert r"DefaultDirName={localappdata}\Programs\H5Viewer" in ISS
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in ISS
    assert 'Type: filesandordirs; Name: "{app}\\licenses"' in ISS
    assert 'Type: files; Name: "{app}\\USER_GUIDE.md"' in ISS
    registry_lines = [line for line in ISS.splitlines() if line.startswith("Root:")]
    assert all("UserChoice" not in line for line in registry_lines)
    assert r"Software\Classes\.h5\OpenWithProgids" in ISS
    assert r"Software\Classes\.hdf5\OpenWithProgids" in ISS
    assert r"Software\RegisteredApplications" in ISS
    assert r'ValueData: """{app}\{#MyAppExeName}"" ""%1"""' in ISS
    for line in ISS.splitlines():
        if "OpenWithProgids" in line or "SupportedTypes" in line:
            assert "ValueType: string" in line and 'ValueData: ""' in line


def test_installer_uninstall_only_deletes_owned_or_named_registration() -> None:
    for line in ISS.splitlines():
        if "Software\\RegisteredApplications" in line or "OpenWithProgids" in line:
            assert "uninsdeletevalue" in line
        if "Flags: uninsdeletekey" in line:
            assert (
                "Software\\H5Viewer" in line
                or "Software\\Classes\\Applications\\{#MyAppExeName}" in line
                or "Software\\Classes\\H5Viewer." in line
            )


def test_early_runtime_hook_diverts_frozen_worker_before_qt(monkeypatch) -> None:
    import multiprocessing
    import multiprocessing.spawn
    import runpy
    import sys

    import PyInstaller
    import pytest

    hook = Path(PyInstaller.__file__).parent / "hooks/rthooks/pyi_rth_multiprocessing.py"
    original_freeze = multiprocessing.freeze_support
    original_spawn_freeze = multiprocessing.spawn.freeze_support
    original_directory = multiprocessing.process.ORIGINAL_DIR
    calls = []
    monkeypatch.setattr(
        sys, "argv", ["H5Viewer.exe", "--multiprocessing-fork", "parent_pid=42", "pipe_handle=99"]
    )
    monkeypatch.setattr(multiprocessing.spawn, "spawn_main", lambda **kwargs: calls.append(kwargs))
    try:
        runpy.run_path(str(hook))
        with pytest.raises(SystemExit):
            runpy.run_path(str(ROOT / "packaging/early_multiprocessing.py"))
        assert calls == [{"parent_pid": 42, "pipe_handle": 99}]
        monkeypatch.setattr(sys, "argv", ["H5Viewer.exe", "--check"])
        runpy.run_path(str(ROOT / "packaging/early_multiprocessing.py"))
        assert calls == [{"parent_pid": 42, "pipe_handle": 99}]
    finally:
        multiprocessing.freeze_support = original_freeze
        multiprocessing.spawn.freeze_support = original_spawn_freeze
        multiprocessing.process.ORIGINAL_DIR = original_directory
