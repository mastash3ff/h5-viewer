# Architecture and implementation contract

The application has three layers: a pure HDF5 reader, a process client, and Qt
presentation. All source opens are read-only. No h5py objects cross process IPC.
Default file view is root contents; default dataset view is values/table.

## Reader contract

`h5_viewer.reader.Reader(filename)` owns one primary file and any referenced files.
`Reader.handle(request: dict) -> dict` executes one operation. `close()` releases
handles. Exceptions are converted to error replies by the process layer.

Operations (all paths are HDF5 paths, default `/`):

- `describe`: request `path`; response `path`, `kind` (`group`, `dataset`, `datatype`),
  `shape` (list, null for null dataspace), `dtype` (str), `properties` (dict of
  display-safe values), `fields` (list of `{name, dtype}`; dotted nested field names),
  `attributes` (list of `{name, dtype, shape, text, error?}`), optional `error`.
  Groups and datatypes omit irrelevant keys. Isolate attribute failures.
- `children`: `path`, `offset=0`, `limit=200`; response `items` list of
  `{name,path,kind,shape?,dtype?,link_kind?,target?,error?,expandable?}`, `total`,
  `offset`, `has_more`. List links without eager external dereference. Mark cycles.
- `read`: `path`, optional `selection` list (one element per dataset dimension;
  integer index or `[start,stop,step]` slice), optional dotted `field`,
  `purpose` (`table`, `image`, `plot`, `probe`, default `table`). Response `array`
  (bounded ndarray, fields applied after dataset selection), `selection` (actual
  selection, including any adaptive image strides), `shape` (original dataset
  shape), `sampled` (bool), `notes` (list[str]). Retained dimensions stay in original
  dimension order. Scalar reads use `selection=[]`. Missing selection must be
  bounded by the reader, never blindly load a whole dataset. For compound data,
  arrays retain field structure unless a field was explicitly selected.
- `search`: `query`, `limit=1000`; response `items` list of `{path,kind,name}`,
  `truncated` (bool). Search object, attribute and compound field names only.
  No payload searches or implicit external-file traversal. This operation can be
  canceled by restarting the reader process; UI must label cancellation.
- `sources`: `path`; response `items` list of dicts with `kind`, `recorded_file`,
  `resolved_file`, `dataset` where applicable, `status`, and mapping/offset details.
  Source resolution and availability checking must match actual reader resolution.
  Source readability and fill behavior are separate; no provenance from fill values.
- `reference`: `path`, `selection` selecting one reference cell, optional `field`;
  response `target` (object path), `filename`, `region` (description if region ref),
  optional `selection` for a regular bounded region. Never eagerly read region data.
- `cell`: `path`, `selection` selecting one cell, optional `field`; bounded expanded
  cell read returns same response shape as `read`. Used for ragged/array cells.
- `attribute`: `path`, `name`; explicitly load a deferred attribute, returning its
  name/type/shape, display-safe text, bounded portable `array`, notes, and any
  reference descriptors. Multi-cell VLEN attributes are deferred during ordinary
  description because HDF5 attribute reads cannot select individual cells. The
  explicit read remains subject to worker memory and expanded-result limits.
  Reference requests also accept `attribute` and nested `field` where applicable.

Reader-level limits: 64 MiB per decoded chunk/result, 256 MiB estimated decoded
source work per interactive request; adaptive striding for image/plot overviews,
otherwise explicit over-budget error. Image overviews are additionally capped at
2048*2048 returned elements; exact probe requests retain native selection. HDF5 raw chunk caches total at most 64 MiB;
all reader caches together at most 256 MiB. Fixed-size attributes preflight type
and dataspace. VLEN previews one cell at a time in isolated worker; text returned
is at most 64 KiB per value and explicitly marked truncated. Never auto-repeat a
failed request. Missing filters, types, source paths and null data are item errors.
128-bit integer attributes use bounded low-level reads and declared endian/sign.

## Process contract

`h5_viewer.process.ReaderClient(filename)` starts a spawned worker with 1 GiB
memory limit. Worker startup reports enforcement failure rather than proceeding
unbounded. `request(request_dict, timeout=30) -> dict` returns reader reply or
raises `ReaderError`. Calls must occur off the GUI thread. `cancel()` terminates
and reaps the worker; next request starts a fresh worker without retrying any
previous request. `close()` releases resources. `status` exposes limits/platform.
Only one request is active per client. Replies carry IDs; obsolete UI generations
must be ignored. IPC is bounded (64 MiB results plus small metadata overhead).
Windows uses Job Objects; Linux test workers use a practical enforced address-space
limit allowing baseline virtual mappings plus a 1 GiB allocation budget.

## UI contract

`h5_viewer.window.MainWindow(filename: str | None = None)` is the main window.
It uses ReaderClient from a background Qt dispatcher, and shares selection controls
between table, plot and image. It never calls h5py on the GUI thread. Errors appear
in the inspector/status; unreadable items do not erase the file browser.
Core widgets: sources/files, lazy HDF tree, path/back/forward/search, contents,
properties+attributes, paged field table, dimensions/slices, plot/image/source tabs.
Aliases are displayed distinctly. Explicit external-link navigation opens a new
window on the resolved file/target. Object/region refs and dimension scales can be
navigated. User-supplied schema semantics are never assumed from shape alone.
Table values preserve large integer precision. Numeric selections expose bounded
statistics/histogram and plot. Images support manual axes/channels, complex component,
contrast, preview stride, pan/zoom and native-pixel region selection.

## Acceptance

Generated fixtures test non-image workflows, nested/compound/VLEN/enum/reference
types, links/cycles, dimensions, VDS/external sources and missing targets, source
resolution, integer precision, limits and failure recovery. Real input files are
read-only integration inputs, never fixtures committed to Git. Windows build
acceptance includes frozen worker startup, enforcement, Unicode paths, offline
runtime, shell argument handling, and actual Explorer integration after release
approval. Local portable checks do not claim registered double-click completion.
