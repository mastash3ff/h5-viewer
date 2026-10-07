"""Capture real Qt views with isolated preferences for local visual verification."""

from __future__ import annotations

import argparse
import multiprocessing
import time
from pathlib import Path


def main():
    from PySide6 import QtCore, QtWidgets

    from h5_viewer.window import MainWindow

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--width", type=int, default=1480)
    parser.add_argument("--height", type=int, default=920)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(
        QtCore.QSettings.Format.IniFormat,
        QtCore.QSettings.Scope.UserScope,
        str(output / "preferences"),
    )
    app = QtWidgets.QApplication([])
    window = MainWindow(args.file)
    window.resize(args.width, args.height)
    window.show()

    def wait():
        deadline = time.monotonic() + 45
        stable = 0
        while time.monotonic() < deadline:
            app.processEvents()
            if not window.callbacks:
                stable += 1
                if stable >= 10:
                    return
            else:
                stable = 0
            time.sleep(0.01)
        raise TimeoutError(f"View did not finish: {window.path}")

    try:
        wait()
        window.grab().save(str(output / "00-root.png"))
        for number, path in enumerate(args.path, 1):
            window.navigate(path)
            wait()
            window.grab().save(str(output / f"{number:02d}-table.png"))
            window.tabs.setCurrentIndex(4)
            wait()
            window.grab().save(str(output / f"{number:02d}-sources.png"))
            if window.description.get("kind") == "dataset":
                shape = window.description.get("shape") or []
                if len(shape) >= 2:
                    window.selection.row_axis.setCurrentIndex(len(shape) - 1)
                    window.selection.column_axis.setCurrentIndex(len(shape))
                    window.tabs.setCurrentIndex(3)
                    wait()
                    window.grab().save(str(output / f"{number:02d}-image.png"))
        print(f"Captured views in {output}")
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
