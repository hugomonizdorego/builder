"""Install GTK, icon and cursor themes from archives into the image.

Themes from sites like gnome-look.org come as ``.tar.gz``/``.tar.xz``/
``.zip`` files with one or more theme directories inside. Each directory is
classified by its content and copied to ``/usr/share/themes`` or
``/usr/share/icons``. Archives are untrusted input: absolute paths, ``..``
components and links that leave the theme are refused.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from os_builder.core.errors import BuilderError

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar.zst", ".tar", ".zip")
_MAX_SIZE = 2 * 1024**3  # unpacked bytes


class ThemeArchiveError(BuilderError):
    """The archive is not a usable theme archive."""


def is_theme_archive(path: str) -> bool:
    return path.lower().endswith(ARCHIVE_SUFFIXES)


def _safe_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not PurePosixPath(name).is_absolute() and ".." not in parts


def _extract(archive: Path, dest: Path) -> None:
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if sum(m.file_size for m in members) > _MAX_SIZE:
                raise ThemeArchiveError(f"{archive.name} is too large.")
            for member in members:
                if not _safe_member(member.filename):
                    raise ThemeArchiveError(f"Unsafe path in {archive.name}: {member.filename}")
            bundle.extractall(dest)
        return
    if archive.name.lower().endswith(".tar.zst"):
        raise ThemeArchiveError("Zstandard tar archives are not supported; use .tar.gz, .tar.xz or .zip.")
    try:
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            if sum(m.size for m in members) > _MAX_SIZE:
                raise ThemeArchiveError(f"{archive.name} is too large.")
            # The "data" filter refuses absolute paths, "..", device files and
            # links pointing outside the destination, and drops setuid bits.
            bundle.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ThemeArchiveError(f"Cannot unpack {archive.name}: {exc}") from exc


def classify(theme_dir: Path) -> str | None:
    """"icons" (icon or cursor theme), "themes" (GTK/shell theme) or None."""
    index = theme_dir / "index.theme"
    text = index.read_text(encoding="utf-8", errors="replace") if index.is_file() else ""
    if (theme_dir / "cursors").is_dir() or "[Icon Theme]" in text:
        return "icons"
    if any((theme_dir / name).is_dir() for name in ("gtk-2.0", "gtk-3.0", "gtk-4.0", "gnome-shell", "xfwm4",
                                                    "metacity-1", "cinnamon", "openbox-3")):
        return "themes"
    if "X-GNOME-Metatheme" in text:
        return "themes"
    return None


def find_themes(root: Path) -> list[tuple[Path, str]]:
    """Theme directories at most two levels below ``root``."""
    found: list[tuple[Path, str]] = []
    candidates = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    for candidate in candidates:
        kind = classify(candidate)
        if kind:
            found.append((candidate, kind))
            continue
        for child in sorted(candidate.iterdir()):  # archives with a wrapper folder
            if child.is_dir() and not child.is_symlink() and (kind := classify(child)):
                found.append((child, kind))
    return found


def install_theme_archive(archive: Path, rootfs: Path) -> list[str]:
    """Unpack ``archive`` and copy its themes into ``rootfs``. Returns their names."""
    if not archive.is_file():
        raise ThemeArchiveError(f"Theme archive not found: {archive}")
    with tempfile.TemporaryDirectory(prefix="os-builder-theme-") as tmp:
        work = Path(tmp)
        _extract(archive, work)
        themes = find_themes(work)
        if not themes:
            raise ThemeArchiveError(
                f"No theme found in {archive.name}: expected folders with index.theme, gtk-3.0, gtk-4.0 or cursors."
            )
        installed = []
        for source, kind in themes:
            target = rootfs / "usr/share" / kind / source.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, symlinks=True)
            for path in [target, *target.rglob("*")]:
                if path.is_symlink():
                    continue
                path.chmod(0o755 if path.is_dir() else 0o644)
            installed.append(f"{kind}/{source.name}")
        return installed
