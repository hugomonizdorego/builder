"""Image helpers without external dependencies.

The backend runs as root without a GUI, so image handling is limited to
what the boot chain needs: recognising formats and writing simple PNGs.
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def image_kind(path: Path) -> str | None:
    """"png", "jpeg", "svg" or "webp" from the file's magic bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if b"<svg" in head.lower():
        return "svg"
    return None


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if not header.startswith(b"\x89PNG") or header[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG file")
    return struct.unpack(">II", header[16:24])


def parse_color(value: str) -> tuple[int, int, int]:
    """``#RRGGBB`` → (r, g, b)."""
    match = _COLOR.match(value.strip())
    if not match:
        raise ValueError(f"Not a colour: {value!r} (expected #RRGGBB)")
    raw = int(match.group(1), 16)
    return (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF


def solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """A PNG filled with one colour (used for the splash progress bar)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgba) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height, 9))
        + chunk(b"IEND", b"")
    )
