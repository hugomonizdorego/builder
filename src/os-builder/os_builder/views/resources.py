"""Icons and style sheets.

The toolbar uses the icon set of Ubuntu Builder 2.x: ``icons/Light`` (white
glyphs for the dark header) and ``icons/Dark`` (dark glyphs for a light
header). The directory is looked up next to the package in a source
checkout and under ``/usr/share/os-builder`` once installed.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PyQt6.QtGui import QIcon, QPixmap

_CANDIDATES = (
    Path(__file__).resolve().parents[2],  # src/os-builder in a checkout
    Path("/usr/share/os-builder"),
)

UBUNTU_ORANGE = "#e95420"


@cache
def data_dir() -> Path | None:
    return next((path for path in _CANDIDATES if (path / "icons").is_dir()), None)


def toolbar_icon(name: str, variant: str) -> QIcon:
    """``name`` is one of build, export, import, qemu, remove, settings, style, wizard."""
    base = data_dir()
    if base is None:
        return QIcon()
    return QIcon(str(base / "icons" / variant / f"{name}-{variant.lower()}.png"))


def app_icon() -> QIcon:
    base = data_dir()
    for path in ((base / "os-builder.png") if base else None, Path("/usr/share/pixmaps/os-builder.png")):
        if path is not None and path.is_file():
            return QIcon(str(path))
    return QIcon()


def logo(size: int) -> QPixmap:
    return app_icon().pixmap(size, size)


def header_style(dark: bool) -> str:
    """Style sheet of the header bar (Ambiance-like dark, or Radiance-like light)."""
    if dark:
        background = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #4a4945, stop:1 #2f2e2b)"
        text, hover, pressed, border = "#f2f1f0", "rgba(255,255,255,30)", "rgba(0,0,0,60)", "#1d1c1a"
    else:
        background = "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f6f6f5, stop:1 #dcdbd9)"
        text, hover, pressed, border = "#3c3b37", "rgba(0,0,0,20)", "rgba(0,0,0,40)", "#b5b3ad"
    return f"""
    QFrame#header {{ background: {background}; border-bottom: 1px solid {border}; }}
    QFrame#header QLabel {{ color: {text}; }}
    QFrame#header QLabel#title {{ font-size: 17pt; font-weight: 600; }}
    QFrame#header QToolButton {{
        color: {text}; background: transparent; border: 1px solid transparent; border-radius: 4px;
        padding: 4px 6px; min-width: 62px;
    }}
    QFrame#header QToolButton:hover {{ background: {hover}; }}
    QFrame#header QToolButton:pressed {{ background: {pressed}; }}
    QFrame#header QToolButton:disabled {{ color: #8c8a85; }}
    QFrame#header QToolButton::menu-indicator {{ image: none; width: 0; }}
    """


APP_STYLE = f"""
QProgressBar {{ border: 1px solid #b5b3ad; border-radius: 3px; text-align: center; height: 16px; }}
QProgressBar::chunk {{ background: {UBUNTU_ORANGE}; border-radius: 2px; }}
QGroupBox {{ font-weight: 600; }}
QPushButton#action {{ text-align: left; padding: 6px 10px; }}
"""
