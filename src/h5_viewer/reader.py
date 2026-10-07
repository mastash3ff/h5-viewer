"""Read-only, bounded HDF5 inspection. No h5py handles leave this module."""

from __future__ import annotations

import itertools
import math
import os
import posixpath
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np

MAX_RESULT = 64 * 1024**2
MAX_IMAGE_ELEMENTS = 2048 * 2048
MAX_CHUNK = 64 * 1024**2
MAX_SOURCE_WORK = 256 * 1024**2
MAX_TEXT = 64 * 1024
MAX_TABLE_CELLS = 100_000
DEFAULT_CELLS = 4096
MAX_ITEMS = 1000
MAX_FILES = 16


class ReadLimitError(ValueError):
    """The requested operation exceeds an interactive reader limit."""


def _decode(value):
    return os.fsdecode(value) if isinstance(value, bytes) else str(value)


def _clip(value, limit=MAX_TEXT):
    text = str(value)
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > limit:
        return raw[: limit - 32].decode("utf-8", errors="ignore") + " ... [truncated]"
    return text


def _product(shape):
    return math.prod(shape)


def _has_vlen(dtype):
    if h5py.check_vlen_dtype(dtype) is not None:
        return True
    if dtype.subdtype:
        return _has_vlen(dtype.subdtype[0])
    return bool(dtype.names and any(_has_vlen(dtype.fields[name][0]) for name in dtype.names))


def _has_reference(dtype):
    if h5py.check_ref_dtype(dtype) is not None:
        return True
    vlen = h5py.check_vlen_dtype(dtype)
    if isinstance(vlen, np.dtype) and _has_reference(vlen):
        return True
    return bool(dtype.names and any(_has_reference(dtype.fields[name][0]) for name in dtype.names))


def _display_dtype(dtype, typ):
    """Retain HDF5 bitfield identity without changing raw numeric values."""
    if typ.get_class() == h5py.h5t.BITFIELD:
        return np.dtype(dtype, metadata={**(dtype.metadata or {}), "hdf5_class": "bitfield"})
    if dtype.names and typ.get_class() == h5py.h5t.COMPOUND:
        formats = [
            _display_dtype(dtype.fields[name][0], typ.get_member_type(index))
            for index, name in enumerate(dtype.names)
        ]
        if any(child is not dtype.fields[name][0] for name, child in zip(dtype.names, formats)):
            return np.dtype(
                {
                    "names": list(dtype.names),
                    "formats": formats,
                    "offsets": [dtype.fields[name][1] for name in dtype.names],
                    "itemsize": dtype.itemsize,
                },
                align=dtype.isalignedstruct,
                metadata=dict(dtype.metadata or {}),
            )
    if dtype.subdtype and typ.get_class() == h5py.h5t.ARRAY:
        base, shape = dtype.subdtype
        child = _display_dtype(base, typ.get_super())
        if child is not base:
            return np.dtype((child, shape))
    return dtype


def _has_bitfield(dtype):
    if (dtype.metadata or {}).get("hdf5_class") == "bitfield":
        return True
    if dtype.subdtype:
        return _has_bitfield(dtype.subdtype[0])
    return bool(dtype.names and any(_has_bitfield(dtype.fields[name][0]) for name in dtype.names))


def _selected_dtype(dtype, field):
    if field:
        for part in field.split("."):
            dtype = dtype.fields[part][0]
    return dtype


def _dtype(obj):
    try:
        typ = obj.id if isinstance(obj, h5py.Datatype) else obj.id.get_type()
        return _display_dtype(obj.dtype, typ)
    except (TypeError, ValueError):
        typ = obj.id if isinstance(obj, h5py.Datatype) else obj.id.get_type()
        if typ.get_class() == h5py.h5t.INTEGER:
            return np.dtype(f"V{typ.get_size()}", metadata={"hdf_integer": True})
        raise


def _wide_values(raw, typ):
    endian = "big" if typ.get_order() == h5py.h5t.ORDER_BE else "little"
    signed = typ.get_sign() != h5py.h5t.SGN_NONE
    precision, offset = typ.get_precision(), typ.get_offset()
    values = np.empty(raw.shape, dtype=object)
    for index in np.ndindex(raw.shape):
        value = (int.from_bytes(raw[index].tobytes(), endian) >> offset) & ((1 << precision) - 1)
        if signed and value & (1 << (precision - 1)):
            value -= 1 << precision
        values[index] = value
    return values


def _fields(dtype, prefix=""):
    result = []
    if dtype.names:
        for name in dtype.names[:MAX_ITEMS]:
            child = dtype.fields[name][0]
            label = prefix + name
            result.append({"name": label, "dtype": str(child)})
            result.extend(_fields(child, label + "."))
    return result


def _field(value, name):
    if name:
        for part in name.split("."):
            value = value[part]
    return value


def _space(space):
    """Bounded dataspace metadata, including non-rectangular region selections."""
    result = {
        "shape": list(space.get_simple_extent_dims()),
        "points": int(space.get_select_npoints()),
    }
    kind = space.get_select_type()
    result["selection_type"] = {
        h5py.h5s.SEL_ALL: "all",
        h5py.h5s.SEL_NONE: "none",
        h5py.h5s.SEL_POINTS: "points",
        h5py.h5s.SEL_HYPERSLABS: "hyperslabs",
    }.get(kind, str(kind))
    if kind == h5py.h5s.SEL_HYPERSLABS and space.is_regular_hyperslab():
        start, stride, count, block = space.get_regular_hyperslab()
        result.update(start=list(start), stride=list(stride), count=list(count), block=list(block))
    if result["points"]:
        try:
            result["bounds"] = [list(x) for x in space.get_select_bounds()]
        except ValueError:
            pass
    return result


class Reader:
    def __init__(self, filename):
        self.filename = str(Path(filename).expanduser().resolve())
        self._files = {}
        for variable in ("HDF5_VDS_PREFIX", "HDF5_EXT_PREFIX"):
            if os.environ.get(variable):
                raise ValueError(
                    f"{variable} overrides HDF5 source resolution; launch without this override to use file-relative sources"
                )
        self.file = self._open_file(self.filename)

    def _open_file(self, filename):
        filename = str(Path(filename).resolve())
        if filename not in self._files:
            if len(self._files) >= MAX_FILES:
                raise ReadLimitError(
                    "Too many source files open; reopen the reader to inspect another source."
                )
            # Disable raw chunk caches, including the defaults inherited by source opens.
            file = h5py.File(filename, "r", rdcc_nbytes=0, rdcc_nslots=1)
            config = file.id.get_mdc_config()
            config.max_size = 4 * 1024**2
            config.initial_size = 1024**2
            config.min_size = 1024**2
            file.id.set_mdc_config(config)
            self._files[filename] = file
        return self._files[filename]

    def close(self):
        for file in self._files.values():
            file.close()
        self._files.clear()

    def handle(self, request):
        op = request.get("op", request.get("operation", "describe"))
        methods = {
            "describe": self.describe,
            "children": self.children,
            "read": self.read,
            "cell": self.cell,
            "search": self.search,
            "sources": self.sources,
            "reference": self.reference,
            "attribute": self.attribute,
        }
        if op not in methods:
            raise ValueError(f"Unknown reader operation: {op}")
        return methods[op](request)

    @staticmethod
    def _resolve(file, recorded):
        if recorded == ".":
            return str(Path(file.filename).resolve())
        path = Path(recorded)
        return str((Path(file.filename).resolve().parent / path).resolve())

    @staticmethod
    def _resolve_virtual(file, recorded):
        if recorded == ".":
            return str(Path(file.filename).resolve())
        path = Path(recorded)
        directory = Path(file.filename).resolve().parent
        # HDF5 tries absolute names first, then the basename under the VDS
        # prefix/location. Relative names retain their components; after the
        # prefix/location, HDF5 also tries the process working directory.
        # Windows rooted paths without a drive (e.g. /old/file.h5) are
        # absolute to HDF5 even though pathlib.is_absolute() returns False.
        candidates = (
            [path, directory / path.name, Path.cwd() / path.name]
            if path.root
            else [directory / path, Path.cwd() / path]
        )
        for candidate in candidates:
            try:
                if h5py.is_hdf5(candidate):
                    return str(candidate.resolve())
            except OSError:
                continue
        return str(candidates[0].resolve())

    def _object(self, path="/", file=None, seen=None, configure=False):
        """Resolve links explicitly, with finite recursion and read-only source opens."""
        file = file if file is not None else self.file
        path = "/" + path.strip("/")
        seen = set() if seen is None else seen
        key = (file.filename, path)
        if key in seen or len(seen) >= 64:
            raise ValueError("Link cycle or excessive link depth")
        seen.add(key)
        parts = [p for p in path.split("/") if p]
        group = file["/"]
        for index, name in enumerate(parts):
            link = group.get(name, getlink=True)
            remaining = parts[index + 1 :]
            if isinstance(link, h5py.ExternalLink):
                external = self._open_file(self._resolve(file, link.filename))
                target = "/".join([link.path.rstrip("/"), *remaining])
                return self._object(target, external, seen, configure)
            if isinstance(link, h5py.SoftLink):
                target = (
                    link.path
                    if link.path.startswith("/")
                    else group.name.rstrip("/") + "/" + link.path
                )
                return self._object(
                    "/".join([target.rstrip("/"), *remaining]), file, seen, configure
                )
            if link is None:
                raise KeyError(f"Object does not exist: {path}")
            object_type = h5py.h5o.get_info(group.id, name=os.fsencode(name)).type
            if object_type == h5py.h5o.TYPE_DATASET:
                if remaining:
                    raise KeyError("Cannot traverse into a dataset")
                dapl = h5py.h5p.create(h5py.h5p.DATASET_ACCESS)
                prefix = os.fsencode(str(Path(file.filename).resolve().parent) + os.sep)
                if configure:
                    dapl.set_virtual_prefix(prefix)
                    dapl.set_efile_prefix(prefix)
                dapl.set_chunk_cache(1, 0, 0.0)
                return h5py.Dataset(h5py.h5d.open(group.id, os.fsencode(name), dapl=dapl))
            group = group[name]
        return group

    @staticmethod
    def _kind(obj):
        if isinstance(obj, h5py.Dataset):
            return "dataset"
        if isinstance(obj, h5py.Datatype):
            return "datatype"
        return "group"

    @staticmethod
    def _identity(obj):
        return (obj.file.filename, h5py.h5o.get_info(obj.id).addr)

    def _text(self, value, file=None):
        if isinstance(value, h5py.Empty):
            return "NULL dataspace"
        if isinstance(value, h5py.Reference):
            if not value:
                return "NULL reference"
            try:
                target = _decode(
                    h5py.h5r.get_name(value, (file if file is not None else self.file).id)
                )
                return (
                    "region -> " if isinstance(value, h5py.RegionReference) else "object -> "
                ) + target
            except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
                return f"Unreadable reference: {exc}"
        if isinstance(value, bytes):
            return _clip(value.decode("utf-8", errors="backslashreplace"))
        if isinstance(value, np.void) and not value.dtype.names:
            return _clip("0x" + value.tobytes().hex())
        if isinstance(value, np.ndarray):
            if value.size == 1:
                return self._text(value.reshape(-1)[0], file)
            parts = [self._text(x, file) for x in itertools.islice(value.flat, 32)]
            return _clip(
                "[" + ", ".join(parts) + (", ... [truncated]" if value.size > 32 else "") + "]"
            )
        return _clip(value)

    @staticmethod
    def _attribute_names(obj, offset=0, limit=100):
        names = []
        if offset >= len(obj.attrs):
            return names

        def collect(name):
            names.append(_decode(name))
            return True if len(names) >= limit else None

        h5py.h5a.iterate(obj.id, collect, index=offset)
        return names

    def _attribute_references(self, value, file):
        references = []

        def collect(value, selection=(), field=""):
            if len(references) >= 100:
                return
            if isinstance(value, h5py.Reference):
                if value:
                    item = {
                        "selection": list(selection),
                        "target": _decode(h5py.h5r.get_name(value, file.id)),
                        "filename": str(Path(file.filename).resolve()),
                        "region": isinstance(value, h5py.RegionReference),
                    }
                    if field:
                        item["field"] = field
                    references.append(item)
                return
            array = np.asarray(value)
            if array.dtype.names:
                for name in array.dtype.names:
                    collect(array[name], selection, field + "." + name if field else name)
            elif array.dtype.hasobject:
                for index in np.ndindex(array.shape):
                    collect(array[index], selection + index, field)
                    if len(references) >= 100:
                        break

        collect(value)
        return references

    def attribute(self, request):
        name = request.get("attribute", request.get("name"))
        if name is None:
            raise ValueError("An attribute name is required")
        return self._attribute(self._object(request.get("path", "/")), name, explicit=True)

    def _attribute(self, obj, name, explicit=False):
        result = {"name": name}
        try:
            aid = obj.attrs.get_id(name)
            typ = aid.get_type()
            shape = aid.shape
            result["shape"] = None if shape is None else list(shape)
            size = typ.get_size() * (_product(shape) if shape else 1)
            if size > MAX_RESULT:
                raise ReadLimitError("Attribute exceeds 64 MiB decoded value budget")
            try:
                dtype = aid.dtype
                result["dtype"] = str(dtype)
                result["reference"] = _has_reference(dtype)
            except (TypeError, ValueError):
                dtype = None
                result["dtype"] = f"HDF5 type class {typ.get_class()}, {typ.get_size() * 8} bits"
            if typ.get_class() == h5py.h5t.INTEGER and typ.get_size() > 8:
                if shape is None:
                    result["text"] = "NULL dataspace"
                    return result
                raw = np.empty(shape or (), dtype=f"V{typ.get_size()}")
                aid.read(raw, mtype=typ)
                endian = "big" if typ.get_order() == h5py.h5t.ORDER_BE else "little"
                signed = typ.get_sign() != h5py.h5t.SGN_NONE
                precision, offset = typ.get_precision(), typ.get_offset()
                values = []
                for item in itertools.islice(raw.flat, 32):
                    integer = (int.from_bytes(item.tobytes(), endian) >> offset) & (
                        (1 << precision) - 1
                    )
                    if signed and integer & (1 << (precision - 1)):
                        integer -= 1 << precision
                    values.append(str(integer))
                result["dtype"] = f"{'int' if signed else 'uint'}{precision} ({endian}-endian)"
                result["text"] = _clip(
                    ", ".join(values) + (" ... [truncated]" if raw.size > 32 else "")
                )
                if explicit:
                    result["value"] = _wide_values(raw, typ)
                    if self._payload_size(result["value"]) > MAX_RESULT:
                        del result["value"]
                        raise ReadLimitError("Expanded integer attribute exceeds 64 MiB")
            else:
                # H5Aread has no hyperslab support. Large unknown VLEN payloads
                # therefore require an explicit action within the bounded worker.
                if (
                    not explicit
                    and dtype is not None
                    and _has_vlen(dtype)
                    and shape
                    and _product(shape) > 1
                ):
                    result["deferred"] = True
                    result["text"] = "Variable-length attribute; load explicitly to inspect values"
                    return result
                value = obj.attrs[name]
                if not isinstance(value, h5py.Empty) and self._payload_size(value) > MAX_RESULT:
                    raise ReadLimitError("Expanded attribute exceeds 64 MiB")
                result["text"] = self._text(value, obj.file)
                if result.get("reference"):
                    result["references"] = self._attribute_references(value, obj.file)
                if explicit and not isinstance(value, h5py.Empty):
                    notes = []
                    if isinstance(value, (str, bytes)) and dtype is not None and _has_vlen(dtype):
                        value = np.asarray(value, dtype=object)
                    result["value"] = self._portable(value, obj.file, notes)
                    display_dtype = _display_dtype(dtype, typ)
                    if (
                        display_dtype is not dtype
                        and display_dtype.itemsize == result["value"].dtype.itemsize
                    ):
                        # Scalar extraction may normalize endianness. Convert numeric
                        # representation before attaching the file dtype metadata.
                        result["value"] = (
                            result["value"].astype(display_dtype, copy=False).view(display_dtype)
                        )
                    if self._payload_size(result["value"]) > MAX_RESULT:
                        del result["value"]
                        raise ReadLimitError("Expanded attribute result exceeds 64 MiB")
                    result["notes"] = list(dict.fromkeys(notes))
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
            result["error"] = str(exc)
            result.setdefault("dtype", "unavailable")
            result.setdefault("shape", None)
            result["text"] = f"Error: {exc}"
        return result

    def describe(self, request):
        path = request.get("path", "/")
        obj = self._object(path)
        offset = max(0, int(request.get("attributes_offset", 0)))
        limit = max(1, min(100, int(request.get("attributes_limit", 100))))
        attrs = [self._attribute(obj, name) for name in self._attribute_names(obj, offset, limit)]
        result = {
            "path": path,
            "kind": self._kind(obj),
            "properties": {},
            "attributes": attrs,
            "attributes_total": len(obj.attrs),
            "attributes_offset": offset,
            "attributes_has_more": offset + len(attrs) < len(obj.attrs),
        }
        props = result["properties"]
        props["filename"] = str(Path(obj.file.filename).resolve())
        props["hard_link_count"] = h5py.h5o.get_info(obj.id).rc
        if isinstance(obj, (h5py.Dataset, h5py.Datatype)):
            try:
                result["dtype"] = str(_dtype(obj))
                result["fields"] = _fields(_dtype(obj))
                if (_dtype(obj).metadata or {}).get("hdf_integer"):
                    typ = obj.id if isinstance(obj, h5py.Datatype) else obj.id.get_type()
                    sign = "uint" if typ.get_sign() == h5py.h5t.SGN_NONE else "int"
                    endian = "big" if typ.get_order() == h5py.h5t.ORDER_BE else "little"
                    result["dtype"] = f"{sign}{typ.get_precision()} ({endian}-endian)"
                enum = h5py.check_enum_dtype(_dtype(obj))
                if enum:
                    props["enum"] = enum
                if h5py.check_opaque_dtype(_dtype(obj)):
                    props["opaque"] = True
            except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
                result["dtype"] = "Unsupported native datatype"
                result["error"] = str(exc)
        if isinstance(obj, h5py.Dataset):
            result["shape"] = None if obj.shape is None else list(obj.shape)
            for name in (
                "chunks",
                "compression",
                "compression_opts",
                "shuffle",
                "fletcher32",
                "scaleoffset",
                "maxshape",
                "is_virtual",
            ):
                try:
                    props[name] = getattr(obj, name)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    RuntimeError,
                    KeyError,
                    OverflowError,
                ) as exc:
                    props[name] = f"Error: {exc}"
            props["storage_bytes"] = obj.id.get_storage_size()
            try:
                props["fillvalue"] = self._text(obj.fillvalue, obj.file)
            except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
                props["fillvalue"] = f"Error: {exc}"
            dimensions = []
            for axis, dim in enumerate(obj.dims):
                entry = {"axis": axis, "label": dim.label, "scales": []}
                try:
                    for _, scale in itertools.islice(dim.items(), 100):
                        entry["scales"].append(
                            {
                                "path": scale.name,
                                "filename": str(Path(scale.file.filename).resolve()),
                            }
                        )
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    RuntimeError,
                    KeyError,
                    OverflowError,
                ) as exc:
                    entry["error"] = str(exc)
                dimensions.append(entry)
            props["dimensions"] = dimensions
        elif isinstance(obj, h5py.Group):
            props["members"] = len(obj)
        return result

    def children(self, request):
        path = request.get("path", "/")
        group = self._object(path)
        if not isinstance(group, h5py.Group):
            raise TypeError("Only groups have children")
        offset = max(0, int(request.get("offset", 0)))
        limit = max(1, min(MAX_ITEMS, int(request.get("limit", 200))))
        ancestors = set()
        prefix = "/"
        for part in [""] + path.strip("/").split("/"):
            prefix = prefix.rstrip("/") + "/" + part
            try:
                ancestors.add(self._identity(self._object(prefix)))
            except (OSError, ValueError, TypeError, RuntimeError, KeyError):
                continue
        items = []
        for name in itertools.islice(group, offset, offset + limit):
            child_path = path.rstrip("/") + "/" + name
            item = {"name": name, "path": child_path}
            try:
                link = group.get(name, getlink=True)
                if isinstance(link, h5py.ExternalLink):
                    item.update(
                        kind="external_link",
                        link_kind="external",
                        target=link.path,
                        recorded_file=link.filename,
                        resolved_file=self._resolve(group.file, link.filename),
                        expandable=False,
                    )
                elif isinstance(link, h5py.SoftLink):
                    item.update(
                        kind="soft_link", link_kind="soft", target=link.path, expandable=False
                    )
                    target_path = posixpath.normpath(
                        link.path if link.path.startswith("/") else posixpath.join(path, link.path)
                    )
                    item["cycle"] = target_path == path or path.startswith(
                        target_path.rstrip("/") + "/"
                    )
                    item["resolution"] = "Deferred until explicit navigation"
                else:
                    obj = self._object(child_path)
                    item.update(kind=self._kind(obj), link_kind="hard")
                    item["hard_link_count"] = h5py.h5o.get_info(obj.id).rc
                    item["alias"] = item["hard_link_count"] > 1
                    cycle = self._identity(obj) in ancestors
                    item["cycle"] = cycle
                    item["expandable"] = isinstance(obj, h5py.Group) and not cycle
                    if cycle:
                        item["target"] = "ancestor (cycle)"
                    if isinstance(obj, h5py.Dataset):
                        item["shape"] = None if obj.shape is None else list(obj.shape)
                    if isinstance(obj, (h5py.Dataset, h5py.Datatype)):
                        item["dtype"] = str(_dtype(obj))
            except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
                item.setdefault("kind", "unreadable")
                item["error"] = str(exc)
            items.append(item)
        return {
            "items": items,
            "total": len(group),
            "offset": offset,
            "has_more": offset + len(items) < len(group),
        }

    def _selection(self, dataset, encoded, purpose):
        if dataset.shape is None:
            raise ValueError("Dataset has a NULL dataspace; there are no values")
        shape = dataset.shape
        if encoded is None:
            selection = [slice(0, n, 1) for n in shape]
            # VLEN previews have an unpredictable decoded size: one selected cell.
            limit = DEFAULT_CELLS
            for axis in range(len(shape)):
                other = _product(
                    len(range(*s.indices(shape[i]))) for i, s in enumerate(selection) if i != axis
                )
                max_count = max(1, limit // max(1, other))
                n = shape[axis]
                if purpose in ("image", "plot") and not _has_vlen(_dtype(dataset)):
                    selection[axis] = slice(0, n, max(1, math.ceil(n / max_count)))
                else:
                    selection[axis] = slice(0, min(n, max_count), 1)
        else:
            if len(encoded) != len(shape):
                raise ValueError("Selection must contain one index or slice per dimension")
            selection = []
            for n, part in zip(shape, encoded):
                if isinstance(part, (int, np.integer)):
                    index = int(part)
                    if index < 0:
                        index += n
                    if not 0 <= index < n:
                        raise IndexError("Selection index outside dataset extent")
                    selection.append(index)
                elif isinstance(part, (list, tuple)) and len(part) == 3:
                    step = 1 if part[2] is None else int(part[2])
                    if step <= 0:
                        raise ValueError("HDF5 selection steps must be positive")
                    start, stop, step = slice(*part).indices(n)
                    selection.append(slice(start, stop, step))
                else:
                    raise ValueError("Selection entries must be integers or [start,stop,step]")
        return selection

    @staticmethod
    def _count(shape, selection):
        return _product(
            len(range(*part.indices(n))) if isinstance(part, slice) else 1
            for n, part in zip(shape, selection)
        )

    @staticmethod
    def _mappings(dataset):
        plist = dataset.id.get_create_plist()
        count = plist.get_virtual_count()
        if count > MAX_ITEMS:
            raise ReadLimitError(
                "More than 1000 virtual mappings; select an individual source file"
            )
        for index in range(count):
            yield SimpleNamespace(
                file_name=plist.get_virtual_filename(index),
                dset_name=plist.get_virtual_dsetname(index),
                vspace=plist.get_virtual_vspace(index),
                src_space=plist.get_virtual_srcspace(index),
            )

    @staticmethod
    def _rectangle(space, actual_shape):
        if space.get_select_type() == h5py.h5s.SEL_ALL:
            return [0] * len(actual_shape), list(actual_shape)
        if space.get_select_type() == h5py.h5s.SEL_HYPERSLABS and space.is_regular_hyperslab():
            start, stride, count, block = space.get_regular_hyperslab()
            if all(c == 1 or s == b for s, c, b in zip(stride, count, block)):
                return list(start), [c * b for c, b in zip(count, block)]
        return None

    def _mapped_selection(self, mapping, source, virtual_shape, selection):
        virtual = self._rectangle(mapping.vspace, virtual_shape)
        original = self._rectangle(mapping.src_space, source.shape)
        if virtual is None or original is None or virtual[1] != original[1]:
            self._work_notes.append(
                "Irregular or reshaped virtual mapping: source-work estimate conservatively includes the full source"
            )
            return [slice(0, n, 1) for n in source.shape]
        result = []
        for axis, part in enumerate(selection):
            lower = virtual[0][axis]
            upper = lower + virtual[1][axis]
            source_lower = original[0][axis]
            if isinstance(part, int):
                if not lower <= part < upper:
                    return None
                index = source_lower + part - lower
                if not 0 <= index < source.shape[axis]:
                    return None
                result.append(index)
            else:
                start, stop, step = part.indices(virtual_shape[axis])
                first = start + max(0, math.ceil((lower - start) / step)) * step
                end = min(stop, upper, lower + source.shape[axis] - source_lower)
                if first >= end:
                    return None
                result.append(slice(source_lower + first - lower, source_lower + end - lower, step))
        return result

    def _work(self, dataset, selection, seen=None):
        count = self._count(dataset.shape, selection)
        if count == 0:
            return 0
        chunk = dataset.chunks
        if chunk:
            decoded = _product(chunk) * _dtype(dataset).itemsize
            if decoded > MAX_CHUNK:
                raise ReadLimitError(f"Decoded chunk is {decoded:,} bytes; limit is 64 MiB")
            chunks = 1
            for n, size, part in zip(dataset.shape, chunk, selection):
                if isinstance(part, int):
                    continue
                start, stop, step = part.indices(n)
                elements = len(range(start, stop, step))
                chunks *= min(elements, (stop - 1) // size - start // size + 1) if elements else 0
            work = chunks * decoded
        else:
            work = count * _dtype(dataset).itemsize
        if dataset.is_virtual:
            seen = set() if seen is None else seen
            identity = self._identity(dataset)
            if identity in seen or len(seen) >= 16:
                raise ValueError("Virtual source cycle or excessive source depth")
            seen = seen | {identity}
            # Map ordinary rectangular VDS selections onto their actual sources.
            # Irregular mappings use a disclosed conservative estimate.
            for mapping in self._mappings(dataset):
                filename = self._resolve_virtual(dataset.file, _decode(mapping.file_name))
                if not os.path.isfile(filename):
                    continue
                try:
                    source = self._object(
                        _decode(mapping.dset_name), self._open_file(filename), configure=True
                    )
                except (KeyError, OSError):
                    continue
                if not isinstance(source, h5py.Dataset) or source.shape is None:
                    continue
                source_sel = self._mapped_selection(mapping, source, dataset.shape, selection)
                if source_sel is None:
                    continue
                work += self._work(source, source_sel, seen)
                if work > MAX_SOURCE_WORK:
                    break
        return work

    @staticmethod
    def _payload_size(array):
        array = np.asarray(array)
        size = array.nbytes
        if array.dtype.names:
            size += sum(
                Reader._payload_size(array[name])
                for name in array.dtype.names
                if array.dtype.fields[name][0].hasobject
            )
        elif array.dtype.kind == "O":
            for value in array.flat:
                size += (
                    Reader._payload_size(value)
                    if isinstance(value, np.ndarray)
                    else len(str(value).encode("utf-8"))
                )
                if size > MAX_RESULT:
                    break
        return size

    def _portable(self, array, file, notes):
        """Replace unpicklable references and bound expanded VLEN payloads."""
        array = np.asarray(array)
        if array.nbytes > MAX_RESULT:
            raise ReadLimitError("Decoded result exceeds 64 MiB")
        if array.dtype.names:
            for name in array.dtype.names:
                if array.dtype.fields[name][0].hasobject:
                    array[name] = self._portable(array[name], file, notes)
            return array
        if array.dtype.kind == "O":
            result = np.empty(array.shape, dtype=object)
            payload = array.nbytes
            for index in np.ndindex(array.shape):
                item = array[index]
                if isinstance(item, h5py.Reference):
                    item = self._text(item, file)
                elif isinstance(item, (str, bytes)):
                    original_len = len(item)
                    item = self._text(item, file)
                    if len(item) < original_len:
                        notes.append("Variable-length text truncated to 64 KiB")
                elif isinstance(item, np.ndarray):
                    item = self._portable(item, file, notes)
                payload += (
                    item.nbytes if isinstance(item, np.ndarray) else len(str(item).encode("utf-8"))
                )
                if payload > MAX_RESULT:
                    raise ReadLimitError("Expanded variable-length result exceeds 64 MiB")
                result[index] = item
            return result
        return array

    def _read(self, request, one_cell=False, portable=True):
        obj = self._object(request.get("path", "/"), configure=True)
        if not isinstance(obj, h5py.Dataset):
            raise TypeError("Values can only be read from datasets")
        purpose = request.get("purpose", "table")
        if purpose not in ("table", "image", "plot", "probe"):
            raise ValueError("Purpose must be table, image, plot, or probe")
        selection = self._selection(obj, request.get("selection"), purpose)
        count = self._count(obj.shape, selection)
        if one_cell and count != 1:
            raise ValueError("Select exactly one cell")
        sampled = request.get("selection") is None and count < _product(obj.shape)
        notes = []
        self._work_notes = []
        selected_dtype = _selected_dtype(_dtype(obj), request.get("field"))
        elements_per_cell = _product(selected_dtype.subdtype[1]) if selected_dtype.subdtype else 1
        while True:
            count = self._count(obj.shape, selection)
            work = self._work(obj, selection)
            oversized = count * _dtype(obj).itemsize > MAX_RESULT or work > MAX_SOURCE_WORK
            if purpose == "table" and count > MAX_TABLE_CELLS:
                oversized = True
            if purpose == "image" and count * elements_per_cell > MAX_IMAGE_ELEMENTS:
                oversized = True
                notes.append(
                    "Image overview limited to 2048 x 2048 elements; selection contains actual strides"
                )
            if not oversized:
                break
            if purpose not in ("image", "plot"):
                raise ReadLimitError(
                    "Selection exceeds decoded result, table, or 256 MiB source-work budget; select a smaller range"
                )
            candidates = [
                (len(range(*part.indices(obj.shape[i]))), i)
                for i, part in enumerate(selection)
                if isinstance(part, slice)
            ]
            if not candidates or max(candidates)[0] <= 1:
                raise ReadLimitError(
                    "Source work exceeds budget even at the smallest overview; choose a native region"
                )
            _, axis = max(candidates)
            part = selection[axis]
            selection[axis] = slice(part.start, part.stop, part.step * 2)
            sampled = True
        if (_dtype(obj).metadata or {}).get("hdf_integer"):
            output_shape = tuple(
                len(range(*part.indices(n)))
                for n, part in zip(obj.shape, selection)
                if isinstance(part, slice)
            )
            raw = np.empty(output_shape, dtype=_dtype(obj))
            space = obj.id.get_space()
            if not count:
                space.select_none()
            elif obj.shape:
                start = tuple(part.start if isinstance(part, slice) else part for part in selection)
                stride = tuple(part.step if isinstance(part, slice) else 1 for part in selection)
                counts = tuple(
                    len(range(*part.indices(n))) if isinstance(part, slice) else 1
                    for n, part in zip(obj.shape, selection)
                )
                space.select_hyperslab(start, counts, stride)
            memory = (
                h5py.h5s.create_simple(output_shape)
                if output_shape
                else h5py.h5s.create(h5py.h5s.SCALAR)
            )
            obj.id.read(memory, space, raw, mtype=obj.id.get_type())
            array = _wide_values(raw, obj.id.get_type())
        elif _has_vlen(_dtype(obj)) and count > 1:
            if count > DEFAULT_CELLS:
                raise ReadLimitError("Variable-length pages are limited to 4096 cells")
            output_shape = tuple(
                len(range(*part.indices(n)))
                for n, part in zip(obj.shape, selection)
                if isinstance(part, slice)
            )
            array = np.empty(output_shape, dtype=_dtype(obj))
            ranges = [
                range(*part.indices(n)) if isinstance(part, slice) else [part]
                for n, part in zip(obj.shape, selection)
            ]
            payload = array.nbytes
            for output_index, source_index in zip(
                np.ndindex(output_shape), itertools.product(*ranges)
            ):
                value = obj[source_index]
                if isinstance(value, (str, bytes)):
                    value = np.asarray(value, dtype=object)
                value = self._portable(value, obj.file, notes) if portable else np.asarray(value)
                payload += self._payload_size(value)
                if payload > MAX_RESULT:
                    raise ReadLimitError("Expanded variable-length page exceeds 64 MiB")
                array[output_index] = value[()] if value.shape == () else value
            array = np.asarray(_field(array, request.get("field")))
        else:
            value = _field(obj[tuple(selection)], request.get("field"))
            if isinstance(value, (str, bytes)) and _has_vlen(_dtype(obj)):
                value = np.asarray(value, dtype=object)
            array = self._portable(value, obj.file, notes) if portable else np.asarray(value)
        display_dtype = selected_dtype.subdtype[0] if selected_dtype.subdtype else selected_dtype
        if _has_bitfield(display_dtype) and display_dtype.itemsize == array.dtype.itemsize:
            # A scalar may be native-endian even when the file is big-endian.
            # astype preserves its value; the final view only restores metadata.
            array = array.astype(display_dtype, copy=False).view(display_dtype)
        if purpose == "image" and array.size > MAX_IMAGE_ELEMENTS:
            raise ReadLimitError("Expanded image exceeds the 2048 x 2048 element overview limit")
        if self._payload_size(array) > MAX_RESULT:
            raise ReadLimitError("Decoded result exceeds 64 MiB")
        notes.extend(self._work_notes)
        if sampled:
            notes.insert(0, "Bounded preview; selection shows the actual sampled range")
        if obj.is_virtual:
            notes.append(
                "Virtual sources may return fill values; values alone do not establish source provenance"
            )
        return {
            "array": array,
            "selection": [
                [x.start, x.stop, x.step] if isinstance(x, slice) else x for x in selection
            ],
            "shape": list(obj.shape),
            "sampled": sampled,
            "notes": list(dict.fromkeys(notes)),
        }

    def read(self, request):
        return self._read(request)

    def cell(self, request):
        return self._read(request, one_cell=True)

    def reference(self, request):
        if request.get("attribute") is not None:
            owner = self._object(request.get("path", "/"))
            aid = owner.attrs.get_id(request["attribute"])
            if aid.shape is None:
                raise ValueError("NULL attribute dataspace")
            if _product(aid.shape) * aid.get_type().get_size() > MAX_RESULT:
                raise ReadLimitError("Reference attribute exceeds 64 MiB")
            value = owner.attrs[request["attribute"]]
            if self._payload_size(value) > MAX_RESULT:
                raise ReadLimitError("Expanded reference attribute exceeds 64 MiB")
            value = _field(value, request.get("field"))
            array = np.asarray(value)
            encoded = request.get("selection", [])
            if encoded:
                if any(not isinstance(x, int) for x in encoded):
                    raise ValueError("Attribute reference selection requires integer indices")
                for index in encoded:
                    array = np.asarray(array[index])
            if array.size != 1:
                raise ValueError("Select exactly one attribute reference")
            ref = array.reshape(-1)[0]
        else:
            result = self._read(request, one_cell=True, portable=False)
            if result["array"].size != 1:
                raise ValueError("Select exactly one reference value")
            ref = result["array"].reshape(-1)[0]
        if not isinstance(ref, h5py.Reference):
            raise TypeError("Selected cell is not an HDF5 reference")
        if not ref:
            raise ValueError("NULL reference")
        dataset = self._object(request.get("path", "/"))
        target = self._object(_decode(h5py.h5r.get_name(ref, dataset.file.id)), dataset.file)
        result = {
            "target": target.name,
            "filename": str(Path(target.file.filename).resolve()),
            "region": None,
        }
        if isinstance(ref, h5py.RegionReference):
            space = h5py.h5r.get_region(ref, dataset.file.id)
            region = _space(space)
            result["region"] = region
            if (
                region.get("selection_type") == "hyperslabs"
                and "start" in region
                and all(
                    b == 1 or s == b or c == 1
                    for b, s, c in zip(region["block"], region["stride"], region["count"])
                )
            ):
                selection = []
                for start, stride, count, block in zip(
                    region["start"], region["stride"], region["count"], region["block"]
                ):
                    selection.append(
                        [start, start + stride * (count - 1) + block, stride if block == 1 else 1]
                    )
                if region["points"] * _dtype(target).itemsize <= MAX_RESULT:
                    result["selection"] = selection
        return result

    def search(self, request):
        query = str(request.get("query", "")).casefold()
        limit = max(1, min(MAX_ITEMS, int(request.get("limit", MAX_ITEMS))))
        items, seen = [], set()
        pending = ["/"]
        visited = 0
        while pending:
            path = pending.pop()
            try:
                obj = self._object(path)
                identity = self._identity(obj)
            except (KeyError, OSError, TypeError, ValueError, RuntimeError):
                continue
            if identity in seen:
                name = path.rsplit("/", 1)[-1] or "/"
                if query in name.casefold() or query in path.casefold():
                    if len(items) >= limit:
                        return {"items": items, "truncated": True}
                    items.append({"path": path, "kind": self._kind(obj), "name": name})
                continue
            seen.add(identity)
            visited += 1
            if visited > 100_000:
                return {"items": items, "truncated": True}
            candidates = [(path, self._kind(obj), path.rsplit("/", 1)[-1] or "/")]
            for name in self._attribute_names(obj, limit=10_000):
                candidates.append((path, "attribute", name))
                if len(candidates) > 10_000:
                    break
            if isinstance(obj, (h5py.Dataset, h5py.Datatype)):
                try:
                    candidates.extend((path, "field", f["name"]) for f in _fields(_dtype(obj)))
                except (ValueError, TypeError):
                    pass
            for item_path, kind, name in candidates:
                if query in name.casefold() or query in item_path.casefold():
                    if len(items) >= limit:
                        return {"items": items, "truncated": True}
                    items.append({"path": item_path, "kind": kind, "name": name})
            if isinstance(obj, h5py.Group):
                for name in obj:
                    link = obj.get(name, getlink=True)
                    child_path = path.rstrip("/") + "/" + name
                    if isinstance(link, h5py.HardLink):
                        pending.append(child_path)
                    elif query in name.casefold():
                        if len(items) >= limit:
                            return {"items": items, "truncated": True}
                        items.append(
                            {
                                "path": child_path,
                                "kind": "external_link"
                                if isinstance(link, h5py.ExternalLink)
                                else "soft_link",
                                "name": name,
                            }
                        )
                    if len(pending) > 100_000:
                        return {"items": items, "truncated": True}
        return {"items": items, "truncated": False}

    def sources(self, request):
        path = request.get("path", "/")
        items = []
        # Expose a selected external link even when its target is absent.
        parent_path, name = path.rstrip("/").rsplit("/", 1) if path != "/" else ("/", "")
        if name:
            parent = self._object(parent_path or "/")
            link = parent.get(name, getlink=True)
            if isinstance(link, h5py.ExternalLink):
                resolved = self._resolve(parent.file, link.filename)
                item = {
                    "kind": "external_link",
                    "recorded_file": link.filename,
                    "resolved_file": resolved,
                    "dataset": link.path,
                }
                self._source_status(item)
                return {"items": [item]}
            if isinstance(link, h5py.SoftLink):
                items.append(
                    {
                        "kind": "soft_link",
                        "recorded_file": ".",
                        "resolved_file": str(Path(parent.file.filename).resolve()),
                        "dataset": link.path
                        if link.path.startswith("/")
                        else parent.name.rstrip("/") + "/" + link.path,
                        "recorded_target": link.path,
                        "status": "link; resolve on navigation",
                    }
                )
        try:
            obj = self._object(path)
        except (OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
            if not items:
                raise
            items[-1]["status"] = f"unavailable: {exc}"
            return {"items": items}
        if isinstance(obj, h5py.Dataset):
            if obj.is_virtual:
                mapping_count = obj.id.get_create_plist().get_virtual_count()
                if mapping_count > MAX_ITEMS:
                    return {
                        "items": items,
                        "truncated": True,
                        "total": mapping_count,
                        "limit": MAX_ITEMS,
                        "notice": "More than 1000 virtual mappings; inspect individual source files",
                    }
                for mapping in self._mappings(obj):
                    recorded = _decode(mapping.file_name)
                    item = {
                        "kind": "virtual",
                        "recorded_file": recorded,
                        "resolved_file": self._resolve_virtual(obj.file, recorded),
                        "dataset": _decode(mapping.dset_name),
                        "virtual_selection": _space(mapping.vspace),
                        "source_selection": _space(mapping.src_space),
                    }
                    self._source_status(item)
                    if item.get("shape") is not None:
                        bounds = item["source_selection"].get("bounds")
                        if bounds and len(bounds[1]) == len(item["shape"]):
                            item["requested_extent_available"] = all(
                                end < extent for end, extent in zip(bounds[1], item["shape"])
                            )
                            if not item["requested_extent_available"]:
                                item["status"] = (
                                    "partial extent: source object exists but mapping exceeds its extent"
                                )
                        elif item["source_selection"]["selection_type"] == "all":
                            item["requested_extent_available"] = item["virtual_selection"][
                                "points"
                            ] <= _product(item["shape"])
                            if not item["requested_extent_available"]:
                                item["status"] = (
                                    "partial extent: source object exists but mapping exceeds its extent"
                                )
                    item["fill_behavior"] = (
                        "Unavailable VDS sources may produce the dataset fill value; no cell provenance is inferred"
                    )
                    items.append(item)
            for filename, offset, size in (obj.external or [])[:MAX_ITEMS]:
                recorded = _decode(filename)
                resolved = self._resolve(obj.file, recorded)
                item = {
                    "kind": "external_raw",
                    "recorded_file": recorded,
                    "resolved_file": resolved,
                    "offset": int(offset),
                    "size": int(size),
                }
                try:
                    with open(resolved, "rb") as stream:
                        stream.seek(0, os.SEEK_END)
                        item["available_bytes"] = stream.tell()
                    required = offset + size if size != h5py.h5f.UNLIMITED else offset
                    item["status"] = (
                        "readable"
                        if item["available_bytes"] >= required
                        else "partial extent: external raw file is too short"
                    )
                except OSError as exc:
                    item["status"] = f"unavailable: {exc}"
                items.append(item)
            for axis, dim in enumerate(obj.dims):
                try:
                    for _, scale in itertools.islice(dim.items(), 100):
                        items.append(
                            {
                                "kind": "dimension_scale",
                                "recorded_file": ".",
                                "resolved_file": str(Path(scale.file.filename).resolve()),
                                "dataset": scale.name,
                                "axis": axis,
                                "status": "readable",
                            }
                        )
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    RuntimeError,
                    KeyError,
                    OverflowError,
                ) as exc:
                    items.append(
                        {
                            "kind": "dimension_scale",
                            "recorded_file": ".",
                            "resolved_file": str(Path(obj.file.filename).resolve()),
                            "axis": axis,
                            "status": f"unavailable: {exc}",
                        }
                    )
        return {"items": items[:MAX_ITEMS], "truncated": len(items) > MAX_ITEMS}

    def _source_status(self, item):
        try:
            obj = self._object(item["dataset"], self._open_file(item["resolved_file"]))
            item["status"] = (
                "readable"
                if isinstance(obj, (h5py.Dataset, h5py.Group, h5py.Datatype))
                else "unsupported object"
            )
            item["readability"] = "Object metadata opened; payload readability has not been probed"
            if isinstance(obj, h5py.Dataset):
                item["shape"] = None if obj.shape is None else list(obj.shape)
                plist = obj.id.get_create_plist()
                missing = [
                    plist.get_filter(i)[0]
                    for i in range(plist.get_nfilters())
                    if not h5py.h5z.filter_avail(plist.get_filter(i)[0])
                ]
                if missing:
                    item["status"] = f"unavailable filters: {missing}"
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, OverflowError) as exc:
            item["status"] = f"unavailable: {exc}"
