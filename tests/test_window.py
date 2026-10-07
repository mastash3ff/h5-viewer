"""Observable desktop workflows using generated files and the real reader process."""

import numpy as np
import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from h5_viewer.ui_data import ArrayModel, SelectionEditor
from h5_viewer.window import MainWindow


@pytest.fixture
def window(qtbot, sample_h5, monkeypatch, tmp_path):
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(
        QtCore.QSettings.Format.IniFormat,
        QtCore.QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    widget = MainWindow(str(sample_h5))
    qtbot.addWidget(widget)
    widget.show()
    qtbot.waitUntil(
        lambda: (
            widget.description.get("kind") == "group" and widget.contents.topLevelItemCount() > 0
        ),
        timeout=15000,
    )
    yield widget
    for sibling in MainWindow.windows[:]:
        sibling.close()


def wait_table(qtbot, window, path):
    window.navigate(path)
    qtbot.waitUntil(
        lambda: window.array_model is not None or window.error.isVisible(), timeout=15000
    )
    assert window.array_model is not None, window.error.text()
    return window.array_model


def test_starts_at_contents_and_preserves_large_integers(qtbot, window):
    assert window.path == "/"
    assert window.tabs.currentIndex() == 0
    model = wait_table(qtbot, window, "/scalar")
    assert window.tabs.currentIndex() == 1
    assert model.data(model.index(0, 0)) == str(2**62 + 1)
    assert model.rowCount() == model.columnCount() == 1
    model = wait_table(qtbot, window, "/records")
    assert model.data(model.index(0, 0)) == str(2**62 + 1)
    assert "position.x" in model.fields
    assert model.data(model.index(0, model.fields.index("position.x"))) == "1.25"
    assert window.fields.count() >= 4


def test_nested_field_plot_and_statistics(qtbot, window):
    wait_table(qtbot, window, "/records")
    for i in range(window.fields.count()):
        if window.fields.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == "position.x":
            window.fields.setCurrentRow(i)
            break
    qtbot.waitUntil(
        lambda: window.array_model is not None and window.array_model.field == "position.x",
        timeout=10000,
    )
    np.testing.assert_array_equal(window.array_model.array, [1.25, 2.5, 3.75, 5])
    window.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: 2 in window.loaded_views, timeout=10000)
    assert "min=1.25" in window.plot.stats.toPlainText()
    assert "max=5.0" in window.plot.stats.toPlainText()
    window.plot.histogram.setChecked(True)
    assert window.plot.plot.listDataItems()


def test_four_dimensional_selection_image_and_native_coordinates(qtbot, window):
    wait_table(qtbot, window, "/stack")
    window.selection.row_axis.setCurrentIndex(window.selection.row_axis.findData(2))
    window.selection.column_axis.setCurrentIndex(window.selection.column_axis.findData(3))
    window.selection.set_selection([1, 1, [0, 4, 2], [1, 5, 2]])
    window.read_selection()
    qtbot.waitUntil(
        lambda: (
            window.array_model is not None
            and window.array_model.selection == [1, 1, [0, 4, 2], [1, 5, 2]]
        ),
        timeout=10000,
    )
    assert window.array_model.source_index(1, 1) == [1, 1, 2, 3]
    assert window.array_model.value(1, 1) == 73
    window.tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: 3 in window.loaded_views, timeout=10000)
    assert window.image.array.shape == (2, 2)
    assert "stride Y=2, X=2" in window.image.note.text()
    window.image.plot.setAspectLocked(False)
    window.image.plot.setRange(xRange=(1, 4), yRange=(0, 3), padding=0)
    window.image.native_region()
    qtbot.waitUntil(
        lambda: (
            window.last_reply is not None
            and window.last_reply.get("selection") == [1, 1, [0, 3, 1], [1, 4, 1]]
        ),
        timeout=10000,
    )
    assert window.image.array.shape == (3, 3)


def test_sources_dimension_scales_and_region_references(qtbot, window):
    wait_table(qtbot, window, "/matrix")
    window.tabs.setCurrentIndex(4)
    qtbot.waitUntil(lambda: 4 in window.loaded_views, timeout=10000)
    scale = next(
        window.sources.topLevelItem(i)
        for i in range(window.sources.topLevelItemCount())
        if window.sources.topLevelItem(i).text(0) == "dimension_scale"
    )
    window.activate_source(scale, 0)
    qtbot.waitUntil(
        lambda: window.path == "/row_coordinate" and window.array_model is not None, timeout=10000
    )
    model = wait_table(qtbot, window, "/region_refs")
    window.follow_reference(model.index(0, 0))
    qtbot.waitUntil(
        lambda: window.path == "/matrix" and window.array_model is not None, timeout=10000
    )
    assert window.array_model.selection == [[1, 3, 1], [2, 5, 1]]
    np.testing.assert_array_equal(window.array_model.array, [[8, 9, 10], [14, 15, 16]])


def test_search_and_external_link_new_window(qtbot, window):
    window.search.setText("position")
    window.search_file()
    qtbot.waitUntil(lambda: "SEARCH RESULTS:" in window.search_label.text(), timeout=10000)
    assert window.search_results.topLevelItemCount() >= 2
    window.activate_item({"path": "/external", "link_kind": "external"})
    qtbot.waitUntil(lambda: bool(MainWindow.windows), timeout=10000)
    child = MainWindow.windows[-1]
    qtbot.waitUntil(lambda: child.array_model is not None or child.error.isVisible(), timeout=15000)
    assert child.array_model is not None, child.error.text()
    assert child.path == "/values"
    assert child.array_model.value(0, 0) == -9


def test_exact_table_indices_and_dimension_paging(qtbot):
    data = np.arange(12).reshape(3, 4)
    model = ArrayModel(data, [[2, 8, 2], [10, 22, 3]], row_axis=1)
    assert model.rowCount() == 4
    assert model.columnCount() == 3
    assert model.source_index(2, 1) == [4, 16]
    assert model.value(2, 1) == 6
    editor = SelectionEditor()
    qtbot.addWidget(editor)
    editor.configure([1000])
    editor.page(1)
    assert editor.selection() == [[200, 400, 1]]
    editor.jump.setText("980")
    editor.jump_to()
    assert editor.selection() == [[980, 1000, 1]]
    editor.edits[0].setText("0:1001:1")
    with pytest.raises(ValueError, match="Slice"):
        editor.selection()


def test_image_preset_fixed_index_and_opt_in_companion(qtbot, window):
    wait_table(qtbot, window, "/stack")
    assert not window.companion_enabled.isChecked()
    window.selection.use_image_plane()
    qtbot.waitUntil(lambda: 3 in window.loaded_views, timeout=10000)
    assert window.selection.selection() == [0, 0, [0, 4, 1], [0, 5, 1]]
    window.companion_enabled.setChecked(True)
    qtbot.waitUntil(lambda: window.companion_choice.count() > 1, timeout=10000)
    for index in range(1, window.companion_choice.count()):
        if window.companion_choice.itemData(index)["path"] == "/text":
            window.companion_choice.setCurrentIndex(index)
            break
    qtbot.waitUntil(lambda: window.companion_table.model() is not None, timeout=10000)
    assert "alpha" in window.companion_table.model().data(
        window.companion_table.model().index(0, 0)
    )
    window.selection.step_index(0, 1)
    qtbot.waitUntil(lambda: "row 1" in window.companion_note.text(), timeout=10000)
    assert "beta" in window.companion_table.model().data(window.companion_table.model().index(0, 0))
    assert window.image.selection[0] == 1
    assert window.selection.selection()[2:] == [[0, 4, 1], [0, 5, 1]]


def test_ragged_cell_metadata_and_missing_target_recovery(qtbot, window):
    model = wait_table(qtbot, window, "/ragged")
    assert "1" in model.data(model.index(0, 0))
    window.inspect_cell(model.index(0, 0))
    qtbot.waitUntil(
        lambda: any(
            child.windowTitle().startswith("/ragged")
            for child in window.findChildren(QtWidgets.QDialog)
        ),
        timeout=10000,
    )
    window.navigate("/broken")
    qtbot.waitUntil(lambda: window.error.isVisible(), timeout=10000)
    assert window.tree.topLevelItemCount() == 1
    window.tabs.setCurrentIndex(4)
    qtbot.waitUntil(lambda: 4 in window.loaded_views, timeout=10000)
    assert window.sources.topLevelItem(0).text(0) == "soft_link"
    wait_table(qtbot, window, "/numbers")
    assert not window.error.isVisible()


def test_dispatcher_cancellation_keeps_requests_off_gui_thread(qtbot, monkeypatch):
    import threading

    from h5_viewer import window as window_module

    gui_thread = threading.get_ident()
    started = threading.Event()
    stopped = threading.Event()
    observed = []

    class Client:
        def __init__(self, filename):
            observed.append(("construct", threading.get_ident()))

        def request(self, request):
            observed.append(("request", threading.get_ident()))
            if request["op"] == "blocked":
                started.set()
                assert stopped.wait(5)
                raise RuntimeError("Canceled")
            return {"done": True}

        def cancel(self):
            stopped.set()

        def close(self):
            stopped.set()

    monkeypatch.setattr(window_module, "ReaderClient", Client)
    dispatcher = window_module.Dispatcher("fake.h5")
    replies = []
    dispatcher.result.connect(lambda *args: replies.append(args))
    dispatcher.submit(1, {"op": "blocked"})
    qtbot.waitUntil(started.is_set)
    dispatcher.cancel()
    dispatcher.submit(2, {"op": "describe"})
    qtbot.waitUntil(lambda: any(row[0] == 2 for row in replies))
    assert all(thread != gui_thread for _, thread in observed)
    assert next(row for row in replies if row[0] == 2)[1] == {"done": True}
    dispatcher.shutdown()
    assert dispatcher.wait(5000)


def test_attribute_reference_navigation_and_paging(qtbot, sample_h5):
    import h5py

    with h5py.File(sample_h5, "a") as file:
        group = file["group"]
        group.attrs["00_target"] = file["numbers"].ref
        for index in range(110):
            group.attrs[f"item_{index:03d}"] = index
    window = MainWindow(str(sample_h5))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.description.get("kind") == "group", timeout=10000)
    window.navigate("/group")
    qtbot.waitUntil(lambda: window.attributes.topLevelItemCount() == 100, timeout=10000)
    assert window.attr_next.isEnabled()
    attribute = window.attributes.topLevelItem(0)
    assert attribute.text(0) == "00_target"
    assert attribute.childCount() == 1
    window.inspect_attribute(attribute.child(0), 0)
    qtbot.waitUntil(
        lambda: window.path == "/numbers" and window.array_model is not None, timeout=10000
    )
    window.navigate("/group")
    qtbot.waitUntil(lambda: window.attributes.topLevelItemCount() == 100, timeout=10000)
    window.page_attributes(1)
    qtbot.waitUntil(lambda: window.attributes.topLevelItemCount() == 12, timeout=10000)
    assert not window.attr_next.isEnabled()
    assert window.attr_previous.isEnabled()
    assert "/ 112" in window.attr_label.text()


def test_dtype_presentation_preserves_raw_values(qtbot):
    enum = np.dtype("u1", metadata={"enum": {"OFF": 0, "ON": 1}})
    nested = np.dtype(
        [
            ("state", enum),
            ("payload", "V2"),
            ("label", np.dtype("S8", metadata={"h5py_encoding": "utf-8"})),
        ]
    )
    values = np.zeros(1, dtype=nested)
    values["state"] = 1
    values["payload"] = np.void(b"\x01\xff")
    values["label"] = "café".encode()
    model = ArrayModel(values)
    assert model.data(model.index(0, 0)) == "ON (1)"
    assert model.value(0, 0) == 1
    assert model.data(model.index(0, 1)) == "0x01ff"
    assert model.value(0, 1).tobytes() == b"\x01\xff"
    assert model.data(model.index(0, 2)) == "café"
    assert model.value(0, 2) == "café".encode()
    assert "utf-8" in model.data(model.index(0, 2), QtCore.Qt.ItemDataRole.ToolTipRole)
    invalid = ArrayModel(np.array([b"\xff"], dtype="S1"))
    assert invalid.data(invalid.index(0, 0)) == "0xff"
    bitfield = ArrayModel(np.array([5], dtype=np.dtype("u2", metadata={"hdf5_class": "bitfield"})))
    assert bitfield.data(bitfield.index(0, 0)) == "0x0005"


def test_exact_pixel_click_does_not_report_a_nearby_preview_sample(qtbot, window):
    wait_table(qtbot, window, "/matrix")
    window.selection.set_selection([[0, 10, 2], [0, 6, 2]])
    window.read_selection()
    qtbot.waitUntil(lambda: window.array_model is not None, timeout=10000)
    window.tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: 3 in window.loaded_views, timeout=10000)
    QtWidgets.QApplication.processEvents()
    scene_point = window.image.plot.plotItem.vb.mapViewToScene(QtCore.QPointF(3.2, 3.2))
    viewport_point = window.image.plot.mapFromScene(scene_point)
    viewport = window.image.plot.viewport()
    # QWidget QtTest mouseMove relies on native cursor delivery, which is absent
    # in some WSL-launched Windows sessions. Dispatch the real viewport event to
    # exercise QGraphicsView -> scene -> probe routing without that dependency.
    move = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove,
        QtCore.QPointF(viewport_point),
        QtCore.QPointF(viewport.mapToGlobal(viewport_point)),
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.KeyboardModifier.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(viewport, move)
    qtbot.waitUntil(
        lambda: "Preview sample at [2, 2]: raw 14.0" in window.image.hover_note.text(), timeout=3000
    )
    assert "Pointer at source [3, 3]" in window.image.hover_note.text()
    qtbot.mouseClick(
        window.image.plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=viewport_point
    )
    qtbot.waitUntil(
        lambda: "Exact source [3, 3]: raw 21.0" in window.image.probe_note.text(), timeout=10000
    )
    assert window.image.selection == [[0, 10, 2], [0, 6, 2]]
    assert not window.companion_axis_label.isVisible()


def test_replaced_models_are_released_and_stale_views_are_cleared(qtbot, window):
    import weakref

    wait_table(qtbot, window, "/matrix")
    old_arrays = []
    for index in range(4):
        old_arrays.append(weakref.ref(window.array_model.array))
        window.selection.set_selection([[index, index + 2, 1], [0, 6, 1]])
        window.read_selection()
        qtbot.waitUntil(lambda: window.array_model is not None, timeout=10000)
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        assert len(window.table.findChildren(ArrayModel)) == 1
    assert all(reference() is None for reference in old_arrays)
    window.tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: 3 in window.loaded_views, timeout=10000)
    window.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: 2 in window.loaded_views, timeout=10000)
    assert window.image.array is not None
    assert window.plot.columns
    wait_table(qtbot, window, "/text")
    assert window.image.array is None
    assert not window.plot.columns
    window.tabs.setCurrentIndex(3)
    qtbot.waitUntil(lambda: window.error.isVisible(), timeout=10000)
    assert window.image.array is None
    assert window.image.item.image is None
    window.tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: "numeric" in window.error.text(), timeout=10000)
    assert not window.plot.plot.listDataItems()


def test_deferred_attribute_load_and_compound_reference(qtbot, sample_h5):
    import h5py

    with h5py.File(sample_h5, "a") as file:
        file["group"].attrs["labels"] = np.array(["north", "south"], dtype=h5py.string_dtype())
        compound = np.empty((), dtype=[("target", h5py.ref_dtype), ("id", "i4")])
        compound["target"] = file["numbers"].ref
        compound["id"] = 7
        file["group"].attrs["reference_record"] = compound
    window = MainWindow(str(sample_h5))
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.description.get("kind") == "group", timeout=10000)
    window.navigate("/group")

    def attribute(name):
        items = window.attributes.findItems(name, QtCore.Qt.MatchFlag.MatchExactly, 0)
        return items[0] if items else None

    qtbot.waitUntil(lambda: attribute("labels") is not None, timeout=10000)
    labels = attribute("labels")
    assert labels.data(0, QtCore.Qt.ItemDataRole.UserRole)["deferred"]
    window.inspect_attribute(labels, 0)
    qtbot.waitUntil(lambda: "south" in labels.text(1), timeout=10000)
    dialogs = [d for d in window.findChildren(QtWidgets.QDialog) if "labels" in d.windowTitle()]
    assert dialogs
    table = dialogs[0].findChild(QtWidgets.QTableView)
    assert table.model().value(1, 0) in ("south", b"south")
    record = attribute("reference_record")
    assert record.childCount() == 1
    assert record.child(0).data(0, QtCore.Qt.ItemDataRole.UserRole)["field"] == "target"
    window.inspect_attribute(record.child(0), 0)
    qtbot.waitUntil(
        lambda: window.path == "/numbers" and window.array_model is not None, timeout=10000
    )
    window.navigate("/matrix")
    qtbot.waitUntil(lambda: attribute("DIMENSION_LIST") is not None, timeout=10000)
    dimensions = attribute("DIMENSION_LIST")
    assert dimensions.data(0, QtCore.Qt.ItemDataRole.UserRole)["deferred"]
    window.inspect_attribute(dimensions, 0)
    qtbot.waitUntil(lambda: dimensions.childCount() == 1, timeout=10000)
    window.inspect_attribute(dimensions.child(0), 0)
    qtbot.waitUntil(
        lambda: window.path == "/row_coordinate" and window.array_model is not None, timeout=10000
    )


def test_obsolete_search_result_is_ignored(qtbot, window, monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        window, "request", lambda request, callback, **kwargs: callbacks.append(callback)
    )
    window.search.setText("alpha")
    window.search_file()
    window.search.setText("beta")
    window.search_file()
    callbacks[0]({"items": [{"name": "alpha", "path": "/alpha", "kind": "dataset"}]})
    callbacks[1]({"items": [{"name": "beta", "path": "/beta", "kind": "dataset"}]})
    assert window.search_results.topLevelItemCount() == 1
    assert window.search_results.topLevelItem(0).text(0) == "beta"
    assert window.search_label.text() == "SEARCH RESULTS: 1"


def test_image_display_cap_precedes_conversion_and_clears_previous(qtbot, monkeypatch):
    from h5_viewer import ui_data

    panel = ui_data.ImagePanel()
    qtbot.addWidget(panel)
    panel.set_result({"array": np.ones((2, 2)), "selection": [[0, 2, 1], [0, 2, 1]]}, 0, 1)
    assert panel.array is not None
    monkeypatch.setattr(ui_data, "MAX_IMAGE_PIXELS", 4)
    with pytest.raises(ValueError, match="smaller native region"):
        panel.set_result(
            {"array": np.zeros((2, 3), dtype="u1"), "selection": [[0, 2, 1], [0, 3, 1]]}, 0, 1
        )
    assert panel.array is None
    assert panel.item.image is None
