"""The small Qt runtime surface used by the widget application."""

from pathlib import PurePosixPath

QT_DLLS = frozenset(
    f"qt6{name}.dll"
    for name in ("core", "gui", "widgets", "network", "opengl", "openglwidgets", "svg")
)


def include_qt_entry(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/").lower())
    if "qml" in path.parts:
        return False
    if path.name in {"qtvirtualkeyboardplugin.dll", "qpdf.dll"}:
        return False
    if path.name.startswith("qt6") and path.suffix == ".dll":
        return path.name in QT_DLLS
    return True
