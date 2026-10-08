"""Divert frozen reader workers before PyInstaller initializes Qt."""

import multiprocessing

# The preceding PyInstaller multiprocessing runtime hook installs its frozen
# spawn/helper support. The GUI parent returns here; workers exit in their own
# reader entrypoint without reaching the automatic PySide runtime hook.
multiprocessing.freeze_support()
