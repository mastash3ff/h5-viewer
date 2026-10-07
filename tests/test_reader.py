import os

import h5py
import numpy as np
import pytest

from h5_viewer.reader import Reader, ReadLimitError


@pytest.fixture
def fixture(tmp_path):
    filename = tmp_path / "inspection.h5"
    with h5py.File(filename, "w") as f:
        group = f.create_group("group")
        group["ancestor"] = f["/"]
        f["alias"] = group
        f["soft"] = h5py.SoftLink("/group")
        f["broken"] = h5py.SoftLink("/absent")
        f["external"] = h5py.ExternalLink("missing.h5", "/data")
        ds = f.create_dataset("numbers", data=np.arange(120).reshape(10, 12))
        ds.attrs["description"] = "known values"
        f.create_dataset("null", dtype="f8")
        f.create_dataset("scalar", data=np.uint64(2**63 + 31))
        dtype = np.dtype([("id", "u8"), ("nested", [("x", "i4"), ("y", "f8")])])
        values = np.zeros(3, dtype=dtype)
        values["id"] = [2**63 + 1, 2**63 + 2, 2**63 + 3]
        values["nested"]["x"] = [3, 4, 5]
        f.create_dataset("records", data=values)
        ragged = f.create_dataset("ragged", (2,), dtype=h5py.vlen_dtype(np.dtype("i4")))
        ragged[0] = np.arange(7)
        ragged[1] = np.arange(2)
        text = f.create_dataset("text", (1,), dtype=h5py.string_dtype())
        text[0] = "x" * 100_000
        f.create_dataset(
            "enum", data=np.array([0, 1], dtype=h5py.enum_dtype({"off": 0, "on": 1}, "i1"))
        )
        f.create_dataset(
            "opaque", data=np.array([b"\x01\x02"], dtype=h5py.opaque_dtype(np.dtype("V2")))
        )
        refs = f.create_dataset("references", (2,), dtype=h5py.ref_dtype)
        refs[0] = ds.ref
        regions = f.create_dataset("regions", (1,), dtype=h5py.regionref_dtype)
        regions[0] = ds.regionref[2:4, 3:8:2]
        axis = f.create_dataset("row_axis", data=np.arange(10))
        axis.make_scale("rows")
        ds.dims[0].attach_scale(axis)
        ds.dims[0].label = "row"
        typ = h5py.h5t.py_create(np.dtype("f8"))
        typ.commit(f.id, b"float_type")
    return filename


def test_structure_fields_attributes_cycles(fixture):
    reader = Reader(fixture)
    try:
        root = reader.handle({"op": "children", "limit": 3})
        assert len(root["items"]) == 3 and root["has_more"]
        group = reader.handle({"op": "children", "path": "/group"})["items"][0]
        assert group["cycle"] and not group["expandable"]
        external = next(
            x for x in reader.handle({"op": "children"})["items"] if x["name"] == "external"
        )
        assert external["kind"] == "external_link" and not reader._files.get(
            str(fixture.parent / "missing.h5")
        )
        records = reader.handle({"op": "describe", "path": "/records"})
        assert [x["name"] for x in records["fields"]] == ["id", "nested", "nested.x", "nested.y"]
        desc = reader.handle({"op": "describe", "path": "/numbers"})
        assert (
            next(x for x in desc["attributes"] if x["name"] == "description")["text"]
            == "known values"
        )
        assert desc["properties"]["dimensions"][0]["scales"][0]["path"] == "/row_axis"
        assert reader.handle({"op": "describe", "path": "/float_type"})["kind"] == "datatype"
        assert reader.handle({"op": "describe", "path": "/enum"})["properties"]["enum"] == {
            "off": 0,
            "on": 1,
        }
        assert reader.handle({"op": "describe", "path": "/opaque"})["properties"]["opaque"]
    finally:
        reader.close()


def test_slices_nested_fields_and_exact_integers(fixture):
    r = Reader(fixture)
    np.testing.assert_array_equal(
        r.read({"path": "/numbers", "selection": [[2, 6, 2], [3, 8, 2]]})["array"],
        np.arange(120).reshape(10, 12)[2:6:2, 3:8:2],
    )
    np.testing.assert_array_equal(
        r.read({"path": "/records", "field": "nested.x"})["array"], [3, 4, 5]
    )
    assert int(r.read({"path": "/records", "field": "id"})["array"][0]) == 2**63 + 1
    assert int(r.read({"path": "/scalar"})["array"]) == 2**63 + 31
    assert r.read({"path": "/scalar"})["selection"] == []
    with pytest.raises(ValueError, match="NULL"):
        r.read({"path": "/null"})
    with pytest.raises(ValueError, match="positive"):
        r.read({"path": "/numbers", "selection": [[0, 10, -1], 0]})
    r.close()


def test_variable_cells_and_reference_portability(fixture):
    import pickle

    r = Reader(fixture)
    result = r.cell({"path": "/ragged", "selection": [0]})
    np.testing.assert_array_equal(result["array"], np.arange(7))
    page = r.read({"path": "/ragged", "selection": [[0, 2, 1]]})["array"]
    np.testing.assert_array_equal(page[0], np.arange(7))
    np.testing.assert_array_equal(page[1], np.arange(2))
    text = r.read({"path": "/text"})
    assert "truncated" in str(text["array"]) and len(str(text["array"])) <= 65550
    ref = r.read({"path": "/references", "selection": [0]})
    assert pickle.loads(pickle.dumps(ref))["array"].item() == "object -> /numbers"
    assert r.reference({"path": "/references", "selection": [0]})["target"] == "/numbers"
    with pytest.raises(ValueError, match="NULL"):
        r.reference({"path": "/references", "selection": [1]})
    region = r.reference({"path": "/regions", "selection": [0]})
    assert region["selection"] == [[2, 4, 1], [3, 8, 2]]
    r.close()


def test_search_is_bounded_and_does_not_follow_external(fixture):
    r = Reader(fixture)
    result = r.search({"query": "nested.x"})
    assert result["items"] == [{"path": "/records", "kind": "field", "name": "nested.x"}]
    assert r.search({"query": "", "limit": 2})["truncated"]
    assert len(r._files) == 1
    r.close()


def test_virtual_and_external_raw_relative_resolution(tmp_path, monkeypatch):
    sources = tmp_path / "nested"
    sources.mkdir()
    with h5py.File(sources / "source.h5", "w") as f:
        f.create_dataset("values", data=[-9, 8, 7])
    layout = h5py.VirtualLayout((6,), dtype="i8")
    layout[:3] = h5py.VirtualSource("source.h5", "values", shape=(3,))
    layout[3:] = h5py.VirtualSource("source.h5", "missing_dataset", shape=(3,))
    np.arange(4, dtype="<i4").tofile(sources / "payload.raw")
    with h5py.File(sources / "index.h5", "w") as f:
        f.create_virtual_dataset("virtual", layout, fillvalue=-9)
        f.create_dataset("raw", shape=(4,), dtype="<i4", external=[("payload.raw", 0, 16)])
        f["linked"] = h5py.ExternalLink("source.h5", "/values")
    monkeypatch.chdir(tmp_path)
    r = Reader(sources / "index.h5")
    np.testing.assert_array_equal(r.read({"path": "/virtual"})["array"], [-9, 8, 7, -9, -9, -9])
    source = r.sources({"path": "/virtual"})["items"]
    assert source[0]["resolved_file"] == str(sources / "source.h5")
    assert source[0]["status"] == "readable"
    assert source[1]["status"].startswith("unavailable:")
    assert not any("provenance" in key for item in source for key in item)
    np.testing.assert_array_equal(r.read({"path": "/raw"})["array"], np.arange(4))
    assert r.sources({"path": "/raw"})["items"][0]["resolved_file"] == str(sources / "payload.raw")
    np.testing.assert_array_equal(r.read({"path": "/linked"})["array"], [-9, 8, 7])
    assert all(f.mode == "r" for f in r._files.values())
    r.close()


def test_huge_chunk_and_source_work_preflight(tmp_path):
    path = tmp_path / "limits.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset(
            "huge_chunk", (10_000_000,), chunks=(10_000_000,), dtype="f8", compression="gzip"
        )
        f.create_dataset("many_chunks", (100_000_000,), chunks=(1024,), dtype="f8")
        f.create_dataset("large_table", (1000, 1000), dtype="f8")
    r = Reader(path)
    with pytest.raises(ReadLimitError, match="Decoded chunk"):
        r.read({"path": "/huge_chunk", "selection": [0]})
    with pytest.raises(ReadLimitError, match="source-work"):
        r.read({"path": "/many_chunks", "selection": [[0, 100_000_000, 1000]]})
    bounded = r.read({"path": "/large_table"})
    assert bounded["array"].size <= 4096 and bounded["sampled"]
    overview = r.read(
        {"path": "/many_chunks", "purpose": "plot", "selection": [[0, 100_000_000, 1000]]}
    )
    assert overview["sampled"] and overview["selection"][0][2] > 1000
    r.close()


def test_attribute_failures_paging_and_128bit(tmp_path):
    path = tmp_path / "attrs.h5"
    with h5py.File(path, "w") as f:
        for i in range(105):
            f.attrs[f"a{i:03}"] = i
        f.attrs["vlen_multiple"] = ["first", "second"]
        typ = h5py.h5t.STD_U64LE.copy()
        typ.set_size(16)
        typ.set_precision(128)
        aid = h5py.h5a.create(f.id, b"wide", typ, h5py.h5s.create(h5py.h5s.SCALAR))
        value = 2**127 + 123
        aid.write(np.array(value.to_bytes(16, "little"), dtype="V16"), mtype=typ)
    r = Reader(path)
    first = r.describe({})
    assert len(first["attributes"]) == 100 and first["attributes_has_more"]
    second = r.describe({"attributes_offset": 100})
    wide = next(x for x in second["attributes"] if x["name"] == "wide")
    assert wide["text"] == str(value)
    broken = next(x for x in second["attributes"] if x["name"] == "vlen_multiple")
    assert broken["deferred"] and "error" not in broken
    assert not second["attributes_has_more"]
    r.close()


def test_string_pages_and_attribute_reference(fixture):
    with h5py.File(fixture, "r+") as f:
        f.attrs["target"] = f["numbers"].ref
        f.attrs["targets"] = [f["numbers"].ref, f["scalar"].ref]
        f.create_dataset("names", data=["first", "second", "third"], dtype=h5py.string_dtype())
        dtype = np.dtype([("name", h5py.string_dtype()), ("value", "i4")])
        f.create_dataset("named_records", data=np.array([("first", 3), ("second", 4)], dtype=dtype))
    r = Reader(fixture)
    np.testing.assert_array_equal(r.read({"path": "/names"})["array"], ["first", "second", "third"])
    page = r.read({"path": "/named_records"})["array"]
    assert page["name"].tolist() == ["first", "second"]
    assert page["value"].tolist() == [3, 4]
    attr = next(a for a in r.describe({})["attributes"] if a["name"] == "target")
    assert attr["reference"] and attr["references"][0]["target"] == "/numbers"
    assert r.reference({"path": "/", "attribute": "target"})["target"] == "/numbers"
    assert (
        r.reference({"path": "/", "attribute": "targets", "selection": [1]})["target"] == "/scalar"
    )
    r.close()


def test_wide_integer_dataset_big_endian_signed(tmp_path):
    path = tmp_path / "wide.h5"
    expected = [-(2**110), 2**120 + 11, -7]
    with h5py.File(path, "w") as f:
        typ = h5py.h5t.STD_I64BE.copy()
        typ.set_size(16)
        typ.set_precision(128)
        typ.commit(f.id, b"wide_named_type")
        ds = h5py.h5d.create(f.id, b"wide", typ, h5py.h5s.create_simple((3,)))
        values = np.array([x.to_bytes(16, "big", signed=True) for x in expected], dtype="V16")
        ds.write(h5py.h5s.ALL, h5py.h5s.ALL, values, mtype=typ)
    r = Reader(path)
    assert r.describe({"path": "/wide"})["dtype"] == "int128 (big-endian)"
    assert r.describe({"path": "/wide_named_type"})["dtype"] == "int128 (big-endian)"
    assert r.read({"path": "/wide"})["array"].tolist() == expected
    assert r.read({"path": "/wide", "selection": [[0, 3, 2]]})["array"].tolist() == expected[::2]
    r.close()


def test_virtual_tiny_selection_does_not_estimate_entire_large_source(tmp_path):
    with h5py.File(tmp_path / "source.h5", "w") as f:
        f.create_dataset("large", (100_000_000,), dtype="f8", chunks=(1024,), fillvalue=23)
    layout = h5py.VirtualLayout((100_000_000,), dtype="f8")
    layout[:] = h5py.VirtualSource("source.h5", "large", shape=(100_000_000,))
    with h5py.File(tmp_path / "virtual.h5", "w") as f:
        f.create_virtual_dataset("values", layout)
    r = Reader(tmp_path / "virtual.h5")
    np.testing.assert_array_equal(
        r.read({"path": "/values", "selection": [[500, 510, 1]]})["array"], np.full(10, 23)
    )
    r.close()


def test_virtual_hdf5_fallbacks_match_reported_source(tmp_path, monkeypatch):
    elsewhere = tmp_path / "working"
    elsewhere.mkdir()
    with h5py.File(tmp_path / "relocated.h5", "w") as f:
        f["x"] = [15]
    with h5py.File(elsewhere / "cwd_only.h5", "w") as f:
        f["x"] = [27]
    monkeypatch.chdir(elsewhere)
    cases = [
        ("/old/missing/relocated.h5", tmp_path / "relocated.h5", 15),
        ("cwd_only.h5", elsewhere / "cwd_only.h5", 27),
    ]
    if os.name == "nt":
        cases.extend(
            [
                (r"\old\missing\relocated.h5", tmp_path / "relocated.h5", 15),
                (tmp_path.drive + r"\old\missing\relocated.h5", tmp_path / "relocated.h5", 15),
            ]
        )
    for recorded, resolved, expected in cases:
        layout = h5py.VirtualLayout((1,), dtype="i8")
        layout[:] = h5py.VirtualSource(recorded, "x", shape=(1,))
        with h5py.File(tmp_path / "index.h5", "w") as f:
            f.create_virtual_dataset("v", layout, fillvalue=-1)
        r = Reader(tmp_path / "index.h5")
        assert r.sources({"path": "/v"})["items"][0]["resolved_file"] == str(resolved)
        assert r.read({"path": "/v"})["array"].item() == expected
        r.close()


def test_missing_soft_sources_and_partial_virtual_extent(fixture, tmp_path):
    r = Reader(fixture)
    broken = r.sources({"path": "/broken"})["items"][0]
    assert broken["dataset"] == "/absent" and broken["status"].startswith("unavailable:")
    assert any(x["name"] == "ancestor" for x in r.search({"query": "ancestor"})["items"])
    r.close()
    with h5py.File(tmp_path / "short.h5", "w") as f:
        f["x"] = [1, 2]
    layout = h5py.VirtualLayout((5,), dtype="i8")
    layout[:] = h5py.VirtualSource("short.h5", "x", shape=(5,))
    with h5py.File(tmp_path / "partial.h5", "w") as f:
        f.create_virtual_dataset("v", layout, fillvalue=-9)
    r = Reader(tmp_path / "partial.h5")
    source = r.sources({"path": "/v"})["items"][0]
    assert source["shape"] == [2] and not source["requested_extent_available"]
    assert "partial extent" in source["status"]
    r.close()


def test_explicit_variable_attribute_load_and_nested_references(tmp_path, monkeypatch):
    import pickle

    path = tmp_path / "attributes.h5"
    with h5py.File(path, "w") as f:
        f.attrs["strings"] = ["first", "second"]
        ds = f.create_dataset("values", (2, 3), dtype="f4")
        scale = f.create_dataset("rows", data=[1, 2])
        scale.make_scale("rows")
        ds.dims[0].attach_scale(scale)
    r = Reader(path)
    original_getitem = h5py.AttributeManager.__getitem__

    def guarded_getitem(self, name):
        if name == "strings":
            raise AssertionError("Describe must defer multi-cell VLEN reads")
        return original_getitem(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(h5py.AttributeManager, "__getitem__", guarded_getitem)
        meta = r.describe({})["attributes"][0]
        assert meta["deferred"] and meta["shape"] == [2]
        assert "error" not in meta
    loaded = r.handle({"op": "attribute", "path": "/", "attribute": "strings"})
    assert loaded["value"].tolist() == ["first", "second"]
    assert loaded["text"] == "[first, second]"
    assert "error" not in loaded
    dimensions = r.handle({"op": "attribute", "path": "/values", "attribute": "DIMENSION_LIST"})
    assert dimensions["references"][0]["selection"] == [0, 0]
    assert dimensions["references"][0]["target"] == "/rows"
    assert pickle.loads(pickle.dumps(dimensions))["value"][0][0] == "object -> /rows"
    target = r.reference({"path": "/values", "attribute": "DIMENSION_LIST", "selection": [0, 0]})
    assert target["target"] == "/rows"
    r.close()


def test_source_mapping_limit_is_explicit(tmp_path):
    path = tmp_path / "many_mappings.h5"
    layout = h5py.VirtualLayout((1001,), dtype="i4")
    for index in range(1001):
        layout[index : index + 1] = h5py.VirtualSource("source.h5", "values", shape=(1,))
    with h5py.File(path, "w") as f:
        f.create_virtual_dataset("values", layout)
    r = Reader(path)
    response = r.sources({"path": "/values"})
    assert response["truncated"] and response["total"] == 1001
    assert response["limit"] == 1000 and "1000" in response["notice"]
    with pytest.raises(ReadLimitError, match="1000 virtual mappings"):
        r.read({"path": "/values", "selection": [0]})
    r.close()


def test_image_element_cap_and_exact_probe(tmp_path):
    from h5_viewer.reader import MAX_IMAGE_ELEMENTS

    path = tmp_path / "large_image.h5"
    with h5py.File(path, "w") as f:
        f.create_dataset("pixels", shape=(4096, 4096), dtype="u1", fillvalue=19)
    r = Reader(path)
    selection = [[0, 4096, 1], [0, 4096, 1]]
    overview = r.read({"path": "/pixels", "selection": selection, "purpose": "image"})
    assert overview["array"].size <= MAX_IMAGE_ELEMENTS
    assert overview["sampled"] and overview["selection"] == [[0, 4096, 2], [0, 4096, 2]]
    assert all(np.all(row == 19) for row in overview["array"])
    assert any("2048 x 2048" in note for note in overview["notes"])
    exact = r.read({"path": "/pixels", "selection": selection, "purpose": "probe"})
    assert exact["array"].shape == (4096, 4096) and not exact["sampled"]
    assert exact["selection"] == selection
    r.close()


def test_bitfield_metadata_survives_scalar_compound_and_attribute_reads(tmp_path):
    import pickle

    path = tmp_path / "bitfields.h5"
    with h5py.File(path, "w") as f:
        bit_type = h5py.h5t.STD_B16LE.copy()
        values = np.array([0x10, 0xFF], dtype="<u2")
        ds = h5py.h5d.create(f.id, b"flags", bit_type, h5py.h5s.create_simple((2,)))
        ds.write(h5py.h5s.ALL, h5py.h5s.ALL, values, mtype=bit_type)
        compound_type = h5py.h5t.create(h5py.h5t.COMPOUND, 4)
        compound_type.insert(b"flags", 0, bit_type)
        compound_type.insert(b"count", 2, h5py.h5t.STD_U16LE)
        compound = h5py.h5d.create(f.id, b"records", compound_type, h5py.h5s.create_simple((2,)))
        records = np.array([(0x10, 3), (0xFF, 4)], dtype=[("flags", "<u2"), ("count", "<u2")])
        compound.write(h5py.h5s.ALL, h5py.h5s.ALL, records, mtype=compound_type)
        attr = h5py.h5a.create(f.id, b"flags", bit_type, h5py.h5s.create_simple((2,)))
        attr.write(values, mtype=bit_type)
    r = Reader(path)
    scalar = r.read({"path": "/flags"})["array"]
    assert scalar.tolist() == [0x10, 0xFF]
    assert pickle.loads(pickle.dumps(scalar)).dtype.metadata["hdf5_class"] == "bitfield"
    records = r.read({"path": "/records"})["array"]
    assert records.dtype.fields["flags"][0].metadata["hdf5_class"] == "bitfield"
    selected = r.read({"path": "/records", "field": "flags"})["array"]
    assert selected.tolist() == [0x10, 0xFF]
    assert selected.dtype.metadata["hdf5_class"] == "bitfield"
    attr = r.attribute({"attribute": "flags"})["value"]
    assert attr.tolist() == [0x10, 0xFF] and attr.dtype.metadata["hdf5_class"] == "bitfield"
    r.close()


def test_big_endian_bitfield_scalar_values_are_not_reinterpreted(tmp_path):
    path = tmp_path / "big_endian_flags.h5"
    bit_type = h5py.h5t.STD_B16BE.copy()
    values = np.array([0x1234, 0x5678], dtype=">u2")
    with h5py.File(path, "w") as f:
        ds = h5py.h5d.create(f.id, b"flags", bit_type, h5py.h5s.create_simple((2,)))
        ds.write(h5py.h5s.ALL, h5py.h5s.ALL, values, mtype=bit_type)
        compound_type = h5py.h5t.create(h5py.h5t.COMPOUND, 2)
        compound_type.insert(b"flags", 0, bit_type)
        compound = h5py.h5d.create(f.id, b"records", compound_type, h5py.h5s.create_simple((2,)))
        records = np.array([(0x1234,), (0x5678,)], dtype=[("flags", ">u2")])
        compound.write(h5py.h5s.ALL, h5py.h5s.ALL, records, mtype=compound_type)
        attr = h5py.h5a.create(f.id, b"flag", bit_type, h5py.h5s.create(h5py.h5s.SCALAR))
        attr.write(np.array(0x1234, dtype=">u2"), mtype=bit_type)
    r = Reader(path)
    assert r.read({"path": "/flags"})["array"].tolist() == [0x1234, 0x5678]
    for index, expected in enumerate([0x1234, 0x5678]):
        scalar = r.read({"path": "/flags", "selection": [index]})["array"]
        assert scalar.item() == expected
        assert scalar.dtype.metadata["hdf5_class"] == "bitfield"
        field = r.read({"path": "/records", "selection": [index], "field": "flags"})["array"]
        assert field.item() == expected
        assert field.dtype.metadata["hdf5_class"] == "bitfield"
    attribute = r.attribute({"attribute": "flag"})["value"]
    assert attribute.item() == 0x1234
    assert attribute.dtype.metadata["hdf5_class"] == "bitfield"
    r.close()
