"""PyInstaller entry point with multiprocessing initialization first."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from h5_viewer.app import main

    raise SystemExit(main())
