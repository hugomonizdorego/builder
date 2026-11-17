"""Small host helpers shared by backend modules."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ubuntu_builder.core.errors import InsufficientDiskSpaceError


def existing_parent(path: Path) -> Path:
    """Closest existing ancestor of ``path`` (for disk space queries)."""
    path = path.absolute()
    while not path.exists():
        path = path.parent
    return path


def free_space(path: Path) -> int:
    """Free bytes on the file system that holds (or will hold) ``path``."""
    return shutil.disk_usage(existing_parent(path)).free


def require_free_space(path: Path, required: int) -> None:
    """Raise :class:`InsufficientDiskSpaceError` unless ``required`` bytes fit."""
    available = free_space(path)
    if available < required:
        raise InsufficientDiskSpaceError(str(existing_parent(path)), required, available)


def invoking_user() -> tuple[int, int] | None:
    """uid/gid of the desktop user when running elevated via pkexec/sudo."""
    for uid_var, gid_var in (("PKEXEC_UID", None), ("SUDO_UID", "SUDO_GID")):
        uid = os.environ.get(uid_var)
        if uid and uid.isdigit():
            gid = os.environ.get(gid_var) if gid_var else None
            if gid and gid.isdigit():
                return int(uid), int(gid)
            try:
                import pwd

                return int(uid), pwd.getpwuid(int(uid)).pw_gid
            except KeyError:
                return int(uid), int(uid)
    return None


def give_to_invoking_user(path: Path) -> None:
    """Hand a file created as root back to the user who asked for it."""
    owner = invoking_user()
    if owner is not None and os.geteuid() == 0:
        os.chown(path, *owner)
