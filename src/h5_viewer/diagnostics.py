"""Bounded diagnostics through the real desktop components and reader process."""

from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory


def exercise_gui(app, filename: str, candidates: dict, outcome: dict, deadline: float) -> None:
    """Render supported views; unsupported views remain explicitly skipped."""
    from PySide6 import QtCore

    from .window import MainWindow

    outcome.update(window_created=False, closed=False, views={})
    window = None
    dispatcher = None

    def wait_for(predicate):
        while True:
            app.processEvents()
            if window.error.isVisible():
                raise RuntimeError(window.error.text())
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("GUI diagnostic deadline exceeded")
            time.sleep(0.01)

    def render(widget):
        app.processEvents()
        snapshot = widget.grab()
        if snapshot.isNull() or snapshot.width() <= 0 or snapshot.height() <= 0:
            raise RuntimeError("GUI widget produced an empty render")
        return {"rendered": True, "width": snapshot.width(), "height": snapshot.height()}

    with TemporaryDirectory(prefix="h5-viewer-check-") as settings_dir:
        try:
            window = MainWindow()
            # Diagnostics must not change recents, geometry, or per-dataset settings.
            window.settings = QtCore.QSettings(
                str(Path(settings_dir) / "settings.ini"), QtCore.QSettings.Format.IniFormat
            )
            window.refresh_recents()
            outcome["window_created"] = True
            window.show()
            window.load_file(filename)
            dispatcher = window.dispatcher
            wait_for(lambda: window.description.get("kind") == "group" and not window.callbacks)
            outcome["contents"] = render(window.contents)
            for name, index in (("table", 1), ("plot", 2), ("image", 3)):
                candidate = candidates.get(name)
                if candidate is None:
                    outcome["views"][name] = {
                        "status": "skipped",
                        "reason": "No suitable dataset in bounded diagnostic sample",
                    }
                    continue
                shape = candidate["shape"]
                # Retain only the last one/two axes, with at most 16 samples each.
                dimensions = min(len(shape), 2 if name == "image" else 1)
                selection = [0] * (len(shape) - dimensions) + [
                    [0, min(size, 16), 1] for size in shape[len(shape) - dimensions :]
                ]
                window.navigate(candidate["path"], selection=selection)
                wait_for(lambda: 1 in window.loaded_views and not window.callbacks)
                if name != "table":
                    for combo, axis in (
                        (window.selection.row_axis, len(shape) - dimensions),
                        (window.selection.column_axis, len(shape) - 1 if dimensions == 2 else -1),
                    ):
                        combo.blockSignals(True)
                        combo.setCurrentIndex(combo.findData(axis))
                        combo.blockSignals(False)
                    window.tabs.setCurrentIndex(index)
                    wait_for(
                        lambda view_index=index: (
                            view_index in window.loaded_views and not window.callbacks
                        )
                    )
                if name == "table":
                    model = window.table.model()
                    if model is None or model.rowCount() < 1 or model.columnCount() < 1:
                        raise RuntimeError("Table contains no displayed values")
                    detail = {"rows": model.rowCount(), "columns": model.columnCount()}
                    widget = window.table
                elif name == "plot":
                    curves = window.plot.plot.listDataItems()
                    if not curves or not any(item.getData()[1].size for item in curves):
                        raise RuntimeError("Plot contains no rendered series")
                    detail = {"series": len(curves)}
                    widget = window.plot
                else:
                    pixels = window.image.item.image
                    if pixels is None or not pixels.size:
                        raise RuntimeError("Image contains no displayed pixels")
                    detail = {"shape": list(pixels.shape)}
                    widget = window.image
                outcome["views"][name] = {
                    "status": "passed",
                    "path": candidate["path"],
                    **detail,
                    **render(widget),
                }
            outcome["reader"] = dispatcher.client.status
        finally:
            if window is not None:
                # MainWindow.closeEvent performs the production shutdown first.
                window.close()
                if dispatcher is not None and dispatcher.isRunning():
                    dispatcher.shutdown()
                    if not dispatcher.wait(35000):
                        raise RuntimeError("GUI reader did not stop after cancellation")
                    window.close()
                outcome["closed"] = dispatcher is None or not dispatcher.isRunning()
                app.processEvents()
