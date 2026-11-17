"""Extend the live initrd without rebuilding it.

The Linux kernel unpacks several initramfs archives placed one after the
other. To give the *live* session a new boot splash, the files Plymouth
needs (theme, plugins, libraries, fonts) are collected with Plymouth's own
initramfs hook inside the image and appended to ``casper/initrd`` as an
extra, uncompressed cpio archive. The original initrd is kept in the
workspace so that applying again does not stack archives.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

_TRAILER = "TRAILER!!!"


def _header(name: str, mode: int, size: int, ino: int, mtime: int, nlink: int) -> bytes:
    fields = (ino, mode, 0, 0, nlink, mtime, size, 0, 0, 0, 0, len(name) + 1, 0)
    return ("070701" + "".join(f"{value:08X}" for value in fields)).encode("ascii")


def _pad(length: int) -> bytes:
    return b"\0" * ((4 - length % 4) % 4)


def write_newc(root: Path, output: Path) -> int:
    """Write the tree below ``root`` as a "newc" cpio archive. Returns the entry count."""
    entries = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    count = 0
    with output.open("wb") as out:
        for ino, path in enumerate(entries, start=1):
            info = path.lstat()
            name = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                data = os.readlink(path).encode()
            elif stat.S_ISREG(info.st_mode):
                data = path.read_bytes()
            elif stat.S_ISDIR(info.st_mode):
                data = b""
            else:
                continue  # device nodes and the like are never needed here
            mode = stat.S_IFMT(info.st_mode) | stat.S_IMODE(info.st_mode)
            header = _header(name, mode, len(data), ino, int(info.st_mtime), 2 if stat.S_ISDIR(info.st_mode) else 1)
            block = header + name.encode() + b"\0"
            out.write(block + _pad(len(block)) + data + _pad(len(data)))
            count += 1
        trailer = _header(_TRAILER, 0, 0, 0, 0, 1) + _TRAILER.encode() + b"\0"
        out.write(trailer + _pad(len(trailer)))
        out.write(b"\0" * ((512 - out.tell() % 512) % 512))
    return count


def append_overlay(initrd: Path, overlay: Path, originals: Path) -> None:
    """``initrd`` := original initrd + ``overlay`` (original kept in ``originals``)."""
    originals.mkdir(parents=True, exist_ok=True)
    original = originals / f"{initrd.name}.orig"
    if not original.exists():
        shutil.copyfile(initrd, original)
    with initrd.open("wb") as out:
        with original.open("rb") as source:
            shutil.copyfileobj(source, out)
        out.write(b"\0" * ((512 - out.tell() % 512) % 512))  # archives start on a block boundary
        with overlay.open("rb") as extra:
            shutil.copyfileobj(extra, out)
