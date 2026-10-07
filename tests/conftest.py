"""Synthetic HDF5 fixtures only; never copy user datasets into tests."""

import h5py
import numpy as np
import pytest


@pytest.fixture
def sample_h5(tmp_path):
    path = tmp_path / "sample.h5"
    source = tmp_path / "source.h5"
    with h5py.File(source, "w") as f:
        f["values"] = np.array([-9, 2, 3, 4], dtype=np.int64)
    with h5py.File(path, "w", libver="latest") as f:
        f.attrs["description"] = "Generic viewer fixture"
        f.attrs["revision"] = 3
        f["numbers"] = np.arange(20, dtype=np.float64)
        f["matrix"] = np.arange(60, dtype=np.float32).reshape(10, 6)
        f["stack"] = np.arange(120, dtype=np.float32).reshape(3, 2, 4, 5)
        f["complex"] = (np.arange(48).reshape(2, 6, 4) * (1 + 2j)).astype(np.complex64)
        f["scalar"] = np.int64(2**62 + 1)
        f["empty"] = np.empty((0, 3), dtype=np.float32)
        f.create_dataset("null", dtype="f8")
        f["nonfinite"] = np.array([np.nan, np.inf, -np.inf, 3, 3])
        f.create_dataset(
            "text",
            data=["alpha", "beta", '{"source": "synthetic", "valid": true}'],
            dtype=h5py.string_dtype(),
        )
        f.create_dataset(
            "enum", data=np.array([0, 1, 0]), dtype=h5py.enum_dtype({"OFF": 0, "ON": 1}, "u1")
        )
        records = np.zeros(
            4,
            dtype=[("id", "i8"), ("position", [("x", "f8"), ("y", "f8")]), ("samples", "f4", (3,))],
        )
        records["id"] = [2**62 + 1, 2, 3, 4]
        records["position"]["x"] = [1.25, 2.5, 3.75, 5]
        records["samples"] = np.arange(12).reshape(4, 3)
        f["records"] = records
        ragged = f.create_dataset("ragged", (2,), dtype=h5py.vlen_dtype(np.dtype("int32")))
        ragged[0], ragged[1] = [1, 2, 3], [7]
        group = f.create_group("group")
        group.attrs["units"] = "arbitrary"
        group["values"] = np.arange(5)
        group["cycle"] = f["/"]
        f["alias"] = f["matrix"]
        f["soft"] = h5py.SoftLink("/numbers")
        f["broken"] = h5py.SoftLink("/absent")
        f["external"] = h5py.ExternalLink("source.h5", "/values")
        f["missing_external"] = h5py.ExternalLink("absent.h5", "/values")
        refs = f.create_dataset("refs", (2,), dtype=h5py.ref_dtype)
        refs[0], refs[1] = f["records"].ref, f["group"].ref
        regions = f.create_dataset("region_refs", (1,), dtype=h5py.regionref_dtype)
        regions[0] = f["matrix"].regionref[1:3, 2:5]
        scale = f.create_dataset("row_coordinate", data=np.arange(10) * 0.5)
        scale.make_scale("distance")
        scale.attrs["units"] = "m"
        f["matrix"].dims[0].attach_scale(scale)
        f["matrix"].dims[0].label = "distance"
        for compression in ("gzip", "lzf"):
            f.create_dataset(
                compression, data=np.arange(100), chunks=(20,), compression=compression
            )
        layout = h5py.VirtualLayout((8,), dtype="i8")
        layout[:4] = h5py.VirtualSource("source.h5", "values", (4,))
        layout[4:] = h5py.VirtualSource("source.h5", "missing_dataset", (4,))
        f.create_virtual_dataset("virtual", layout, fillvalue=-9)
    return path
