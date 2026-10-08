"""Create only synthetic release-test data; refuse to overwrite an existing file."""

from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import numpy as np


def create_fixture(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(destination, "x") as file:
        file.attrs["description"] = "Synthetic release smoke fixture"
        matrix = np.arange(192, dtype=np.float32).reshape(12, 16)
        for compression in ("gzip", "lzf"):
            file.create_dataset(compression, data=matrix, compression=compression)
        file["complex"] = matrix.astype(np.complex64) * (1 + 2j)
        file.create_dataset("text", data=["alpha", "café"], dtype=h5py.string_dtype())
        file["compound"] = np.array([(7, 1.25), (9, 2.5)], dtype=[("id", "i8"), ("value", "f8")])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    create_fixture(parser.parse_args().destination)


if __name__ == "__main__":
    main()
