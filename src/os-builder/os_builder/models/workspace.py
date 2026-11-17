"""Workspace layout and persisted project state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Placeholder stored in :attr:`ProjectState.boot_options` instead of the
#: absolute boot-image directory, so a workspace can be moved or renamed.
BOOT_DIR_TOKEN = "@BOOT@"


@dataclass(slots=True)
class ProjectState:
    """What was learned about the source ISO when it was extracted."""

    source_iso: str = ""
    volume_id: str = ""
    #: Squashfs being customised, relative to the ISO tree
    #: (``casper/filesystem.squashfs`` or one layer of a layered image).
    target_squashfs: str = ""
    #: Layers below the target, bottom first. Empty for classic images.
    lower_squashfs: list[str] = field(default_factory=list)
    #: Boot related ``xorriso -as mkisofs`` options replicating the source.
    boot_options: list[str] = field(default_factory=list)
    extracted: bool = False

    @property
    def is_layered(self) -> bool:
        return bool(self.lower_squashfs)


@dataclass(frozen=True, slots=True)
class Workspace:
    """Directory layout of one remastering project.

    ``iso/``      extracted ISO tree (everything except the root file system)
    ``edit/``     unpacked contents of the squashfs being customised
    ``rootfs/``   chroot root — only populated while a session is mounted
    ``lower/``    read-only mounts of lower squashfs layers (layered images)
    ``boot/``     boot sectors / EFI partition copied from the source ISO
    """

    root: Path

    @property
    def iso_dir(self) -> Path:
        return self.root / "iso"

    @property
    def edit_dir(self) -> Path:
        return self.root / "edit"

    @property
    def rootfs_dir(self) -> Path:
        return self.root / "rootfs"

    @property
    def lower_dir(self) -> Path:
        return self.root / "lower"

    @property
    def overlay_work_dir(self) -> Path:
        return self.root / "overlay-work"

    @property
    def boot_dir(self) -> Path:
        return self.root / "boot"

    @property
    def downloads_dir(self) -> Path:
        return self.root / "downloads"

    @property
    def state_file(self) -> Path:
        return self.root / "project.json"

    def load_state(self) -> ProjectState:
        """Read ``project.json``; a missing file yields an empty state."""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ProjectState()
        known = {name: data[name] for name in ProjectState.__dataclass_fields__ if name in data}
        return ProjectState(**known)

    def save_state(self, state: ProjectState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
