# Using H5 Viewer

H5 Viewer opens source files read-only. It does not change datasets, labels,
attributes, links, or external sources. Viewing preferences are stored separately.

## Open and explore

Open a file with **File > Open file**, drag it onto the window, or use the installed
Windows **Open with** entry. Once you select H5 Viewer as the default application
for `.h5` and `.hdf5`, double-clicking a file opens it directly.

**File > Browse folder** lists HDF5 files in a local folder. Each opened file has
its own window. The **File contents** tree and **Contents** tab expose the hierarchy;
enter a complete HDF5 object path in the toolbar to go directly to an object.
**Back**, **Forward**, and **Root** navigate within the file.

Search in the toolbar to find object, attribute, and compound-field names. Search
does not scan all stored values. A canceled or limited search is labeled accordingly.

## Inspect fields and values

Datasets open in **Table / Values**. The right-hand **Fields** list shows the dataset's
compound members, including nested paths such as `position.x`. Select a field to
inspect it independently, or select **All fields / dataset values** for a record table.

| Control | Use |
|---|---|
| Rows / Y and Columns / X | Choose which dimensions appear as rows and columns |
| Dimension entry | Enter an index, or a `start:stop:step` slice; stop is exclusive |
| Page, Previous, Next | Move through a bounded page along the selected row axis |
| Jump | Go to a particular row index |
| Read selection | Apply the selected dimensions and ranges |
| Fixed-dimension - / + | Move one sample or channel without changing the display plane |

Indices are zero-based. Double-click a cell to expand a string, array, or variable-length
value. Right-click to copy selected cells or follow an object/region reference.
Large integers remain exact in the table; plots necessarily use numeric display
representations.

**Properties** shows storage and type information. **Attributes** shows metadata on
the current object; use its pagination controls for large attribute collections.
Double-click an attribute for its full bounded inspection. Some variable-length
attributes defer their payload until explicitly opened.

## Explore relationships and sources

**Sources / Links** shows external links, virtual-dataset mappings, backing storage,
and dimension scales. Inspect an entry's details for the recorded filename, effective
resolved path, target, selection, and availability. Follow an available HDF5 target
to inspect its contents. A recorded provenance string is displayed as metadata; it
does not automatically become a trusted link.

Virtual-dataset fill values are not evidence of source provenance. The viewer reports
source accessibility, mapped extents, and fill behavior separately. Missing targets
and decoding limitations remain visible rather than being silently treated as data.

For aligned record columns, enable **Show companion row (opt in)**, choose the sample
axis, and select a sibling dataset with a matching leading length. This is your
explicit association; matching lengths alone do not prove that records correspond.

## Plots and images

**Plot / Statistics** lets you choose numeric columns, the X coordinate, complex
component, and histogram display. Statistics describe the loaded selection, not
necessarily the entire dataset. Sampling and excluded non-finite values are labeled.

For an image stack, choose **Image plane: last 2 axes**, check the axis assignments,
and switch to **Image**. Fix every other dimension to a sample or channel index.
Use the dimension steppers to browse. For a channel-last array, select the first two
axes as Y/X and fix the final channel dimension instead.

Drag to pan and use the wheel to zoom. Use automatic or manual contrast and choose
a color map. Complex data offers magnitude, phase, real, and imaginary representations.
The viewer displays the actual selection and sampling strides. **Read visible at
native pixels** requests an exact region; an oversized exact request produces an
explanation instead of silently reducing resolution.

## Large or unusual files

The viewer limits individual reads and isolates HDF5 work from the interface. If an
operation exceeds a limit, select a smaller range or inspect the metadata. Large
stored chunks may exceed the limit even for a small requested image. **Cancel read**
stops outstanding work; choose an object or read a new selection to continue.

The standard build includes gzip and LZF support. Files requiring an unavailable
third-party filter show the filter and read limitation. HDF4 files and custom vendor
decoders are not included. See [architecture](ARCHITECTURE.md) for precise resource
limits and [Windows delivery](WINDOWS.md) for build and installation instructions.
