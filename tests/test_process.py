"""Observable process isolation and recovery checks using actual spawned children."""

import multiprocessing as mp
import os
import signal
import sys
import threading
import time

import h5py
import numpy as np
import pytest

import h5_viewer.process as process_module
from h5_viewer.process import (
    IPC_LIMIT,
    MEMORY_LIMIT,
    ReaderClient,
    ReaderError,
    _encode,
    _enforce_limit,
)
from h5_viewer.process import (
    _worker as real_worker,
)


@pytest.fixture
def process_file(tmp_path):
    path = tmp_path / "reader_測試.h5"
    with h5py.File(path, "w") as file:
        file["numbers"] = np.arange(48).reshape(6, 8)
    return path


def _wait_for_active(client):
    deadline = time.monotonic() + 5
    while not client._request_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client._request_lock.locked()


def test_roundtrip_enforcement_and_read_only(process_file):
    before = process_file.read_bytes()
    parent_threads = os.environ.get("OPENBLAS_NUM_THREADS")
    with ReaderClient(process_file) as client:
        state = client.status
        assert state["enforced"] and state["running"]
        assert state["memory_limit"] == MEMORY_LIMIT
        assert state["pid"] != os.getpid()
        assert not state["qt_loaded"]
        response = client.request(
            {"op": "read", "path": "/numbers", "selection": [[1, 3, 1], [2, 5, 1]]}
        )
        np.testing.assert_array_equal(response["array"], np.arange(48).reshape(6, 8)[1:3, 2:5])
        if sys.platform == "win32":
            assert state["working_set_hard"]
            assert state["commit_limit"] == state["working_set_limit"] == MEMORY_LIMIT
        else:
            assert state["address_space_limit"] <= state["baseline_virtual_bytes"] + MEMORY_LIMIT
    assert process_file.read_bytes() == before
    assert os.environ.get("OPENBLAS_NUM_THREADS") == parent_threads
    assert not client.status["running"]
    # Windows rejects this open if the worker retained an incompatible handle.
    with h5py.File(process_file, "r+") as file:
        assert file["numbers"].shape == (6, 8)
    with pytest.raises(ReaderError, match="closed"):
        client.request({"op": "describe"})


def test_read_error_context_and_next_request_recovery(process_file):
    with ReaderClient(process_file) as client:
        with pytest.raises(ReaderError, match="read /missing"):
            client.request({"op": "read", "path": "/missing"})
        assert client.request({"op": "describe", "path": "/numbers"})["shape"] == [6, 8]


def test_cancel_and_restart(process_file):
    with ReaderClient(process_file) as client:
        original = client.status["pid"]
        client.cancel()
        assert not client.status["running"]
        assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"
        assert client.status["pid"] != original


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP provides deterministic in-flight work")
@pytest.mark.parametrize("action", ["cancel", "kill", "close", "timeout"])
def test_inflight_interrupt_and_recovery(process_file, action):
    client = ReaderClient(process_file)
    process = client._process
    errors = []
    os.kill(process.pid, signal.SIGSTOP)

    def request():
        try:
            client.request(
                {"op": "describe", "path": "/numbers"}, timeout=0.15 if action == "timeout" else 5
            )
        except ReaderError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=request)
    thread.start()
    try:
        _wait_for_active(client)
        with pytest.raises(ReaderError, match="already active"):
            client.request({"op": "describe"})
        if action == "kill":
            process.kill()
        elif action == "cancel":
            client.cancel()
        elif action == "close":
            client.close()
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert errors
        assert "describe /numbers" in errors[0]
        if action in ("cancel", "close"):
            assert "cancel" in errors[0] or "closed" in errors[0]
        if action == "timeout":
            assert "timed out" in errors[0]
        if action != "close":
            assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"
    finally:
        client.close()
        thread.join(timeout=4)


def test_result_and_request_limits(process_file):
    with pytest.raises(ReaderError, match="IPC result limit"):
        _encode({"result": b"x" * (IPC_LIMIT + 1)})
    with ReaderClient(process_file) as client:
        with pytest.raises(ReaderError, match="request exceeds"):
            client.request({"op": "describe", "unused": "x" * (1024**2 + 1)})
        assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"


def _allocation_probe(connection):
    try:
        status, job = _enforce_limit()
        assert status["memory_limit"] == MEMORY_LIMIT
        try:
            allocation = bytearray(MEMORY_LIMIT * 2)
        except (MemoryError, OSError):
            connection.send({"enforced": True, "rejected": True, "status": status})
        else:
            connection.send({"enforced": True, "rejected": False})
            del allocation
        _ = job
    finally:
        connection.close()


def test_os_rejects_allocation_above_enforced_budget():
    parent, child = mp.get_context("spawn").Pipe(duplex=False)
    process = mp.get_context("spawn").Process(target=_allocation_probe, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(20), "Resource probe did not report"
        result = parent.recv()
        assert result["enforced"] and result["rejected"]
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        parent.close()
        process.close()


def test_missing_file_reports_startup_failure_without_orphan(tmp_path):
    before = {process.pid for process in mp.active_children()}
    with pytest.raises(ReaderError, match="startup/worker failure"):
        ReaderClient(tmp_path / "missing.h5")
    assert {process.pid for process in mp.active_children()} == before


class _OversizedReader:
    def __init__(self, filename):
        pass

    def handle(self, request):
        return {"payload": b"x" * (IPC_LIMIT + 1)}

    def close(self):
        pass


def _oversized_worker(connection, filename):
    # Inject the reader only in this spawned test process. The production process
    # enforces its OS cap before constructing this deliberately oversized result.
    import h5_viewer.reader as reader_module

    reader_module.Reader = _OversizedReader
    real_worker(connection, filename)


def test_worker_rejects_oversized_ipc_result_and_recovers(process_file, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(process_module, "_worker", _oversized_worker)
        client = ReaderClient(process_file)
    try:
        assert client.status["enforced"]
        with pytest.raises(ReaderError, match="IPC result limit"):
            client.request({"op": "read", "path": "/numbers"})
        assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"
    finally:
        client.close()


def _client_owner(connection, filename):
    with ReaderClient(filename) as client:
        connection.send(client.status["pid"])
        connection.recv()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Checks Linux process exit state")
def test_worker_exits_when_owner_dies(process_file):
    parent, child = mp.get_context("spawn").Pipe()
    owner = mp.get_context("spawn").Process(target=_client_owner, args=(child, process_file))
    owner.start()
    child.close()
    worker_pid = None
    try:
        assert parent.poll(20)
        worker_pid = parent.recv()
        owner.kill()
        owner.join(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{worker_pid}/stat", encoding="ascii") as stream:
                    state = stream.read().rsplit(")", 1)[1].split()[0]
                if state == "Z":
                    break  # terminated; its adoption/reaping belongs to the OS
            except FileNotFoundError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Reader worker survived its owner")
    finally:
        if owner.is_alive():
            owner.kill()
            owner.join(timeout=5)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        parent.close()
        owner.close()


def test_large_numpy_pickle_buffer_roundtrip(process_file):
    values = np.arange(256 * 256, dtype="float32").reshape(256, 256)
    with h5py.File(process_file, "r+") as file:
        file["large"] = values
    with ReaderClient(process_file) as client:
        response = client.request(
            {"op": "read", "path": "/large", "selection": [[0, 256, 1], [0, 256, 1]]}
        )
        np.testing.assert_array_equal(response["array"], values)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP deterministically blocks pipe send")
def test_deadline_interrupts_blocked_pipe_send(process_file):
    with ReaderClient(process_file) as client:
        os.kill(client.status["pid"], signal.SIGSTOP)
        started = time.monotonic()
        with pytest.raises(ReaderError, match="timed out"):
            client.request({"op": "describe", "padding": "x" * 900_000}, timeout=0.1)
        assert time.monotonic() - started < 2
        assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"


class _PartialConnection:
    def __init__(self, connection):
        self.connection = connection
        self.ready_sent = False

    def send_bytes(self, message):
        if not self.ready_sent:
            self.ready_sent = True
            return self.connection.send_bytes(message)
        import struct

        os.write(self.connection.fileno(), struct.pack("!i", len(message)) + message[:10])
        time.sleep(60)

    def recv_bytes(self, limit):
        return self.connection.recv_bytes(limit)

    def close(self):
        self.connection.close()


def _partial_worker(connection, filename):
    real_worker(_PartialConnection(connection), filename)


@pytest.mark.skipif(sys.platform == "win32", reason="Injects a partial POSIX pipe frame")
def test_deadline_interrupts_partial_response(process_file, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(process_module, "_worker", _partial_worker)
        client = ReaderClient(process_file)
    try:
        started = time.monotonic()
        with pytest.raises(ReaderError, match="timed out"):
            client.request({"op": "describe", "path": "/numbers"}, timeout=0.1)
        assert time.monotonic() - started < 2
        assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"
    finally:
        client.close()


class _SlowReader(_OversizedReader):
    def handle(self, request):
        time.sleep(60)
        return {}


def _slow_worker(connection, filename):
    import h5_viewer.reader as reader_module

    reader_module.Reader = _SlowReader
    real_worker(connection, filename)


@pytest.mark.parametrize("action", ["cancel", "kill", "close", "timeout"])
def test_inflight_recovery_on_all_platforms(process_file, monkeypatch, action):
    with monkeypatch.context() as scoped:
        scoped.setattr(process_module, "_worker", _slow_worker)
        client = ReaderClient(process_file)
    errors = []

    def request():
        try:
            client.request(
                {"op": "describe", "path": "/numbers"}, timeout=0.2 if action == "timeout" else 5
            )
        except ReaderError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=request)
    thread.start()
    try:
        _wait_for_active(client)
        with pytest.raises(ReaderError, match="already active"):
            client.request({"op": "describe"})
        if action == "cancel":
            client.cancel()
        elif action == "kill":
            client._process.kill()
        elif action == "close":
            client.close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert errors
        if action == "timeout":
            assert "timed out" in errors[0]
        if action in ("cancel", "close"):
            assert "cancel" in errors[0] or "closed" in errors[0]
        if action != "close":
            assert client.request({"op": "describe", "path": "/numbers"})["kind"] == "dataset"
    finally:
        client.close()
        thread.join(timeout=3)
