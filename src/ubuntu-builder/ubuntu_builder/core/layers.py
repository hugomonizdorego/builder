"""Squashfs layout of Ubuntu live images.

Up to 22.10 an image holds a single ``casper/filesystem.squashfs``. Newer
images are *layered*: ``minimal.squashfs`` is the base, and a file named
``minimal.standard.squashfs`` only stores what ``standard`` adds on top of
``minimal``. The dotted file name therefore spells out the whole stack.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import yaml

CLASSIC_SQUASHFS = "casper/filesystem.squashfs"
INSTALL_SOURCES = "casper/install-sources.yaml"


def _stem(path: str) -> str:
    return PurePosixPath(path).name.removesuffix(".squashfs")


def lower_layers(target: str, available: list[str]) -> list[str]:
    """Layers stacked below ``target``, bottom first.

    ``a.b.c`` sits on ``a.b`` which sits on ``a``; unrelated siblings such
    as ``a.de`` (a language pack) are not part of the stack.
    """
    parts = _stem(target).split(".")
    prefixes = {".".join(parts[:count]) for count in range(1, len(parts))}
    below = [path for path in available if _stem(path) in prefixes]
    return sorted(below, key=lambda path: _stem(path).count("."))


def default_target(available: list[str], install_sources: bytes | None = None) -> str | None:
    """Pick the squashfs a user most likely wants to customise.

    Preference: the classic single file system, then the installer's
    default install source (the system that ends up on disk), then the
    base layer.
    """
    if not available:
        return None
    if CLASSIC_SQUASHFS in available:
        return CLASSIC_SQUASHFS
    by_name = {PurePosixPath(path).name: path for path in available}
    if install_sources:
        try:
            document = yaml.safe_load(install_sources)
        except yaml.YAMLError:
            document = None
        # Format version 2 wraps the list in a mapping; version 1 is the list.
        sources = document.get("sources") if isinstance(document, dict) else document
        if isinstance(sources, list):
            entries = [s for s in sources if isinstance(s, dict) and s.get("path") in by_name]
            for entry in entries:
                if entry.get("default"):
                    return by_name[entry["path"]]
            if entries:
                return by_name[entries[0]["path"]]
    return min(available, key=lambda path: (_stem(path).count("."), path))


def describe_layer(target: str, available: list[str]) -> str:
    """One-line explanation of a layer for the selection dialog."""
    below = lower_layers(target, available)
    name = PurePosixPath(target).name
    if target == CLASSIC_SQUASHFS:
        return f"{name} — the complete root file system"
    if not below:
        return f"{name} — base layer (shared by every variant on this image)"
    return f"{name} — adds to {' + '.join(_stem(p).split('.')[-1] for p in below)}"
