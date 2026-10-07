"""Read-only application-reader comparisons against user-supplied HDF5 files.

Run with explicit files or directories. Reports are local artifacts, not source.
The test selects bounded first/middle/last regions; it is not a full data audit.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import time
from pathlib import Path


def main():
    import h5py
    import numpy as np

    from h5_viewer.process import ReaderClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    paths = []
    for value in args.inputs:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in {".h5", ".hdf5"}))
        else:
            paths.append(path)
    destination = Path(args.report).expanduser().resolve()
    if destination.suffix.lower() != ".json":
        parser.error("--report must have a .json extension")
    for path in paths:
        if destination == path.resolve() or (destination.exists() and destination.samefile(path)):
            parser.error("--report cannot overwrite a source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Bounded read-only comparisons, not a whole-file audit", "files": []}
    for path in dict.fromkeys(paths):
        before = path.stat()
        item = {"file": str(path), "bytes": before.st_size, "checks": [], "errors": []}
        started = time.monotonic()
        client = ReaderClient(str(path))
        try:
            with h5py.File(path, "r") as f:
                stack = ["/"]
                seen = set()
                while stack:
                    node = stack.pop()
                    address = h5py.h5o.get_info(f[node].id).addr
                    if address in seen:
                        continue
                    seen.add(address)
                    description = client.request({"op": "describe", "path": node})
                    if isinstance(f[node], h5py.Group):
                        offset = 0
                        while True:
                            children = client.request(
                                {"op": "children", "path": node, "offset": offset}
                            )
                            for entry in children["items"]:
                                if entry.get("kind") in {"group", "dataset"} and entry.get(
                                    "link_kind", "hard"
                                ).lower() not in {"soft", "external"}:
                                    stack.append(entry["path"])
                            offset += len(children["items"])
                            if not children.get("has_more"):
                                break
                        continue
                    if not isinstance(f[node], h5py.Dataset):
                        continue
                    dataset = f[node]
                    if dataset.shape is None or any(n == 0 for n in dataset.shape):
                        item["checks"].append({"path": node, "kind": "empty", "ok": True})
                        continue
                    assert description["shape"] == list(dataset.shape), (
                        node,
                        description["shape"],
                        dataset.shape,
                    )
                    # First/middle/last records; a narrow final-axis strip keeps
                    # allocation bounded while exercising every dataset's values.
                    positions = (
                        sorted({0, dataset.shape[0] // 2, dataset.shape[0] - 1})
                        if dataset.ndim
                        else [0]
                    )
                    for position in positions:
                        selection = [0] * dataset.ndim
                        if selection:
                            selection[0] = position
                            if dataset.ndim >= 2:
                                selection[-1] = [0, min(dataset.shape[-1], 24), 1]
                        actual = client.request(
                            {"op": "read", "path": node, "selection": selection}
                        )["array"]
                        index = tuple(slice(*s) if isinstance(s, list) else s for s in selection)
                        expected = dataset[index if index else ()]
                        if (
                            h5py.check_string_dtype(dataset.dtype)
                            and np.asarray(actual).dtype.kind in "OU"
                        ):
                            expected = dataset.asstr()[index if index else ()]
                        np.testing.assert_equal(actual, expected)
                        item["checks"].append({"path": node, "selection": selection, "ok": True})
                    client.request({"op": "sources", "path": node})
                    if dataset.ndim >= 2 and dataset.dtype.kind in "biufc":
                        selection = [0] * dataset.ndim
                        selection[-2] = [0, min(dataset.shape[-2], 256), 1]
                        selection[-1] = [0, min(dataset.shape[-1], 256), 1]
                        preview = client.request(
                            {"op": "read", "path": node, "selection": selection, "purpose": "image"}
                        )
                        actual_selection = preview.get("selection", selection)
                        index = tuple(
                            slice(*s) if isinstance(s, list) else s for s in actual_selection
                        )
                        np.testing.assert_equal(preview["array"], dataset[index])
                        item["checks"].append(
                            {
                                "path": node,
                                "purpose": "image",
                                "selection": actual_selection,
                                "ok": True,
                            }
                        )
                item["reader"] = client.status
        except Exception as exc:  # noqa: BLE001 - record per-file verification failures
            item["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            client.close()
        after = path.stat()
        item["source_metadata_unchanged"] = (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        )
        item["elapsed_seconds"] = round(time.monotonic() - started, 3)
        item["ok"] = not item["errors"] and item["source_metadata_unchanged"]
        report["files"].append(item)
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(
            f"{'PASS' if item['ok'] else 'FAIL'} {path.name}: {len(item['checks'])} selections",
            flush=True,
        )
    report["ok"] = bool(report["files"]) and all(item["ok"] for item in report["files"])
    destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
