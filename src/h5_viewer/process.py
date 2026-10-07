"""Spawned, resource-bounded HDF5 access. This module must never import Qt."""

from __future__ import annotations

import io
import multiprocessing as mp
import os
import pickle
import sys
import threading
import time
from typing import Any, Self

MEMORY_LIMIT = 1024**3
RESULT_LIMIT = 64 * 1024**2
IPC_LIMIT = RESULT_LIMIT + 1024**2
REQUEST_LIMIT = 1024**2


class ReaderError(RuntimeError):
    """A contextual reader, resource, transport, or cancellation failure."""


class _LimitedBuffer(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + memoryview(data).nbytes > IPC_LIMIT:
            raise ReaderError("Result exceeds the 64 MiB IPC result limit")
        return super().write(data)


def _encode(message: dict) -> bytes:
    stream = _LimitedBuffer()
    pickle.Pickler(stream, protocol=5).dump(message)
    return stream.getvalue()


def _windows_limit() -> tuple[dict, Any]:
    """Enforce commit and working-set ceilings; retain the job until process exit."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IOCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.SetProcessWorkingSetSizeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_size_t,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
    kernel.GetProcessWorkingSetSizeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetProcessWorkingSetSizeEx.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        # PROCESS_MEMORY | JOB_MEMORY | KILL_ON_JOB_CLOSE. Job WORKINGSET
        # requires privileges ordinary desktop users may not possess. Enforce
        # the working-set ceiling separately with the explicit HARDWS_MAX flag.
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x200 | 0x2000
        limits.ProcessMemoryLimit = MEMORY_LIMIT
        limits.JobMemoryLimit = MEMORY_LIMIT
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        actual = ExtendedLimits()
        if not kernel.QueryInformationJobObject(
            job, 9, ctypes.byref(actual), ctypes.sizeof(actual), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            actual.ProcessMemoryLimit != MEMORY_LIMIT
            or actual.JobMemoryLimit != MEMORY_LIMIT
            or actual.BasicLimitInformation.LimitFlags & 0x2300 != 0x2300
        ):
            raise ReaderError("Windows did not retain the required job memory limits")
        current = kernel.GetCurrentProcess()
        minimum, maximum, flags = ctypes.c_size_t(), ctypes.c_size_t(), wintypes.DWORD()
        if not kernel.GetProcessWorkingSetSizeEx(
            current, ctypes.byref(minimum), ctypes.byref(maximum), ctypes.byref(flags)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # HARDWS_MIN_DISABLE | HARDWS_MAX_ENABLE; preserve the existing minimum.
        if not kernel.SetProcessWorkingSetSizeEx(current, minimum.value, MEMORY_LIMIT, 0x6):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetProcessWorkingSetSizeEx(
            current, ctypes.byref(minimum), ctypes.byref(maximum), ctypes.byref(flags)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if maximum.value > MEMORY_LIMIT or not flags.value & 0x4:
            raise ReaderError("Windows did not retain the hard working-set ceiling")
        # Assign only after all other checks: closing an assigned kill-on-close
        # job on bootstrap failure would terminate us before reporting the error.
        if not kernel.AssignProcessToJobObject(job, current):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "mechanism": "Windows Job Object",
            "memory_limit": MEMORY_LIMIT,
            "commit_limit": MEMORY_LIMIT,
            "working_set_limit": maximum.value,
            "working_set_hard": True,
        }, job
    except BaseException:
        kernel.CloseHandle(job)
        raise


def _enforce_limit() -> tuple[dict, Any]:
    if sys.platform == "win32":
        return _windows_limit()
    if sys.platform.startswith("linux"):
        import resource

        import h5py  # noqa: F401

        # Import the numerical runtime before measuring its reserved mappings.
        # Thread controls are local to the already-spawned child.
        import numpy  # noqa: F401

        with open("/proc/self/statm", encoding="ascii") as stream:
            baseline = int(stream.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
        desired = baseline + MEMORY_LIMIT
        _, existing_hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = (
            desired if existing_hard == resource.RLIM_INFINITY else min(desired, existing_hard)
        )
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        actual = resource.getrlimit(resource.RLIMIT_AS)
        if actual != (ceiling, ceiling):
            raise ReaderError("RLIMIT_AS memory enforcement did not take effect")
        return {
            "mechanism": "RLIMIT_AS",
            "memory_limit": MEMORY_LIMIT,
            "baseline_virtual_bytes": baseline,
            "address_space_limit": ceiling,
            "allocation_budget": max(0, ceiling - baseline),
        }, None
    raise ReaderError(f"Memory enforcement is unsupported on {sys.platform}")


def _context(request: dict) -> str:
    return f"{request.get('op', '?')} {request.get('path', '/')}"


def _exit_with_parent() -> None:
    """A worker must not retain a file if the GUI process exits unexpectedly."""
    from multiprocessing.connection import wait

    parent = mp.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        os._exit(1)


def _worker(connection: Any, filename: str) -> None:
    # Do not change the parent environment: these settings apply only in this child.
    for name in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ[name] = "1"
    reader = None
    job = None
    try:
        limits, job = _enforce_limit()
        threading.Thread(target=_exit_with_parent, name="reader-parent-watch", daemon=True).start()
        from .reader import Reader

        reader = Reader(filename)
        connection.send_bytes(
            _encode(
                {
                    "id": 0,
                    "ready": True,
                    "status": {
                        **limits,
                        "platform": sys.platform,
                        "enforced": True,
                        "pid": os.getpid(),
                        "ipc_limit": IPC_LIMIT,
                        "result_limit": RESULT_LIMIT,
                        "qt_loaded": any(name.startswith("PySide6") for name in sys.modules),
                    },
                }
            )
        )
        while True:
            message = pickle.loads(connection.recv_bytes(REQUEST_LIMIT))
            if message.get("close"):
                break
            correlation = message["id"]
            request = message["request"]
            try:
                response = reader.handle(request)
                encoded = _encode({"id": correlation, "result": response})
            except Exception as exc:  # noqa: BLE001 - all reader failures become item errors
                encoded = _encode(
                    {
                        "id": correlation,
                        "error": f"{_context(request)}: {type(exc).__name__}: {exc}",
                    }
                )
            connection.send_bytes(encoded)
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:  # noqa: BLE001 - report even a failed worker bootstrap
        try:
            connection.send_bytes(
                _encode(
                    {
                        "id": 0,
                        "error": f"Reader startup/worker failure: {type(exc).__name__}: {exc}",
                    }
                )
            )
        except (OSError, MemoryError):
            pass
    finally:
        if reader is not None:
            reader.close()
        connection.close()
        # Keep the Windows job handle alive through interpreter teardown.
        # Closing a KILL_ON_JOB_CLOSE job here would kill this very process.
        _ = job


class ReaderClient:
    """One file and one in-flight request; cancellation never waits on a request lock."""

    def __init__(self, filename: str | os.PathLike[str]):
        self.filename = os.fspath(filename)
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._process: Any = None
        self._connection: Any = None
        self._generation = 0
        self._sequence = 0
        self._closed = False
        self._status: dict = {
            "enforced": False,
            "platform": sys.platform,
            "memory_limit": MEMORY_LIMIT,
        }
        self._start(time.monotonic() + 30)

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                **self._status,
                "running": self._process is not None and self._process.is_alive(),
                "closed": self._closed,
            }

    @staticmethod
    def _reap(process: Any, connection: Any) -> None:
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()
        if connection is not None:
            connection.close()

    def _detach(self, generation: int | None = None) -> tuple[Any, Any]:
        with self._lock:
            if generation is not None and generation != self._generation:
                return None, None
            process, connection = self._process, self._connection
            self._process = self._connection = None
            self._generation += 1
            return process, connection

    def _receive(self, generation: int, process: Any, connection: Any, deadline: float) -> dict:
        while True:
            with self._lock:
                if generation != self._generation:
                    raise ReaderError(
                        "Reader request canceled" if not self._closed else "Reader client is closed"
                    )
                if time.monotonic() >= deadline:
                    raise ReaderError("Reader request timed out; worker stopped")
            try:
                if connection.poll(0):
                    message = pickle.loads(connection.recv_bytes(IPC_LIMIT))
                    with self._lock:
                        if generation != self._generation:
                            raise ReaderError("Reader request canceled")
                    return message
                if not process.is_alive():
                    raise ReaderError(
                        f"Reader worker exited (code {process.exitcode}); "
                        "possible memory limit or native library failure"
                    )
            except (EOFError, OSError, ValueError) as exc:
                with self._lock:
                    if generation != self._generation:
                        raise ReaderError("Reader request canceled") from exc
                raise ReaderError(
                    "Reader worker disconnected; possible memory limit or native library failure"
                ) from exc
            time.sleep(0.01)

    def _start(self, deadline: float) -> tuple[int, Any, Any]:
        with self._lock:
            if self._closed:
                raise ReaderError("Reader client is closed")
            if self._process is not None and self._process.is_alive():
                return self._generation, self._process, self._connection
            stale = self._detach()
            expected_generation = self._generation
        self._reap(*stale)
        with self._lock:
            if expected_generation != self._generation:
                raise ReaderError("Reader startup canceled")
            if self._closed:
                raise ReaderError("Reader client is closed")
            parent, child = mp.get_context("spawn").Pipe(duplex=True)
            process = mp.get_context("spawn").Process(
                target=_worker, args=(child, self.filename), daemon=True
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                raise
            child.close()
            self._generation += 1
            generation = self._generation
            self._process, self._connection = process, parent
        try:
            message = self._receive(generation, process, parent, deadline)
            if message.get("id") != 0 or not message.get("ready"):
                raise ReaderError(message.get("error", "Invalid reader startup response"))
            with self._lock:
                if generation != self._generation:
                    raise ReaderError("Reader startup canceled")
                self._status = message["status"]
            return generation, process, parent
        except BaseException:
            self._reap(*self._detach(generation))
            raise

    def request(self, request: dict, timeout: float = 30) -> dict:
        if not isinstance(request, dict):
            raise ReaderError("Reader request must be a dictionary")
        if timeout <= 0:
            raise ReaderError("Reader request timeout must be positive")
        if not self._request_lock.acquire(blocking=False):
            raise ReaderError("Another reader request is already active")
        generation = None
        watchdog = None
        finished = threading.Event()
        expired = threading.Event()
        try:
            deadline = time.monotonic() + timeout
            generation, process, connection = self._start(deadline)

            def expire() -> None:
                # Pipe send/receive may block after poll (large or partial frames).
                # Enforce the deadline independently by closing the other endpoint.
                with self._lock:
                    if finished.is_set():
                        return
                    expired.set()
                    stopped = self._detach(generation)
                self._reap(*stopped)

            watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
            watchdog.daemon = True
            watchdog.start()
            with self._lock:
                if generation != self._generation:
                    raise ReaderError("Reader request canceled")
                self._sequence += 1
                correlation = self._sequence
            payload = _encode({"id": correlation, "request": request})
            if len(payload) > REQUEST_LIMIT:
                raise ReaderError("Reader request exceeds the 1 MiB limit")
            connection.send_bytes(payload)
            response = self._receive(generation, process, connection, deadline)
            if response.get("id") != correlation:
                raise ReaderError(response.get("error", "Reader reply correlation mismatch"))
            if "error" in response:
                raise ReaderError(response["error"])
            with self._lock:
                if expired.is_set():
                    raise ReaderError("Reader request timed out; worker stopped")
                finished.set()
            return response["result"]
        except (ReaderError, OSError, EOFError, ValueError) as exc:
            if generation is not None:
                self._reap(*self._detach(generation))
            if expired.is_set():
                raise ReaderError(
                    f"{_context(request)}: Reader request timed out; worker stopped"
                ) from exc
            if isinstance(exc, ReaderError):
                prefix = f"{_context(request)}: "
                raise ReaderError(
                    str(exc) if str(exc).startswith(prefix) else prefix + str(exc)
                ) from exc
            raise ReaderError(f"{_context(request)}: Reader transport failure: {exc}") from exc
        finally:
            with self._lock:
                finished.set()
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()
            self._request_lock.release()

    def cancel(self) -> None:
        self._reap(*self._detach())

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
