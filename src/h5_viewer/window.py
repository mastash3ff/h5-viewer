"""General purpose desktop HDF5 explorer with isolated asynchronous reads."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import traceback
from pathlib import Path
from typing import ClassVar

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from .process import ReaderClient
from .ui_data import ArrayModel, ImagePanel, PlotPanel, SelectionEditor, display


class Dispatcher(QtCore.QThread):
    result = QtCore.Signal(int, object, object)

    def __init__(self, filename, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.jobs = queue.Queue()
        self.client = None
        self.stopping = False
        self.epoch = 0
        self.cancel_done = threading.Event()
        self.cancel_done.set()
        self.start()

    def submit(self, identifier, request):
        self.jobs.put((self.epoch, identifier, request))

    def cancel(self):
        self.epoch += 1
        self.cancel_done.clear()

        def terminate():
            try:
                if self.client is not None:
                    self.client.cancel()
            finally:
                self.cancel_done.set()

        threading.Thread(target=terminate, daemon=True).start()

    def shutdown(self):
        self.stopping = True
        self.cancel()
        self.jobs.put(None)

    def run(self):
        try:
            while not self.stopping:
                job = self.jobs.get()
                if job is None:
                    break
                epoch, identifier, request = job
                self.cancel_done.wait()
                if epoch != self.epoch or self.stopping:
                    continue
                try:
                    if self.client is None:
                        self.client = ReaderClient(self.filename)
                    if epoch != self.epoch or self.stopping:
                        self.client.cancel()
                        continue
                    reply = self.client.request(request)
                    self.result.emit(identifier, reply, None)
                except Exception as exc:  # noqa: BLE001 - isolate all reader failures from Qt
                    self.result.emit(identifier, None, str(exc))
        finally:
            if self.client is not None:
                self.client.close()


class MainWindow(QtWidgets.QMainWindow):
    """One read-only file per window. All reader calls execute in Dispatcher."""

    windows: ClassVar[list] = []

    def __init__(self, filename=None):
        super().__init__()
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("H5 Viewer")
        self.resize(1480, 920)
        self.settings = QtCore.QSettings("H5Viewer", "H5Viewer")
        self.filename = None
        self.dispatcher = None
        self.path = "/"
        self.description = {}
        self.generation = 0
        self.callbacks = {}
        self.request_serial = 0
        self.history = []
        self.history_index = -1
        self.attribute_offset = 0
        self.array_model = None
        self.last_reply = None
        self.loaded_views = set()
        self.read_revision = 0
        self.probe_revision = 0
        self.search_revision = 0
        self._restoring = False
        self._build()
        self.setAcceptDrops(True)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if filename:
            self.load_file(filename)
        else:
            self.statusBar().showMessage("Open an HDF5 file to browse its contents.")

    def _build(self):
        file_menu = self.menuBar().addMenu("&File")
        open_action = file_menu.addAction("&Open file...")
        open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.choose_file)
        folder_action = file_menu.addAction("Browse &folder...")
        folder_action.triggered.connect(self.choose_folder)
        self.recent_menu = file_menu.addMenu("Recent files")
        self.refresh_recents()
        file_menu.addSeparator()
        close = file_menu.addAction("&Close window")
        close.setShortcut(QtGui.QKeySequence.StandardKey.Close)
        close.triggered.connect(self.close)
        view_menu = self.menuBar().addMenu("&View")
        refresh = view_menu.addAction("Refresh current object")
        refresh.setShortcut("F5")
        refresh.triggered.connect(lambda: self.navigate(self.path, history=False))
        toolbar = self.addToolBar("Navigation")
        toolbar.setMovable(False)
        self.back = toolbar.addAction("Back")
        self.back.setShortcut("Alt+Left")
        self.back.triggered.connect(lambda: self.history_move(-1))
        self.forward = toolbar.addAction("Forward")
        self.forward.setShortcut("Alt+Right")
        self.forward.triggered.connect(lambda: self.history_move(1))
        root = toolbar.addAction("Root")
        root.triggered.connect(lambda: self.navigate("/"))
        self.path_edit = QtWidgets.QLineEdit("/")
        self.path_edit.setPlaceholderText("HDF5 object path")
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text()))
        toolbar.addWidget(self.path_edit)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search object, field or attribute names")
        self.search.setMinimumWidth(260)
        self.search.returnPressed.connect(self.search_file)
        toolbar.addWidget(self.search)
        self.cancel_action = toolbar.addAction("Cancel read")
        self.cancel_action.triggered.connect(self.cancel_read)

        splitter = QtWidgets.QSplitter()
        self.setCentralWidget(splitter)
        sidebar = QtWidgets.QWidget()
        sidebar.setMinimumWidth(220)
        side = QtWidgets.QVBoxLayout(sidebar)
        side.addWidget(QtWidgets.QLabel("LOCAL FILES"))
        self.files = QtWidgets.QListWidget()
        self.files.setMaximumHeight(115)
        self.files.itemDoubleClicked.connect(
            lambda item: self.open_file(item.data(QtCore.Qt.ItemDataRole.UserRole))
        )
        side.addWidget(self.files)
        side.addWidget(QtWidgets.QLabel("FILE CONTENTS"))
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Object", "Type / shape"])
        self.tree.setColumnWidth(0, 185)
        self.tree.itemExpanded.connect(self.expand_tree)
        self.tree.itemActivated.connect(lambda item, _col: self.activate_tree(item))
        self.tree.itemClicked.connect(lambda item, _col: self.activate_tree(item))
        side.addWidget(self.tree, 1)
        self.search_label = QtWidgets.QLabel("SEARCH RESULTS")
        side.addWidget(self.search_label)
        self.search_results = QtWidgets.QTreeWidget()
        self.search_results.setHeaderLabels(["Name", "Object path", "Kind"])
        self.search_results.setMaximumHeight(220)
        self.search_results.itemActivated.connect(self.activate_search)
        side.addWidget(self.search_results)
        splitter.addWidget(sidebar)

        center = QtWidgets.QWidget()
        middle = QtWidgets.QVBoxLayout(center)
        self.title = QtWidgets.QLabel("Open a file")
        font = self.title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self.title.setFont(font)
        self.summary = QtWidgets.QLabel("Browse data, metadata, fields and source relationships.")
        self.summary.setWordWrap(True)
        middle.addWidget(self.title)
        middle.addWidget(self.summary)
        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error.setStyleSheet("QLabel { color: #a42b24; padding: 6px; background: #fff0ec; }")
        self.error.hide()
        middle.addWidget(self.error)
        self.selection = SelectionEditor()
        self.selection.applied.connect(self.read_selection)
        self.selection.image_requested.connect(self.use_image_plane)
        self.selection.hide()
        middle.addWidget(self.selection)
        self.tabs = QtWidgets.QTabWidget()
        self.contents = QtWidgets.QTreeWidget()
        self.contents.setHeaderLabels(["Name", "Kind / link", "Shape", "Type / target"])
        self.contents.setColumnWidth(0, 200)
        self.contents.itemActivated.connect(
            lambda item, _col: self.activate_item(item.data(QtCore.Qt.ItemDataRole.UserRole))
        )
        self.tabs.addTab(self.contents, "Contents")
        table_panel = QtWidgets.QWidget()
        table_layout = QtWidgets.QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setDefaultSectionSize(150)
        self.table.verticalHeader().setDefaultSectionSize(25)
        self.table.doubleClicked.connect(self.inspect_cell)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        table_layout.addWidget(self.table)
        self.table_note = QtWidgets.QLabel(
            "Double-click a cell to inspect array or ragged values. Right-click to copy or follow a reference."
        )
        self.table_note.setWordWrap(True)
        table_layout.addWidget(self.table_note)
        self.tabs.addTab(table_panel, "Table / Values")
        self.plot = PlotPanel()
        self.tabs.addTab(self.plot, "Plot / Statistics")
        self.image = ImagePanel()
        self.image.native_requested.connect(self.read_native_region)
        self.image.probe_requested.connect(self.read_pixel)
        self.tabs.addTab(self.image, "Image")
        self.sources = QtWidgets.QTreeWidget()
        self.sources.setHeaderLabels(
            ["Relationship", "Recorded source", "Resolved source / target", "Status"]
        )
        self.sources.setColumnWidth(0, 150)
        self.sources.itemActivated.connect(self.activate_source)
        self.tabs.addTab(self.sources, "Sources / Links")
        self.tabs.currentChanged.connect(self.tab_changed)
        middle.addWidget(self.tabs, 1)
        splitter.addWidget(center)

        inspector = QtWidgets.QWidget()
        inspector.setMinimumWidth(270)
        right = QtWidgets.QVBoxLayout(inspector)
        inspector_tabs = QtWidgets.QTabWidget()
        self.inspector_tabs = inspector_tabs
        self.properties = QtWidgets.QTreeWidget()
        self.properties.setHeaderLabels(["Property", "Value"])
        self.properties.setColumnWidth(0, 135)
        inspector_tabs.addTab(self.properties, "Properties")
        attr_panel = QtWidgets.QWidget()
        attr_layout = QtWidgets.QVBoxLayout(attr_panel)
        self.attributes = QtWidgets.QTreeWidget()
        self.attributes.setHeaderLabels(["Attribute", "Value", "Type / shape"])
        self.attributes.setColumnWidth(0, 120)
        self.attributes.itemDoubleClicked.connect(self.inspect_attribute)
        attr_layout.addWidget(self.attributes)
        attr_bar = QtWidgets.QHBoxLayout()
        self.attr_previous = QtWidgets.QPushButton("Previous")
        self.attr_next = QtWidgets.QPushButton("Next")
        self.attr_label = QtWidgets.QLabel()
        self.attr_previous.clicked.connect(lambda: self.page_attributes(-1))
        self.attr_next.clicked.connect(lambda: self.page_attributes(1))
        for widget in (self.attr_previous, self.attr_label, self.attr_next):
            attr_bar.addWidget(widget)
        attr_layout.addLayout(attr_bar)
        inspector_tabs.addTab(attr_panel, "Attributes")
        right.addWidget(inspector_tabs, 2)
        right.addWidget(QtWidgets.QLabel("FIELDS"))
        self.field_filter = QtWidgets.QLineEdit()
        self.field_filter.setPlaceholderText("Filter nested fields")
        self.field_filter.textChanged.connect(self.filter_fields)
        right.addWidget(self.field_filter)
        self.fields = QtWidgets.QListWidget()
        self.fields.currentItemChanged.connect(self.field_changed)
        right.addWidget(self.fields, 1)
        self.field_info = QtWidgets.QLabel(
            "Select All fields for a structured table, or one field for numeric views."
        )
        self.field_info.setWordWrap(True)
        right.addWidget(self.field_info)
        self.companion_enabled = QtWidgets.QCheckBox("Show companion row (opt in)")
        self.companion_axis = QtWidgets.QComboBox()
        self.companion_axis.setToolTip("Match sibling leading length to this chosen sample axis")
        self.companion_choice = QtWidgets.QComboBox()
        self.companion_choice.setToolTip(
            "Sibling datasets with matching leading length; length alone does not establish a semantic relationship"
        )
        self.companion_note = QtWidgets.QLabel(
            "Choose the sample axis and a companion dataset explicitly."
        )
        self.companion_note.setWordWrap(True)
        self.companion_table = QtWidgets.QTableView()
        self.companion_table.setMaximumHeight(165)
        self.companion_table.setAlternatingRowColors(True)
        right.addWidget(self.companion_enabled)
        companion_bar = QtWidgets.QHBoxLayout()
        self.companion_axis_label = QtWidgets.QLabel("Sample axis")
        companion_bar.addWidget(self.companion_axis_label)
        companion_bar.addWidget(self.companion_axis)
        right.addLayout(companion_bar)
        right.addWidget(self.companion_choice)
        right.addWidget(self.companion_note)
        right.addWidget(self.companion_table)
        self.companion_enabled.toggled.connect(self.refresh_companions)
        self.companion_axis.currentIndexChanged.connect(self.refresh_companions)
        self.companion_choice.currentIndexChanged.connect(self.read_companion)
        for widget in (
            self.companion_axis_label,
            self.companion_axis,
            self.companion_choice,
            self.companion_note,
            self.companion_table,
        ):
            widget.setVisible(False)
        splitter.addWidget(inspector)
        splitter.setSizes([270, 890, 320])
        splitter.setStretchFactor(1, 1)
        self.back.setEnabled(False)
        self.forward.setEnabled(False)
        self.statusBar().setSizeGripEnabled(True)
        copy_action = QtGui.QAction(self.table)
        copy_action.setShortcut(QtGui.QKeySequence.StandardKey.Copy)
        copy_action.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        copy_action.triggered.connect(self.copy_cells)
        self.table.addAction(copy_action)
        for widget in (self.tree, self.contents, self.sources):
            widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, w=widget: self.object_menu(w, pos)
            )

    def request(self, request, callback, *, navigation=True):
        if self.dispatcher is None:
            return
        self.request_serial += 1
        identifier = self.request_serial
        self.callbacks[identifier] = (self.generation if navigation else None, callback)
        self.dispatcher.submit(identifier, request)
        self.statusBar().showMessage(f"Reading {request['op']} — {request.get('path', self.path)}")

    @QtCore.Slot(int, object, object)
    def receive(self, identifier, reply, error):
        entry = self.callbacks.pop(identifier, None)
        if entry is None:
            return
        generation, callback = entry
        if generation is not None and generation != self.generation:
            return
        if error:
            self.show_error(error)
            return
        if reply.get("error"):
            self.show_error(str(reply["error"]))
            return
        try:
            callback(reply)
            if not self.callbacks:
                self.statusBar().showMessage(f"Ready — {self.filename}")
        except (ValueError, TypeError, IndexError, KeyError) as exc:
            self.show_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - contain a failed view without losing navigation
            self.show_error(str(exc))
            traceback.print_exc()

    def show_error(self, message):
        self.error.setText(str(message))
        self.error.show()
        self.statusBar().showMessage(str(message))

    def cancel_read(self):
        if self.dispatcher:
            self.dispatcher.cancel()
        self.callbacks.clear()
        if self.search_label.text().startswith("SEARCHING"):
            self.search_label.setText("SEARCH CANCELED")
        self.show_error("Read canceled. Choose an object or press Read selection to continue.")

    def choose_file(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open HDF5 file",
            str(Path(self.filename).parent) if self.filename else "",
            "HDF5 files (*.h5 *.hdf5 *.hdf *.he5);;All files (*)",
        )
        if filename:
            self.open_file(filename)

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Browse local folder")
        if folder:
            self.list_folder(folder)

    def list_folder(self, folder):
        self.files.clear()
        try:
            for path in sorted(Path(folder).iterdir()):
                if path.is_file() and path.suffix.lower() in {".h5", ".hdf5", ".hdf", ".he5"}:
                    item = QtWidgets.QListWidgetItem(path.name)
                    item.setToolTip(str(path))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path))
                    self.files.addItem(item)
        except OSError as exc:
            self.show_error(str(exc))

    def open_file(self, filename, path="/", selection=None):
        filename = str(Path(filename).expanduser().resolve())
        if self.filename is None:
            self.load_file(filename, path, selection)
            return self
        window = MainWindow()
        MainWindow.windows.append(window)
        window.destroyed.connect(
            lambda: MainWindow.windows.remove(window) if window in MainWindow.windows else None
        )
        window.load_file(filename, path, selection)
        window.show()
        return window

    def load_file(self, filename, path="/", selection=None):
        if self.filename is not None:
            return self.open_file(filename, path, selection)
        self.filename = str(Path(filename).expanduser().resolve())
        self.setWindowTitle(f"{Path(self.filename).name} — H5 Viewer")
        self.dispatcher = Dispatcher(self.filename, self)
        self.dispatcher.result.connect(self.receive)
        self.list_folder(Path(self.filename).parent)
        self.add_recent(self.filename)
        self.tree.clear()
        root = QtWidgets.QTreeWidgetItem(["/", "Root"])
        root.setData(
            0, QtCore.Qt.ItemDataRole.UserRole, {"path": "/", "kind": "group", "loaded": False}
        )
        root.addChild(QtWidgets.QTreeWidgetItem(["Loading..."]))
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)
        self.navigate(path, selection=selection)

    def refresh_recents(self):
        self.recent_menu.clear()
        recent = self.settings.value("recent", [], type=list)
        for filename in recent[:12]:
            action = self.recent_menu.addAction(str(filename))
            action.triggered.connect(lambda _checked=False, f=filename: self.open_file(f))
        self.recent_menu.setEnabled(bool(recent))

    def add_recent(self, filename):
        recent = self.settings.value("recent", [], type=list)
        recent = [filename] + [p for p in recent if p != filename]
        self.settings.setValue("recent", recent[:12])
        self.refresh_recents()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_dir():
                    self.list_folder(path)
                else:
                    self.open_file(str(path))
        event.acceptProposedAction()

    def history_move(self, offset):
        index = self.history_index + offset
        if 0 <= index < len(self.history):
            self.history_index = index
            self.navigate(self.history[index], history=False)

    def navigate(self, path, history=True, selection=None, focus=None):
        if not self.filename:
            return
        self.save_selection()
        path = "/" + str(path).strip("/")
        self.path = path
        self.path_edit.setText(path)
        self.generation += 1
        self.error.hide()
        self.title.setText(path)
        self.summary.setText("Loading object information...")
        self.description = {}
        self.properties.clear()
        self.attributes.clear()
        self.replace_model(self.companion_table, None)
        self.array_model = None
        self.replace_model(self.table, None)
        self.clear_data_views()
        self.loaded_views.clear()
        self.attribute_offset = 0
        self.sources.clear()
        if history:
            self.history = self.history[: self.history_index + 1] + [path]
            self.history_index = len(self.history) - 1
        self.back.setEnabled(self.history_index > 0)
        self.forward.setEnabled(self.history_index + 1 < len(self.history))
        self.request(
            {"op": "describe", "path": path}, lambda reply: self.describe(reply, selection, focus)
        )

    def describe(self, reply, initial_selection=None, focus=None):
        self.description = reply
        kind = reply.get("kind", "unknown")
        shape = reply.get("shape")
        self.summary.setText(
            f"{kind.capitalize()}"
            + (
                f"  |  Shape: {shape}  |  Type: {reply.get('dtype', '')}"
                if kind == "dataset"
                else ""
            )
            + "  |  Read-only"
        )
        self.properties.clear()
        for key, value in reply.get("properties", {}).items():
            self.add_property(self.properties.invisibleRootItem(), key, value)
        self.show_attributes(reply)
        self.fields.blockSignals(True)
        self.fields.clear()
        all_fields = QtWidgets.QListWidgetItem("All fields / dataset values")
        all_fields.setData(QtCore.Qt.ItemDataRole.UserRole, "")
        self.fields.addItem(all_fields)
        for field in reply.get("fields", []):
            item = QtWidgets.QListWidgetItem(f"{field['name']}  ({field['dtype']})")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, field["name"])
            item.setToolTip(field["dtype"])
            self.fields.addItem(item)
        self.fields.setCurrentRow(0)
        self.fields.blockSignals(False)
        self.filter_fields(self.field_filter.text())
        self.selection.setVisible(kind == "dataset" and shape is not None)
        self.tabs.blockSignals(True)
        if kind == "dataset":
            self.selection.configure(shape)
            self.companion_axis.blockSignals(True)
            self.companion_axis.clear()
            for axis, size in enumerate(shape or []):
                self.companion_axis.addItem(f"{axis} (length {size:,})", axis)
            self.companion_axis.blockSignals(False)
            self.restore_selection()
            if focus and focus.get("kind") == "field":
                self.fields.blockSignals(True)
                for index in range(self.fields.count()):
                    if self.fields.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == focus.get(
                        "name"
                    ):
                        self.fields.setCurrentRow(index)
                        break
                self.fields.blockSignals(False)
            if initial_selection is not None:
                self.selection.set_selection(initial_selection)
            self.tabs.setCurrentIndex(1)
        else:
            self.tabs.setCurrentIndex(0)
        self.tabs.blockSignals(False)
        if focus and focus.get("kind") == "attribute":
            self.inspector_tabs.setCurrentIndex(1)
            matches = self.attributes.findItems(
                focus.get("name", ""), QtCore.Qt.MatchFlag.MatchExactly, 0
            )
            if matches:
                self.attributes.setCurrentItem(matches[0])
                self.attributes.scrollToItem(matches[0])
        if kind == "group":
            self.contents.clear()
            self.load_contents(0)
        elif kind == "dataset":
            if shape is None:
                self.show_error(
                    "This dataset has a null dataspace; it contains no values. Properties and attributes remain available."
                )
            else:
                self.read_selection()
                self.refresh_companions()
        else:
            self.contents.clear()
            self.show_error("This is a named datatype. Inspect its properties and attributes.")

    def add_property(self, parent, name, value):
        item = QtWidgets.QTreeWidgetItem(
            parent, [str(name), "" if isinstance(value, (dict, list)) else display(value)]
        )
        item.setToolTip(1, display(value))
        if isinstance(value, dict):
            for key, child in value.items():
                self.add_property(item, key, child)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                self.add_property(item, str(i), child)
        return item

    def show_attributes(self, reply):
        self.attributes.clear()
        attributes = reply.get("attributes", [])
        for attribute in attributes:
            value = str(attribute.get("error", attribute.get("text", "")))
            item = QtWidgets.QTreeWidgetItem(
                [
                    attribute["name"],
                    value,
                    f"{attribute.get('dtype', '')} {attribute.get('shape', '')}",
                ]
            )
            item.setToolTip(1, value)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, attribute)
            if attribute.get("error"):
                item.setForeground(1, QtGui.QColor("#a42b24"))
            for ref in attribute.get("references", []):
                child = QtWidgets.QTreeWidgetItem(
                    ["Follow reference", ref["target"], str(ref.get("selection", []))]
                )
                child.setData(
                    0,
                    QtCore.Qt.ItemDataRole.UserRole,
                    {
                        "attribute": attribute["name"],
                        "selection": ref.get("selection", []),
                        **({"field": ref["field"]} if ref.get("field") else {}),
                    },
                )
                item.addChild(child)
            self.attributes.addTopLevelItem(item)
        total = reply.get("attributes_total", len(attributes))
        self.attr_label.setText(
            f"{self.attribute_offset + 1 if total else 0}–{self.attribute_offset + len(attributes)} / {total}"
        )
        self.attr_previous.setEnabled(self.attribute_offset > 0)
        self.attr_next.setEnabled(reply.get("attributes_has_more", False))

    def page_attributes(self, direction):
        self.attribute_offset = max(0, self.attribute_offset + direction * 100)
        self.request(
            {
                "op": "describe",
                "path": self.path,
                "attributes_offset": self.attribute_offset,
                "attributes_limit": 100,
            },
            self.show_attributes,
        )

    def inspect_attribute(self, item, _column):
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if data and "attribute" in data:
            self.request({"op": "reference", "path": self.path, **data}, self.open_reference_result)
        elif data and (data.get("deferred") or data.get("loaded_explicitly")):
            self.request(
                {"op": "attribute", "path": self.path, "attribute": data["name"]},
                self.show_loaded_attribute,
            )
        elif data:
            self.text_dialog(
                f"Attribute: {data['name']}",
                "\n".join(f"{k}: {display(v)}" for k, v in data.items()),
            )

    def show_loaded_attribute(self, reply):
        matches = self.attributes.findItems(reply["name"], QtCore.Qt.MatchFlag.MatchExactly, 0)
        if matches:
            item = matches[0]
            item.setText(1, str(reply.get("text", "")))
            item.setToolTip(1, str(reply.get("text", "")))
            cached = {key: value for key, value in reply.items() if key != "value"}
            cached["loaded_explicitly"] = True
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, cached)
            item.takeChildren()
            for reference in reply.get("references", []):
                child = QtWidgets.QTreeWidgetItem(
                    ["Follow reference", reference["target"], str(reference.get("selection", []))]
                )
                child.setData(
                    0,
                    QtCore.Qt.ItemDataRole.UserRole,
                    {
                        "attribute": reply["name"],
                        "selection": reference.get("selection", []),
                        **({"field": reference["field"]} if reference.get("field") else {}),
                    },
                )
                item.addChild(child)
            if item.childCount():
                item.setExpanded(True)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"Attribute: {self.path} / {reply['name']}")
        dialog.resize(760, 470)
        layout = QtWidgets.QVBoxLayout(dialog)
        summary = QtWidgets.QLabel(
            f"Type: {reply.get('dtype', '')} | Shape: {reply.get('shape', '')}. Double-click a value for its complete bounded text."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if "value" in reply:
            table = QtWidgets.QTableView()
            model = ArrayModel(reply["value"], parent=table)
            table.setModel(model)
            table.doubleClicked.connect(
                lambda index: self.text_dialog(
                    "Attribute value", display(model.value(index.row(), index.column()))
                )
            )
            layout.addWidget(table)
        text = QtWidgets.QPlainTextEdit(str(reply.get("text", "")))
        text.setReadOnly(True)
        text.setMaximumHeight(130)
        layout.addWidget(text)
        if reply.get("references"):
            label = QtWidgets.QLabel(
                "Reference targets are expanded under this attribute in the inspector. Double-click a target to follow it."
            )
            label.setWordWrap(True)
            layout.addWidget(label)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def filter_fields(self, text):
        for index in range(self.fields.count()):
            item = self.fields.item(index)
            item.setHidden(index > 0 and text.casefold() not in item.text().casefold())

    def field_changed(self, current, _previous):
        if current and self.description.get("kind") == "dataset" and not self._restoring:
            self.loaded_views.clear()
            self.read_selection()

    def selected_field(self):
        item = self.fields.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else ""

    def load_contents(self, offset):
        self.request(
            {"op": "children", "path": self.path, "offset": offset, "limit": 200},
            self.show_contents,
        )

    def show_contents(self, reply):
        for row in reply.get("items", []):
            item = QtWidgets.QTreeWidgetItem(
                [
                    row["name"],
                    " / ".join(str(x) for x in [row.get("kind"), row.get("link_kind")] if x),
                    display(row.get("shape", "")),
                    display(row.get("error", row.get("target", row.get("dtype", "")))),
                ]
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
            item.setToolTip(3, display(row))
            self.contents.addTopLevelItem(item)
        if reply.get("has_more"):
            item = QtWidgets.QTreeWidgetItem(["Load more objects..."])
            item.setData(
                0,
                QtCore.Qt.ItemDataRole.UserRole,
                {"more_contents": reply.get("offset", 0) + len(reply.get("items", []))},
            )
            self.contents.addTopLevelItem(item)
        self.statusBar().showMessage(
            f"{reply.get('total', self.contents.topLevelItemCount()):,} objects in {self.path}"
        )

    def expand_tree(self, item):
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not data or data.get("loaded") or data.get("kind") != "group":
            return
        data["loaded"] = True
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, data)
        self.load_tree_children(item, data["path"], 0)

    def load_tree_children(self, parent, path, offset):
        def complete(reply):
            if offset == 0:
                parent.takeChildren()
            for row in reply.get("items", []):
                item = QtWidgets.QTreeWidgetItem(
                    [
                        row["name"],
                        display(row.get("shape", row.get("link_kind", row.get("kind", "")))),
                    ]
                )
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
                item.setToolTip(0, display(row))
                if row.get("kind") == "group" and row.get("expandable", True):
                    item.addChild(QtWidgets.QTreeWidgetItem(["Expand to load..."]))
                parent.addChild(item)
            if reply.get("has_more"):
                item = QtWidgets.QTreeWidgetItem(["Load more objects..."])
                item.setData(
                    0,
                    QtCore.Qt.ItemDataRole.UserRole,
                    {
                        "more_tree": True,
                        "path": path,
                        "offset": offset + len(reply.get("items", [])),
                    },
                )
                parent.addChild(item)

        self.request(
            {"op": "children", "path": path, "offset": offset, "limit": 200},
            complete,
            navigation=False,
        )

    def activate_tree(self, item):
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data.get("more_tree"):
            parent = item.parent()
            parent.removeChild(item)
            self.load_tree_children(parent, data["path"], data["offset"])
        else:
            self.activate_item(data)

    def activate_item(self, data):
        if not data:
            return
        if "more_contents" in data:
            self.contents.takeTopLevelItem(self.contents.topLevelItemCount() - 1)
            self.load_contents(data["more_contents"])
        elif data.get("link_kind", "").lower() == "external":
            self.request({"op": "sources", "path": data["path"]}, self.follow_external)
        elif data.get("path"):
            self.navigate(data["path"])

    def follow_external(self, reply):
        candidates = [row for row in reply.get("items", []) if row.get("resolved_file")]
        if not candidates:
            self.show_error(
                "The external target could not be resolved. Inspect Sources / Links for details."
            )
            return
        row = candidates[0]
        self.open_file(row["resolved_file"], row.get("dataset", "/"))

    def search_file(self):
        query = self.search.text().strip()
        if not query:
            return
        self.search_revision += 1
        revision = self.search_revision
        self.search_results.clear()
        self.search_label.setText("SEARCHING... (Cancel read stops search)")

        def complete(reply):
            if revision != self.search_revision:
                return
            items = reply.get("items", [])
            for row in items:
                item = QtWidgets.QTreeWidgetItem(
                    [row.get("name", ""), row["path"], row.get("kind", "")]
                )
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
                item.setToolTip(0, display(row))
                self.search_results.addTopLevelItem(item)
            self.search_label.setText(
                f"SEARCH RESULTS: {len(items):,}"
                + (" (limit reached)" if reply.get("truncated") else "")
            )

        self.request({"op": "search", "query": query, "limit": 1000}, complete, navigation=False)

    def activate_search(self, item, _column):
        row = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if row:
            path = row.get("object_path", row["path"])
            self.navigate(path, focus=row)

    def tab_changed(self, index):
        if index == 4 and self.filename:
            self.load_sources()
            return
        if not self.description:
            return
        if (
            index in (1, 2, 3)
            and self.description.get("kind") == "dataset"
            and index not in self.loaded_views
        ):
            self.read_selection(invalidate=False)

    @staticmethod
    def replace_model(view, model):
        previous = view.model()
        previous_selection = view.selectionModel()
        view.setModel(model)
        if previous_selection is not None and previous_selection is not view.selectionModel():
            previous_selection.setParent(None)
            previous_selection.deleteLater()
        if previous is not None and previous is not model:
            previous.setParent(None)
            previous.deleteLater()

    def clear_view(self, index):
        self.loaded_views.discard(index)
        if index == 1:
            self.array_model = None
            self.replace_model(self.table, None)
            self.table_note.setText("Choose a selection to read values.")
        elif index == 2:
            self.plot.clear()
        elif index == 3:
            self.image.clear()

    def clear_data_views(self):
        self.last_reply = None
        for index in (1, 2, 3):
            self.clear_view(index)
        self.replace_model(self.companion_table, None)

    def read_selection(self, invalidate=True, native=False):
        if self.description.get("kind") != "dataset" or self.description.get("shape") is None:
            return
        try:
            selection = self.selection.selection()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.error.hide()
        if invalidate:
            self.clear_data_views()
            self.loaded_views.clear()
            self.read_revision += 1
        revision = self.read_revision
        index = self.tabs.currentIndex()
        if index not in (1, 2, 3):
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(1)
            self.tabs.blockSignals(False)
            index = 1
        self.clear_view(index)
        purpose = "probe" if native else {1: "table", 2: "plot", 3: "image"}[index]
        request = {"op": "read", "path": self.path, "selection": selection, "purpose": purpose}
        field = self.selected_field()
        if field:
            request["field"] = field
        self.save_selection()

        def complete(reply):
            if revision != self.read_revision:
                return
            actual = reply.get("selection", selection)
            if index == 1:
                self.array_model = ArrayModel(
                    reply["array"], actual, field, self.selection.row_axis.currentData(), self.table
                )
                self.replace_model(self.table, self.array_model)
                self.table_note.setText(
                    f"Source selection: {actual}  |  {self.array_model.rowCount():,} rows, {self.array_model.columnCount():,} columns. "
                    + " ".join(reply.get("notes", []))
                    + " Double-click to inspect a cell; right-click to copy or follow a reference."
                )
            elif index == 2:
                self.plot.set_result(reply, self.selection.row_axis.currentData())
            elif index == 3:
                self.image.set_result(
                    reply,
                    self.selection.row_axis.currentData(),
                    self.selection.column_axis.currentData(),
                )
            self.last_reply = reply
            self.loaded_views.add(index)

        self.request(request, complete)
        self.read_companion()

    def use_image_plane(self):
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(3)
        self.tabs.blockSignals(False)
        self.read_selection()

    def read_pixel(self, selection):
        self.probe_revision += 1
        probe_revision = self.probe_revision
        read_revision = self.read_revision
        request = {"op": "read", "path": self.path, "selection": selection, "purpose": "probe"}
        field = self.selected_field()
        if field:
            request["field"] = field

        def complete(reply):
            if probe_revision == self.probe_revision and read_revision == self.read_revision:
                self.image.show_probe(reply)

        self.request(request, complete)

    def read_native_region(self, selection):
        self.selection.set_selection(selection)
        self.read_selection(native=True)

    def table_menu(self, pos):
        index = self.table.indexAt(pos)
        menu = QtWidgets.QMenu(self)
        copy = menu.addAction("Copy selected cells")
        copy.triggered.connect(self.copy_cells)
        if index.isValid():
            inspect = menu.addAction("Inspect cell...")
            inspect.triggered.connect(lambda: self.inspect_cell(index))
            follow = menu.addAction("Follow object / region reference")
            follow.triggered.connect(lambda: self.follow_reference(index))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def copy_cells(self):
        if self.array_model is None:
            return
        indices = self.table.selectionModel().selectedIndexes()
        if not indices:
            return
        rows = sorted({i.row() for i in indices})
        cols = sorted({i.column() for i in indices})
        selected = {(i.row(), i.column()) for i in indices}
        text = "\n".join(
            "\t".join(
                display(self.array_model.value(r, c)) if (r, c) in selected else "" for c in cols
            )
            for r in rows
        )
        QtWidgets.QApplication.clipboard().setText(text)

    def inspect_cell(self, index):
        if self.array_model is None or not index.isValid():
            return
        value = self.array_model.value(index.row(), index.column())
        if isinstance(value, str) and (
            value.startswith(("object ->", "region ->")) or value == "NULL reference"
        ):
            self.follow_reference(index)
            return
        selection = self.array_model.source_index(index.row(), index.column())
        field = self.array_model.cell_field(index.column())
        request = {"op": "cell", "path": self.path, "selection": selection}
        if field:
            request["field"] = field
        self.request(request, lambda reply: self.cell_dialog(reply, selection, field))

    def cell_dialog(self, reply, selection, field):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"{self.path}{'.' + field if field else ''} at {selection}")
        dialog.resize(780, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        array = np.asarray(reply["array"])
        label = QtWidgets.QLabel(
            f"Expanded value: shape {array.shape}, type {array.dtype}. "
            + " ".join(reply.get("notes", []))
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        table = QtWidgets.QTableView()
        model = ArrayModel(array, parent=table)
        table.setModel(model)
        layout.addWidget(table)
        text = QtWidgets.QPlainTextEdit(display(array))
        text.setReadOnly(True)
        text.setMaximumHeight(100)
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def follow_reference(self, index):
        if self.array_model is None:
            return
        request = {
            "op": "reference",
            "path": self.path,
            "selection": self.array_model.source_index(index.row(), index.column()),
        }
        field = self.array_model.cell_field(index.column())
        if field:
            request["field"] = field

        self.request(request, self.open_reference_result)

    def open_reference_result(self, reply):
        target = reply.get("target")
        if not target:
            self.show_error("This is a null reference; no target object is stored.")
            return
        filename = reply.get("filename", self.filename)
        if Path(filename).resolve() == Path(self.filename).resolve():
            self.navigate(target, selection=reply.get("selection"))
        else:
            self.open_file(filename, target, reply.get("selection"))
        if reply.get("region") and not reply.get("selection"):
            self.text_dialog(
                "Region reference",
                "Target opened. Region description:\n"
                + display(reply["region"])
                + "\nThis irregular region is not automatically loaded; choose a bounded selection.",
            )

    def load_sources(self):
        self.request({"op": "sources", "path": self.path}, self.show_sources)

    def show_sources(self, reply):
        self.sources.clear()
        for row in reply.get("items", []):
            item = QtWidgets.QTreeWidgetItem(
                [
                    str(row.get("kind", "")),
                    str(row.get("recorded_file", "")),
                    f"{row.get('resolved_file', '')} {row.get('dataset', '')}",
                    str(row.get("status", "")),
                ]
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
            item.setToolTip(2, display(row))
            for key, value in row.items():
                if key not in {"kind", "recorded_file", "resolved_file", "dataset", "status"}:
                    self.add_property(item, key, value)
            self.sources.addTopLevelItem(item)
        if reply.get("notice"):
            self.sources.addTopLevelItem(QtWidgets.QTreeWidgetItem([str(reply["notice"])]))
        elif not reply.get("items"):
            self.sources.addTopLevelItem(
                QtWidgets.QTreeWidgetItem(["No source relationships recorded for this object."])
            )
        self.loaded_views.add(4)

    def activate_source(self, item, _column):
        row = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not row:
            return
        filename = row.get("resolved_file")
        target = row.get("dataset")
        if filename and target:
            if Path(filename).resolve() == Path(self.filename).resolve():
                self.navigate(target)
            else:
                self.open_file(filename, target)
        elif target:
            self.navigate(target)
        else:
            self.text_dialog(
                "Source details",
                "\n".join(f"{key}: {display(value)}" for key, value in row.items()),
            )

    def text_dialog(self, title, text):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(700, 450)
        layout = QtWidgets.QVBoxLayout(dialog)
        box = QtWidgets.QPlainTextEdit(text)
        box.setReadOnly(True)
        layout.addWidget(box)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def object_menu(self, widget, position):
        item = widget.itemAt(position)
        if item is None:
            return
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not data:
            return
        menu = QtWidgets.QMenu(self)
        path = data.get("path", data.get("dataset", self.path))
        action = menu.addAction("Copy object path")
        action.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(path))
        details = menu.addAction("Inspect relationship / object details...")
        details.triggered.connect(
            lambda: self.text_dialog(
                "Object details", "\n".join(f"{k}: {display(v)}" for k, v in data.items())
            )
        )
        menu.exec(widget.viewport().mapToGlobal(position))

    def refresh_companions(self):
        enabled = self.companion_enabled.isChecked() and self.description.get("kind") == "dataset"
        for widget in (
            self.companion_axis_label,
            self.companion_axis,
            self.companion_choice,
            self.companion_note,
            self.companion_table,
        ):
            widget.setVisible(enabled)
        if not enabled:
            return
        axis = self.companion_axis.currentData()
        shape = self.description.get("shape")
        if not shape or axis is None:
            self.companion_note.setText("Scalar and null datasets have no sample axis.")
            return
        length = shape[axis]
        current = self.companion_choice.currentData()
        current_path = current.get("path") if current else None
        self.companion_choice.blockSignals(True)
        self.companion_choice.clear()
        self.companion_choice.addItem("Choose a sibling dataset...", None)
        self.companion_choice.blockSignals(False)
        self.replace_model(self.companion_table, None)
        parent = self.path.rsplit("/", 1)[0] or "/"
        self.companion_note.setText(
            f"Matching sibling leading length {length:,}. Select a dataset; matching length alone does not establish a relationship."
        )

        def complete(reply):
            if axis != self.companion_axis.currentData() or not self.companion_enabled.isChecked():
                return
            self.companion_choice.blockSignals(True)
            for row in reply.get("items", []):
                candidate_shape = row.get("shape")
                if (
                    row.get("kind") == "dataset"
                    and candidate_shape
                    and candidate_shape[0] == length
                    and row["path"] != self.path
                ):
                    self.companion_choice.addItem(f"{row['name']} {candidate_shape}", row)
            self.companion_choice.blockSignals(False)
            if reply.get("has_more"):
                self.companion_note.setText(
                    "Showing matches among the first 1,000 siblings. Use the object browser to inspect later objects."
                )
            for index in range(1, self.companion_choice.count()):
                if self.companion_choice.itemData(index)["path"] == current_path:
                    self.companion_choice.setCurrentIndex(index)
                    break

        self.request({"op": "children", "path": parent, "offset": 0, "limit": 1000}, complete)

    def read_companion(self):
        if not self.companion_enabled.isChecked() or self.description.get("kind") != "dataset":
            return
        candidate = self.companion_choice.currentData()
        axis = self.companion_axis.currentData()
        if not candidate or axis is None:
            return
        try:
            selected = self.selection.selection()[axis]
        except (IndexError, ValueError):
            return
        index = selected[0] if isinstance(selected, list) else selected
        selection = [index] + [[0, min(n, 32), 1] for n in candidate["shape"][1:]]
        revision = self.read_revision

        def complete(reply):
            if revision != self.read_revision or candidate != self.companion_choice.currentData():
                return
            model = ArrayModel(
                reply["array"], reply.get("selection", selection), parent=self.companion_table
            )
            self.replace_model(self.companion_table, model)
            self.companion_note.setText(
                f"User-selected companion: {candidate['path']} at row {index}. Leading lengths match; no semantic relationship is assumed. Selection: {reply.get('selection', selection)}"
            )

        self.request(
            {"op": "read", "path": candidate["path"], "selection": selection, "purpose": "table"},
            complete,
        )

    def selection_key(self):
        if not self.filename or self.description.get("kind") != "dataset":
            return None
        try:
            stat = Path(self.filename).stat()
        except OSError:
            return None
        identity = [
            str(Path(self.filename).resolve()),
            stat.st_size,
            stat.st_mtime_ns,
            self.path,
            self.description.get("shape"),
        ]
        return "datasets/" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def save_selection(self):
        key = self.selection_key()
        if key:
            try:
                value = {
                    "selection": self.selection.selection(),
                    "row": self.selection.row_axis.currentData(),
                    "column": self.selection.column_axis.currentData(),
                    "field": self.selected_field(),
                    "page": self.selection.page_size.value(),
                }
                self.settings.setValue(key, json.dumps(value))
            except ValueError:
                pass

    def restore_selection(self):
        key = self.selection_key()
        if not key:
            return
        raw = self.settings.value(key)
        if not raw:
            return
        try:
            value = json.loads(raw)
            self._restoring = True
            self.selection.row_axis.setCurrentIndex(
                self.selection.row_axis.findData(value.get("row", 0))
            )
            self.selection.column_axis.setCurrentIndex(
                self.selection.column_axis.findData(value.get("column", -1))
            )
            self.selection.set_selection(value["selection"])
            self.selection.page_size.setValue(value.get("page", 200))
            for i in range(self.fields.count()):
                if self.fields.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == value.get(
                    "field", ""
                ):
                    self.fields.setCurrentRow(i)
                    break
            self.selection.selection()
        except (ValueError, TypeError, KeyError):
            self.selection.configure(self.description.get("shape"))
        finally:
            self._restoring = False

    def closeEvent(self, event):
        self.save_selection()
        self.settings.setValue("geometry", self.saveGeometry())
        self.callbacks.clear()
        if self.dispatcher:
            self.dispatcher.shutdown()
            if not self.dispatcher.wait(5000):
                self.show_error(
                    "The reader is still shutting down. Close the window again after the active read stops."
                )
                event.ignore()
                return
        event.accept()
