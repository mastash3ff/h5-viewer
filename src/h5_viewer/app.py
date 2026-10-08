"""Application entry point; keep imports light for frozen worker startup."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

from h5_viewer import __version__


def _check(filename: str, report: str) -> int:
    """Exercise the packaged GUI runtime and isolated reader without source writes."""
    destination = Path(report).expanduser().resolve()
    source = Path(filename).expanduser().resolve()
    if destination.suffix.lower() != ".json":
        raise ValueError("Diagnostic report must have a .json extension")
    if destination == source or (
        destination.exists() and source.exists() and destination.samefile(source)
    ):
        raise ValueError("Diagnostic report cannot overwrite the source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = {"filename": str(Path(filename).resolve()), "ok": False}
    client = None
    deadline = time.monotonic() + 90
    candidates = {}
    try:
        import numpy as np
        from PySide6.QtWidgets import QApplication

        from h5_viewer.diagnostics import exercise_gui
        from h5_viewer.process import ReaderClient

        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setApplicationName("H5 Viewer")
        app.setApplicationVersion(__version__)
        result["application_version"] = app.applicationVersion()
        client = ReaderClient(filename)

        def request(command):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Diagnostic deadline exceeded")
            return client.request(command, timeout=min(30, remaining))

        result["root"] = request({"op": "describe", "path": "/"})
        result["children"] = request({"op": "children", "path": "/", "limit": 30})
        pending = list(result["children"]["items"])
        result["reads"] = []
        visits = 0
        while pending and len(result["reads"]) < 16 and visits < 100:
            item = pending.pop(0)
            visits += 1
            if item.get("link_kind", "hard").lower() in {"soft", "external"}:
                continue
            if item.get("kind") == "group" and item.get("expandable", True):
                pending.extend(
                    request({"op": "children", "path": item["path"], "limit": 30})["items"]
                )
            elif item.get("kind") == "dataset":
                shape = item.get("shape")
                if shape is None or any(n == 0 for n in shape):
                    continue
                selection = [0] * len(shape)
                if selection:
                    selection[-1] = [0, min(shape[-1], 16), 1]
                block = request({"op": "read", "path": item["path"], "selection": selection})
                array = np.asarray(block["array"])
                candidates.setdefault("table", item)
                if array.dtype.kind in "buifc" and shape:
                    candidates.setdefault("plot", item)
                    if len(shape) >= 2:
                        candidates.setdefault("image", item)
                result["reads"].append(
                    {
                        "path": item["path"],
                        "dtype": str(array.dtype),
                        "shape": list(array.shape),
                        "notes": block.get("notes", []),
                    }
                )
        result["reader"] = client.status
        result["qt_platform"] = app.platformName()
        client.close()
        result["gui"] = {}
        exercise_gui(app, str(source), candidates, result["gui"], deadline)
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary must persist reader errors
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            client.close()
    destination.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Read-only HDF5 desktop explorer")
    parser.add_argument("files", nargs="*", help="HDF5 files to inspect")
    parser.add_argument(
        "--check", metavar="FILE", help="Check GUI runtime and reader without editing FILE"
    )
    parser.add_argument("--report", metavar="JSON", help="Write diagnostic results for --check")
    args = parser.parse_args(argv)
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(key, "1")
    if args.check:
        if not args.report:
            parser.error("--check requires --report")
        return _check(args.check, args.report)

    # Set before importing NumPy in the parent or spawned children. Existing user
    # choices are respected; limiting library threads keeps reader startup bounded.
    from PySide6.QtWidgets import QApplication

    from h5_viewer.window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setOrganizationName("H5Viewer")
    app.setApplicationName("H5 Viewer")
    app.setApplicationVersion(__version__)
    windows = []
    for filename in args.files or [None]:
        window = MainWindow(filename)
        window.show()
        windows.append(window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
