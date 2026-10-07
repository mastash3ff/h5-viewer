"""Qt presentation of bounded reader results; no HDF5 access lives here."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

MAX_IMAGE_PIXELS = 2048 * 2048


def display(value: Any) -> str:
    """Format integer and compound values without coercing through float."""
    if isinstance(value, bytes):
        return repr(value)
    if isinstance(value, np.ndarray):
        return np.array2string(value, threshold=12, edgeitems=3, max_line_width=160)
    if isinstance(value, np.void) and value.dtype.names:
        return "{" + ", ".join(f"{n}: {display(value[n])}" for n in value.dtype.names) + "}"
    if isinstance(value, (np.integer, int)):
        return str(int(value))
    if isinstance(value, (np.floating, float)):
        return repr(float(value))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {display(v)}" for k, v in value.items())
    return str(value)


def display_typed(value, dtype):
    """Use declared presentation metadata without changing the underlying value."""
    metadata = dtype.metadata or {}
    if metadata.get("enum") and np.ndim(value) == 0:
        code = int(value)
        names = [str(name) for name, member in metadata["enum"].items() if member == code]
        return f"{' / '.join(names)} ({code})" if names else f"{code} (unmapped enum)"
    if dtype.kind == "V" and not dtype.names:
        return "0x" + np.asarray(value, dtype=dtype).tobytes().hex()
    if metadata.get("hdf5_class") == "bitfield":
        return f"0x{int(value):0{dtype.itemsize * 2}x}"
    if dtype.kind == "S" and isinstance(value, (bytes, np.bytes_)):
        raw = bytes(value)
        try:
            return raw.decode(metadata.get("h5py_encoding", "ascii"))
        except (UnicodeDecodeError, LookupError):
            return "0x" + raw.hex()
    return display(value)


def leaves(dtype, prefix=""):
    if dtype.names:
        for name in dtype.names:
            child = f"{prefix}.{name}" if prefix else name
            yield from leaves(dtype[name], child)
    else:
        yield prefix


def field_value(value, field):
    for part in field.split(".") if field else []:
        value = value[part]
    return value


class ArrayModel(QtCore.QAbstractTableModel):
    """Retain exact NumPy values and source indices for every displayed cell."""

    def __init__(self, array, selection=(), field="", row_axis=None, parent=None):
        super().__init__(parent)
        self.array = np.asarray(array)
        self.selection = selection
        self.field = field
        self.fields = list(leaves(self.array.dtype)) if self.array.dtype.names else []
        self.retained_axes = [i for i, s in enumerate(selection) if isinstance(s, (list, tuple))]
        self.row_dim = self.retained_axes.index(row_axis) if row_axis in self.retained_axes else 0
        if self.row_dim >= self.array.ndim:
            self.row_dim = 0
        self.other_dims = [i for i in range(self.array.ndim) if i != self.row_dim]
        self.rows = self.array.shape[self.row_dim] if self.array.ndim else 1
        self.other_shape = tuple(self.array.shape[i] for i in self.other_dims)
        self.base_columns = math.prod(self.other_shape) if self.other_shape else 1

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else self.rows

    def columnCount(self, parent=None):
        return (
            0
            if parent is not None and parent.isValid()
            else self.base_columns * max(1, len(self.fields))
        )

    def local_index(self, row, column):
        if not self.array.ndim:
            return ()
        base = column // max(1, len(self.fields))
        others = np.unravel_index(base, self.other_shape) if self.other_shape else ()
        idx = [0] * self.array.ndim
        idx[self.row_dim] = row
        for dim, pos in zip(self.other_dims, others):
            idx[dim] = int(pos)
        return tuple(idx)

    def cell_field(self, column):
        nested = self.fields[column % len(self.fields)] if self.fields else ""
        return ".".join(p for p in (self.field, nested) if p)

    def source_index(self, row, column):
        local = iter(self.local_index(row, column))
        result = []
        for item in self.selection:
            if isinstance(item, (list, tuple)):
                result.append(int(item[0]) + next(local, 0) * int(item[2]))
            else:
                result.append(int(item))
        return result

    def value(self, row, column):
        value = self.array[self.local_index(row, column)]
        if self.fields:
            value = field_value(value, self.fields[column % len(self.fields)])
        return value

    def column_dtype(self, column):
        dtype = self.array.dtype
        if self.fields:
            for name in self.fields[column % len(self.fields)].split("."):
                dtype = dtype[name]
        return dtype

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
            dtype = self.column_dtype(index.column())
            value = self.value(index.row(), index.column())
            result = display_typed(value, dtype)
            if role == QtCore.Qt.ItemDataRole.ToolTipRole:
                return f"{result}\nDeclared type: {dtype}\nMetadata: {dtype.metadata or {}}\nRaw: {display(value)}"
            return result[:2048]
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            return int(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Vertical:
            if self.retained_axes and self.row_dim < len(self.retained_axes):
                s = self.selection[self.retained_axes[self.row_dim]]
                return str(s[0] + section * s[2])
            return str(section)
        if self.fields:
            name = self.fields[section % len(self.fields)]
            return name if self.base_columns == 1 else f"{section // len(self.fields)}: {name}"
        if len(self.other_dims) == 1 and self.other_dims[0] < len(self.retained_axes):
            s = self.selection[self.retained_axes[self.other_dims[0]]]
            return str(s[0] + section * s[2])
        return "Value" if self.base_columns == 1 else str(section)


class SelectionEditor(QtWidgets.QWidget):
    applied = QtCore.Signal()
    image_requested = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.shape = []
        self.edits = []
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.row_axis = QtWidgets.QComboBox()
        self.column_axis = QtWidgets.QComboBox()
        self.page_size = QtWidgets.QSpinBox()
        self.page_size.setRange(1, 100000)
        self.page_size.setValue(200)
        self.jump = QtWidgets.QLineEdit("0")
        self.jump.setMaximumWidth(90)
        self.jump.setPlaceholderText("Row index")
        for text, widget in [
            ("Rows / Y", self.row_axis),
            ("Columns / X", self.column_axis),
            ("Page", self.page_size),
            ("Jump", self.jump),
        ]:
            bar.addWidget(QtWidgets.QLabel(text))
            bar.addWidget(widget)
        self.previous = QtWidgets.QPushButton("Previous")
        self.next = QtWidgets.QPushButton("Next")
        self.apply = QtWidgets.QPushButton("Read selection")
        self.image_preset = QtWidgets.QPushButton("Image plane: last 2 axes")
        self.image_preset.setToolTip(
            "Suggested display axes only; this does not assign sample, channel or physical meaning."
        )
        bar.addStretch()
        outer.addLayout(bar)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(90)
        scroll.setMaximumHeight(95)
        panel = QtWidgets.QWidget()
        self.dim_layout = QtWidgets.QHBoxLayout(panel)
        scroll.setWidget(panel)
        outer.addWidget(scroll)
        self.help = QtWidgets.QLabel(
            "Each dimension: index or start:stop:step. Axes choose table orientation and image Y/X; other dimensions may be fixed to any channel or frame."
        )
        self.help.setWordWrap(True)
        outer.addWidget(self.help)
        actions = QtWidgets.QHBoxLayout()
        for widget in (self.image_preset, self.previous, self.next, self.apply):
            actions.addWidget(widget)
        actions.addStretch()
        outer.addLayout(actions)
        self.image_preset.clicked.connect(self.use_image_plane)
        self.apply.clicked.connect(self.applied)
        self.previous.clicked.connect(lambda: self.page(-1))
        self.next.clicked.connect(lambda: self.page(1))
        self.jump.returnPressed.connect(self.jump_to)
        self.row_axis.currentIndexChanged.connect(self.axes_changed)
        self.column_axis.currentIndexChanged.connect(self.axes_changed)

    def configure(self, shape):
        self.shape = list(shape or [])
        while self.dim_layout.count():
            item = self.dim_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.edits = []
        for combo in (self.row_axis, self.column_axis):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("None", -1)
            for i in range(len(self.shape)):
                combo.addItem(str(i), i)
        self.row_axis.setCurrentIndex(1 if self.shape else 0)
        self.column_axis.setCurrentIndex(len(self.shape) if len(self.shape) > 1 else 0)
        for combo in (self.row_axis, self.column_axis):
            combo.blockSignals(False)
        for i, size in enumerate(self.shape):
            box = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 4, 0)
            layout.addWidget(QtWidgets.QLabel(f"Dimension {i} ({size:,})"))
            edit = QtWidgets.QLineEdit()
            edit.setMinimumWidth(120)
            edit.setMaximumWidth(210)
            edit.returnPressed.connect(self.applied)
            self.edits.append(edit)
            input_bar = QtWidgets.QHBoxLayout()
            back = QtWidgets.QToolButton()
            back.setText("-")
            back.setToolTip("Previous fixed index (frame / channel when applicable)")
            forward = QtWidgets.QToolButton()
            forward.setText("+")
            forward.setToolTip("Next fixed index (frame / channel when applicable)")
            back.clicked.connect(lambda _checked=False, axis=i: self.step_index(axis, -1))
            forward.clicked.connect(lambda _checked=False, axis=i: self.step_index(axis, 1))
            input_bar.addWidget(back)
            input_bar.addWidget(edit)
            input_bar.addWidget(forward)
            layout.addLayout(input_bar)
            self.dim_layout.addWidget(box)
        self.dim_layout.addStretch()
        self.image_preset.setEnabled(len(self.shape) >= 2)
        self.axes_changed()

    def use_image_plane(self):
        if len(self.shape) < 2:
            return
        previous = [e.text() for e in self.edits]
        self.row_axis.setCurrentIndex(self.row_axis.findData(len(self.shape) - 2))
        self.column_axis.setCurrentIndex(self.column_axis.findData(len(self.shape) - 1))
        for axis, edit in enumerate(self.edits):
            if axis >= len(self.shape) - 2:
                edit.setText(f"0:{self.shape[axis]}:1")
            else:
                edit.setText(previous[axis].split(":")[0] or "0")
        self.image_requested.emit()

    def step_index(self, axis, direction):
        edit = self.edits[axis]
        try:
            if ":" in edit.text():
                self.help.setText(
                    "Index steppers apply to fixed dimensions. Enter a single index for the frame or channel dimension first."
                )
                return
            edit.setText(str(min(max(0, int(edit.text()) + direction), self.shape[axis] - 1)))
            self.applied.emit()
        except ValueError:
            self.applied.emit()

    def axes_changed(self):
        row, col = self.row_axis.currentData(), self.column_axis.currentData()
        for i, (edit, size) in enumerate(zip(self.edits, self.shape)):
            if i == row:
                if ":" not in edit.text():
                    edit.setText(f"0:{min(size, self.page_size.value())}:1")
            elif i == col:
                if ":" not in edit.text():
                    edit.setText(f"0:{min(size, 200)}:1")
            elif not edit.text() or ":" in edit.text():
                edit.setText("0" if size else "0:0:1")

    def selection(self):
        result = []
        for edit, size in zip(self.edits, self.shape):
            value = edit.text().strip()
            if ":" in value:
                parts = value.split(":")
                if len(parts) not in (2, 3):
                    raise ValueError("Use start:stop:step for each sliced dimension.")
                parts += [""] * (3 - len(parts))
                start, stop, step = [
                    int(p) if p else default for p, default in zip(parts, [0, size, 1])
                ]
                if step <= 0 or not 0 <= start <= stop <= size:
                    raise ValueError(
                        f"Slice must satisfy 0 <= start <= stop <= {size} and step > 0."
                    )
                result.append([start, stop, step])
            else:
                index = int(value)
                if not 0 <= index < size:
                    raise ValueError(f"Index must be between 0 and {size - 1}.")
                result.append(index)
        return result

    def set_selection(self, selection):
        for edit, item in zip(self.edits, selection):
            edit.setText(":".join(map(str, item)) if isinstance(item, (list, tuple)) else str(item))

    def page(self, direction):
        axis = self.row_axis.currentData()
        if axis is None or axis < 0:
            return
        try:
            selection = self.selection()
            old = selection[axis]
            start, step = (old[0], old[2]) if isinstance(old, list) else (old, 1)
            span = self.page_size.value() * step
            start = min(max(0, start + direction * span), max(0, self.shape[axis] - 1))
            selection[axis] = [start, min(self.shape[axis], start + span), step]
            self.set_selection(selection)
            self.applied.emit()
        except ValueError:
            self.applied.emit()

    def jump_to(self):
        try:
            axis = self.row_axis.currentData()
            if axis is None or axis < 0:
                return
            selection = self.selection()
            start = int(self.jump.text())
            selection[axis] = [start, min(self.shape[axis], start + self.page_size.value()), 1]
            self.set_selection(selection)
            self.applied.emit()
        except ValueError:
            self.applied.emit()


class ImagePanel(QtWidgets.QWidget):
    native_requested = QtCore.Signal(list)
    probe_requested = QtCore.Signal(list)

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.component = QtWidgets.QComboBox()
        self.component.addItems(["Magnitude", "Real", "Imaginary", "Phase"])
        self.colormap = QtWidgets.QComboBox()
        self.colormap.addItems(["viridis", "grey", "plasma", "magma", "CET-L9"])
        self.low = QtWidgets.QLineEdit()
        self.high = QtWidgets.QLineEdit()
        self.low.setMaximumWidth(100)
        self.high.setMaximumWidth(100)
        auto = QtWidgets.QPushButton("Auto contrast")
        apply = QtWidgets.QPushButton("Set contrast")
        fit = QtWidgets.QPushButton("Fit")
        native = QtWidgets.QPushButton("Read visible at native pixels")
        for label, widget in [
            ("Complex", self.component),
            ("Color", self.colormap),
            ("Min", self.low),
            ("Max", self.high),
        ]:
            bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(widget)
        bar.addStretch()
        layout.addLayout(bar)
        actions = QtWidgets.QHBoxLayout()
        for w in (auto, apply, fit, native):
            actions.addWidget(w)
        actions.addStretch()
        layout.addLayout(actions)
        self.plot = pg.PlotWidget()
        self.plot.invertY(True)
        self.plot.setAspectLocked(True)
        self.item = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.item)
        layout.addWidget(self.plot)
        self.note = QtWidgets.QLabel(
            "Choose two sliced dimensions for an image. Drag to pan; use the wheel to zoom."
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.hover_note = QtWidgets.QLabel(
            "Hover to inspect a displayed pixel; click to read an exact source pixel."
        )
        self.hover_note.setWordWrap(True)
        self.probe_note = QtWidgets.QLabel()
        self.probe_note.setWordWrap(True)
        self.probe_note.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.hover_note)
        layout.addWidget(self.probe_note)
        self.plot.scene().sigMouseMoved.connect(self.mouse_moved)
        self.plot.scene().sigMouseClicked.connect(self.mouse_clicked)
        self.array = None
        self.selection = []
        self.axes = []
        self.component.currentIndexChanged.connect(self.refresh)
        self.colormap.currentTextChanged.connect(self.refresh)
        auto.clicked.connect(lambda: self.refresh(True))
        apply.clicked.connect(self.manual_levels)
        fit.clicked.connect(self.plot.autoRange)
        native.clicked.connect(self.native_region)

    def set_result(self, reply, row_axis, column_axis):
        self.clear()
        array = np.asarray(reply["array"])
        selection = reply.get("selection", [])
        retained = [i for i, s in enumerate(selection) if isinstance(s, (list, tuple))]
        if array.dtype.kind not in "buifc" or array.ndim != 2 or len(retained) != 2:
            raise ValueError(
                "Image view needs a numeric field and exactly two sliced dimensions; fix every other dimension to an index."
            )
        if array.size > MAX_IMAGE_PIXELS:
            raise ValueError(
                f"Image display is limited to {MAX_IMAGE_PIXELS:,} pixels. Choose a smaller native region or increase the selection strides for an overview."
            )
        if row_axis == column_axis or row_axis not in retained or column_axis not in retained:
            raise ValueError(
                "Choose distinct Rows / Y and Columns / X axes matching the two sliced dimensions."
            )
        self.array = np.moveaxis(
            array, [retained.index(row_axis), retained.index(column_axis)], [0, 1]
        )
        self.selection = selection
        self.axes = [row_axis, column_axis]
        self.probe_note.clear()
        self.hover_note.setText(
            "Hover to inspect a displayed pixel; click to read an exact source pixel."
        )
        self.refresh(True)
        y, x = (selection[a] for a in self.axes)
        self.item.setRect(
            QtCore.QRectF(x[0], y[0], self.array.shape[1] * x[2], self.array.shape[0] * y[2])
        )
        self.plot.setLabel("left", f"Dimension {row_axis}")
        self.plot.setLabel("bottom", f"Dimension {column_axis}")
        self.plot.autoRange()
        self.note.setText(
            f"Source selection: {selection}   |   Preview stride Y={y[2]}, X={x[2]}   |   {self.array.shape[1]:,} x {self.array.shape[0]:,} displayed pixels. Drag to pan; wheel to zoom."
        )

    def clear(self):
        self.array = None
        self.selection = []
        self.axes = []
        self.item.clear()
        self.note.setText("Choose a numeric field and two sliced dimensions for an image.")
        self.hover_note.clear()
        self.probe_note.clear()

    def refresh(self, auto=False):
        if self.array is None or self.array.size == 0:
            return
        arr = self.array
        if np.iscomplexobj(arr):
            arr = {"Magnitude": np.abs, "Real": np.real, "Imaginary": np.imag, "Phase": np.angle}[
                self.component.currentText()
            ](arr)
        arr = np.asarray(arr, dtype=float)
        finite = arr[np.isfinite(arr)]
        if not len(finite):
            self.note.setText("This selection contains no finite image values.")
            self.item.clear()
            return
        lo, hi = (
            np.percentile(finite, [1, 99])
            if auto
            else (float(np.min(finite)), float(np.max(finite)))
        )
        if lo == hi:
            hi = lo + 1
        self.low.setText(f"{lo:.8g}")
        self.high.setText(f"{hi:.8g}")
        self.item.setImage(arr, autoLevels=False, levels=(lo, hi))
        try:
            cmap = pg.colormap.get(self.colormap.currentText())
            self.item.setLookupTable(cmap.getLookupTable(nPts=256))
        except (FileNotFoundError, KeyError):
            self.item.setLookupTable(None)

    def manual_levels(self):
        try:
            lo, hi = float(self.low.text()), float(self.high.text())
            if not np.isfinite([lo, hi]).all() or lo >= hi:
                raise ValueError
            self.item.setLevels((lo, hi))
        except ValueError:
            self.note.setText(
                "Contrast requires finite minimum and maximum values, with minimum < maximum."
            )

    def mouse_moved(self, position):
        if self.plot.sceneBoundingRect().contains(position):
            point = self.plot.plotItem.vb.mapSceneToView(position)
            self.probe_at(point.x(), point.y())

    def mouse_clicked(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            point = self.plot.plotItem.vb.mapSceneToView(event.scenePos())
            if self.probe_at(point.x(), point.y(), click=True):
                event.accept()

    def probe_at(self, x, y, *, click=False):
        """Map scene coordinates to source indices, preserving sampled-pixel identity."""
        if self.array is None or not self.array.size or len(self.axes) != 2:
            return False
        coordinates = list(self.selection)
        sampled_coordinates = list(self.selection)
        local = []
        strided = False
        for axis, position in zip(self.axes, [y, x]):
            start, stop, step = self.selection[axis]
            source_index = math.floor(position)
            if source_index < start or source_index >= stop:
                self.hover_note.setText("Pointer is outside the loaded source selection.")
                return False
            pixel = (source_index - start) // step
            local.append(pixel)
            coordinates[axis] = source_index
            sampled_coordinates[axis] = start + pixel * step
            strided = strided or step > 1
        value = self.array[tuple(local)]
        if strided:
            self.hover_note.setText(
                f"Pointer at source {coordinates}. Preview sample at {sampled_coordinates}: raw {display(value)}. Click to read the exact pointer coordinate."
            )
        else:
            self.hover_note.setText(
                f"Source {coordinates}: raw {display(value)} ({self.array.dtype})."
            )
        if click:
            self.probe_note.setText(f"Reading exact source pixel {coordinates}...")
            self.probe_requested.emit(coordinates)
        return True

    def show_probe(self, reply):
        value = np.asarray(reply["array"])
        raw = value.item() if value.size == 1 else value
        self.probe_note.setText(
            f"Exact source {reply.get('selection', [])}: raw {display(raw)} ({value.dtype})."
        )

    def native_region(self):
        if len(self.axes) != 2:
            return
        x_range, y_range = self.plot.viewRange()
        selection = [s[:] if isinstance(s, list) else s for s in self.selection]
        for axis, bounds in zip(self.axes, [y_range, x_range]):
            old = selection[axis]
            start = max(old[0], math.floor(bounds[0]))
            stop = min(old[1], math.ceil(bounds[1]))
            if stop <= start:
                self.note.setText("Pan to overlap the loaded image before reading a native region.")
                return
            selection[axis] = [start, stop, 1]
        self.native_requested.emit(selection)


class PlotPanel(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        bar = QtWidgets.QHBoxLayout()
        self.x = QtWidgets.QComboBox()
        self.y = QtWidgets.QListWidget()
        self.y.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.y.setMaximumHeight(82)
        self.component = QtWidgets.QComboBox()
        self.component.addItems(["Real", "Imaginary", "Magnitude", "Phase"])
        self.histogram = QtWidgets.QCheckBox("Histogram")
        redraw = QtWidgets.QPushButton("Plot selected columns")
        bar.addWidget(QtWidgets.QLabel("X"))
        bar.addWidget(self.x)
        bar.addWidget(QtWidgets.QLabel("Y (Ctrl-click for multiple)"))
        bar.addWidget(self.y, 1)
        bar.addWidget(self.component)
        bar.addWidget(self.histogram)
        bar.addWidget(redraw)
        layout.addLayout(bar)
        self.plot = pg.PlotWidget()
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        layout.addWidget(self.plot)
        self.stats = QtWidgets.QPlainTextEdit()
        self.stats.setReadOnly(True)
        self.stats.setMaximumHeight(115)
        layout.addWidget(self.stats)
        self.columns = {}
        self.indices = np.empty(0)
        self.sampled = False
        redraw.clicked.connect(self.redraw)
        self.histogram.toggled.connect(self.redraw)

    def set_result(self, reply, row_axis):
        self.clear()
        model = ArrayModel(reply["array"], reply.get("selection", []), row_axis=row_axis)
        self.columns = {}
        # Curves and statistics remain bounded even when the table has many columns.
        column_count = min(model.columnCount(), 256)
        row_stride = max(1, math.ceil(model.rowCount() / max(1, 100000 // max(1, column_count))))
        rows = range(0, model.rowCount(), row_stride)
        for col in range(column_count):
            values = [model.value(row, col) for row in rows]
            arr = np.asarray(values)
            if arr.ndim == 1 and arr.dtype.kind in "buifc":
                name = str(model.headerData(col, QtCore.Qt.Orientation.Horizontal))
                self.columns[name] = arr
        if not self.columns:
            raise ValueError(
                "Choose a numeric dataset or field to plot. Array-valued cells can be expanded from the table."
            )
        self.x.clear()
        self.x.addItem("Source row index")
        self.x.addItems(list(self.columns))
        self.y.clear()
        self.y.addItems(list(self.columns))
        self.y.item(0).setSelected(True)
        self.indices = np.array(
            [int(model.headerData(row, QtCore.Qt.Orientation.Vertical)) for row in rows]
        )
        self.sampled = reply.get("sampled", False) or row_stride > 1
        self.column_limit = model.columnCount() > 256
        self.redraw()

    def clear(self):
        self.columns = {}
        self.indices = np.empty(0)
        self.plot.clear()
        self.x.clear()
        self.y.clear()
        self.stats.clear()

    def real_values(self, arr):
        if np.iscomplexobj(arr):
            return {"Real": np.real, "Imaginary": np.imag, "Magnitude": np.abs, "Phase": np.angle}[
                self.component.currentText()
            ](arr)
        return arr

    def redraw(self):
        if not self.columns:
            return
        self.plot.clear()
        lines = [
            "Statistics cover only the returned selection"
            + (" (sampled overview)." if self.sampled else ".")
            + " Plot coordinates use floating point; exact values remain in Table."
        ]
        if self.column_limit:
            lines.append(
                "Column chooser covers the first 256 columns; narrow the shared selection to inspect later columns."
            )
        names = [item.text() for item in self.y.selectedItems()][:16]
        x = (
            self.indices
            if self.x.currentIndex() == 0
            else self.real_values(self.columns[self.x.currentText()])
        )
        for i, name in enumerate(names):
            raw = self.real_values(self.columns[name])
            values = np.asarray(raw, dtype=float)
            finite = np.isfinite(values)
            finite_values = values[finite]
            if not len(finite_values):
                lines.append(f"{name}: no finite values")
                continue
            exact = raw[finite]
            lines.append(
                f"{name}: n={len(values):,}, finite={len(finite_values):,}, min={display(exact.min())}, max={display(exact.max())}, mean={finite_values.mean():.8g}, std={finite_values.std():.8g}"
            )
            pen = pg.mkPen(pg.intColor(i, hues=max(3, len(names))), width=2)
            if self.histogram.isChecked():
                counts, edges = np.histogram(
                    finite_values, bins=min(64, max(1, int(np.sqrt(len(finite_values)))))
                )
                self.plot.plot(edges, counts, stepMode="center", pen=pen, name=name)
            else:
                self.plot.plot(np.asarray(x, dtype=float), values, pen=pen, name=name)
        self.stats.setPlainText("\n".join(lines))
