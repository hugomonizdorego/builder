"""High level remastering workflow.

:class:`BuilderBackend` strings the lower level modules together:
extract an ISO into a workspace, open a chroot session, apply
customizations and build the new hybrid ISO. It has no GUI dependency and
reports through three plain hooks — log lines, progress and a cancel event —
so it can be driven from a worker thread, the CLI or tests alike.

Most methods need root (unsquashfs has to recreate device nodes and
ownership, and mounting is privileged). The GUI therefore runs them through
``os_builder.cli`` under pkexec; see ``controllers/workers.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from os_builder.core import iso, layers
from os_builder.core.chroot import ChrootSession, mount_points_under, unmount_tree
from os_builder.core.customizer import Customizer
from os_builder.core.errors import BuilderError, PreflightError, WorkspaceError
from os_builder.core.preflight import check_host_tools, install_command, missing_required
from os_builder.core.runner import CommandRunner, parse_squashfs_progress
from os_builder.core.system import give_to_invoking_user, require_free_space
from os_builder.models.customization import CustomizationConfig
from os_builder.models.workspace import ProjectState, Workspace

LogCallback = Callable[[str], None]
StageProgress = Callable[[int, str], None]  # overall percent, stage description

COMPRESSIONS = ("xz", "zstd", "gzip", "lz4")
_GIB = 1024**3


@dataclass(frozen=True, slots=True)
class IsoInspection:
    """What can be learned from an ISO before extracting it."""

    volume_id: str
    squashfs: dict[str, int]  # relative path → compressed size
    default_target: str | None

    @property
    def is_layered(self) -> bool:
        return layers.CLASSIC_SQUASHFS not in self.squashfs and len(self.squashfs) > 0


class BuilderBackend:
    """Facade over the remastering workflow for one workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        log: LogCallback | None = None,
        progress: StageProgress | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.workspace = workspace
        self._log = log or (lambda _line: None)
        self._progress = progress or (lambda _percent, _stage: None)
        self.runner = CommandRunner(self._log, cancel_event)

    # -- helpers -----------------------------------------------------------

    def _stage(self, start: int, end: int, name: str) -> Callable[[float], None]:
        """Map a step's own 0–100 % onto its slice of the overall progress."""
        self._log(f"== {name} ==")
        self._progress(start, name)
        last = start

        def update(percent: float) -> None:
            nonlocal last
            value = start + int((end - start) * max(0.0, min(percent, 100.0)) / 100.0)
            if value != last:  # do not flood the GUI with identical values
                last = value
                self._progress(value, name)

        return update

    @staticmethod
    def require_root() -> None:
        if os.geteuid() != 0:
            raise BuilderError("This operation needs administrator rights.")

    @staticmethod
    def require_host_tools() -> None:
        statuses = check_host_tools()
        missing = missing_required(statuses)
        if missing:
            names = ", ".join(s.tool.name for s in missing)
            raise PreflightError(f"Missing host tools: {names}. Run: {install_command(statuses)}")

    # -- inspection (no root needed) ----------------------------------------

    def inspect_iso(self, iso_path: Path) -> IsoInspection:
        """Look into an ISO without extracting it."""
        if not iso_path.is_file():
            raise WorkspaceError(f"ISO file not found: {iso_path}")
        squashfs = iso.list_squashfs(self.runner, iso_path)
        if not squashfs:
            raise WorkspaceError(
                f"{iso_path.name} contains no casper/*.squashfs — it is not an Ubuntu live image."
            )
        sources = iso.read_iso_file(self.runner, iso_path, layers.INSTALL_SOURCES)
        return IsoInspection(
            volume_id=iso.read_volume_id(self.runner, iso_path),
            squashfs=squashfs,
            default_target=layers.default_target(list(squashfs), sources),
        )

    # -- extraction ----------------------------------------------------------

    def extract_iso(self, iso_path: Path, target_squashfs: str | None = None) -> ProjectState:
        """Unpack ``iso_path`` into the workspace.

        ``target_squashfs`` selects the file system (layer) to customise;
        by default the one :func:`layers.default_target` recommends.
        """
        self.require_root()
        self.require_host_tools()
        ws = self.workspace
        if ws.load_state().extracted or ws.iso_dir.exists() or ws.edit_dir.exists():
            raise WorkspaceError(
                f"{ws.root} already holds an extracted image. Delete the workspace first."
            )
        inspection = self.inspect_iso(iso_path)
        target = target_squashfs or inspection.default_target
        if target not in inspection.squashfs:
            raise WorkspaceError(f"{target} does not exist in {iso_path.name}.")

        # Space needed: the ISO tree plus the unpacked squashfs. The latter
        # is estimated from its compressed size (xz/zstd reach roughly 3:1).
        required = int(iso_path.stat().st_size * 1.05) + inspection.squashfs[target] * 4
        require_free_space(ws.root, required)
        self._log(f"Free disk space check passed ({required / _GIB:.1f} GiB needed).")

        ws.root.mkdir(parents=True, exist_ok=True)
        state = ProjectState(source_iso=str(iso_path), volume_id=inspection.volume_id)
        try:
            iso.extract_tree(self.runner, iso_path, ws.iso_dir, self._stage(0, 40, "Extracting ISO contents"))

            self._stage(40, 45, "Saving boot records")
            state.boot_options = iso.analyze_boot(self.runner, iso_path, ws.boot_dir)
            if not iso.has_boot_record(state.boot_options):
                self._log("WARNING: the source ISO has no boot record; one will be generated at build time.")

            state.target_squashfs = target
            state.lower_squashfs = layers.lower_layers(target, list(inspection.squashfs))
            self._unsquash(ws.iso_dir / target, ws.edit_dir, self._stage(45, 100, f"Unpacking {target}"))
        except BaseException:
            # Never leave a half extracted workspace behind: it would be
            # mistaken for a valid one the next time.
            self._log("Extraction did not finish — cleaning up.")
            self._remove_workspace_dirs()
            raise
        state.extracted = True
        ws.save_state(state)
        self._log(f"Boot support: {iso.boot_report(state.boot_options, ws.iso_dir).summary()}")
        os.chmod(ws.root, 0o755)
        self._progress(100, "Extraction finished")
        return state

    def _unsquash(self, squashfs: Path, dest: Path, progress: Callable[[float], None]) -> None:
        def on_line(line: str) -> None:
            percent = parse_squashfs_progress(line)
            if percent is not None:
                progress(percent)

        # A pty makes unsquashfs draw its progress bar; quiet=True keeps the
        # thousands of redraws out of the log.
        self.runner.log(f"$ unsquashfs -f -d {dest} {squashfs}")
        self.runner.run(["unsquashfs", "-f", "-d", dest, squashfs], on_line=on_line, use_pty=True, quiet=True)

    # -- chroot ----------------------------------------------------------------

    def _extracted_state(self) -> ProjectState:
        state = self.workspace.load_state()
        if not state.extracted:
            raise WorkspaceError("No extracted image in this workspace yet. Extract an ISO first.")
        return state

    @contextmanager
    def session(self) -> Iterator[ChrootSession]:
        """Mounted chroot session; always unmounted again on exit."""
        self.require_root()
        with ChrootSession(self.workspace, self._extracted_state(), self.runner) as session:
            yield session

    def apply_customizations(self, config: CustomizationConfig) -> None:
        customizer = Customizer(self.workspace, self.runner)
        customizer.validate(config)  # before anything gets mounted
        self._progress(0, "Applying customizations")
        with self.session() as session:
            customizer.apply(config, session)
        self._progress(100, "Customizations applied")

    # -- build -------------------------------------------------------------------

    def source_compression(self, squashfs: Path) -> str:
        """Compression algorithm of an existing squashfs (default: xz)."""
        output = self.runner.capture(["unsquashfs", "-s", squashfs], check=False)
        match = re.search(r"^Compression\s+(\w+)", output, re.MULTILINE)
        return match.group(1) if match and match.group(1) in COMPRESSIONS else "xz"

    def boot_report(self) -> iso.BootReport:
        state = self._extracted_state()
        return iso.boot_report(state.boot_options, self.workspace.iso_dir)

    def build_iso(
        self, output: Path, volume_id: str | None = None, compression: str | None = None, boot_mode: str = "hybrid"
    ) -> Path:
        """Repack the edited file system and build the ISO.

        ``boot_mode`` "hybrid" boots on legacy BIOS and UEFI, "uefi" on UEFI
        only (no BIOS boot code; for modern PCs without a CSM).
        """
        if boot_mode not in iso.BOOT_MODES:
            raise BuilderError(f"Unknown boot mode: {boot_mode}")
        self.require_root()
        self.require_host_tools()
        ws = self.workspace
        state = self._extracted_state()
        target = ws.iso_dir / state.target_squashfs
        if compression is not None and compression not in COMPRESSIONS:
            raise BuilderError(f"Unsupported compression: {compression}")
        compression = compression or (self.source_compression(target) if target.exists() else "xz")
        output = output.absolute()
        if ws.iso_dir.resolve() in output.resolve().parents:
            raise BuilderError("The output ISO must not be placed inside the ISO tree.")

        # Worst case: new squashfs as large as the old ISO, plus the ISO itself.
        iso_tree = sum(p.stat().st_size for p in ws.iso_dir.rglob("*") if p.is_file() and not p.is_symlink())
        require_free_space(ws.root, iso_tree // 2)
        require_free_space(output.parent, int(iso_tree * 1.1))

        self._stage(0, 5, "Cleaning up the image")
        with self.session() as session:
            session.run(["apt-get", "clean"], check=False)
            for leftover in ("root/.bash_history", "var/lib/apt/periodic/update-success-stamp"):
                (session.rootfs / leftover).unlink(missing_ok=True)
            if not state.is_layered:
                self._write_manifest(session, target.with_name("filesystem.manifest"))

        new_squashfs = target.with_name(target.name + ".new")
        try:
            self._mksquashfs(ws.edit_dir, new_squashfs, compression, self._stage(5, 70, "Compressing file system"))
            os.replace(new_squashfs, target)
        finally:
            new_squashfs.unlink(missing_ok=True)
        self._write_size_file(target)
        # The original signature no longer matches the repacked file system.
        target.with_name(target.name + ".gpg").unlink(missing_ok=True)

        boot_options = state.boot_options
        if not iso.has_boot_record(boot_options):
            boot_options = iso.regenerate_boot_images(self.runner, ws.iso_dir, ws.boot_dir)
        if boot_mode == "uefi":
            boot_options = iso.uefi_only_options(boot_options)
            self._log("Boot mode: UEFI only (legacy BIOS boot code left out).")

        self._stage(70, 80, "Updating md5sum.txt")
        count = iso.write_md5sums(ws.iso_dir, iso.boot_image_paths(boot_options), self.runner)
        self._log(f"md5sum.txt regenerated ({count} files).")

        output.parent.mkdir(parents=True, exist_ok=True)
        iso.build_iso(
            self.runner, ws.iso_dir, output, volume_id or state.volume_id or "Custom Ubuntu",
            boot_options, ws.boot_dir,
            self._stage(80, 100, "Building UEFI ISO" if boot_mode == "uefi" else "Building hybrid BIOS/UEFI ISO"),
        )  # fmt: skip
        report = iso.boot_report(boot_options, ws.iso_dir)
        self._log(f"Boot support: {report.summary()}")
        give_to_invoking_user(output)
        self._progress(100, "ISO finished")
        self._log(f"ISO written to {output} ({output.stat().st_size / _GIB:.2f} GiB).")
        return output

    def _mksquashfs(self, source: Path, dest: Path, compression: str, progress: Callable[[float], None]) -> None:
        def on_line(line: str) -> None:
            percent = parse_squashfs_progress(line)
            if percent is not None:
                progress(percent)

        command = ["mksquashfs", source, dest, "-noappend", "-comp", compression, "-b", "1M"]
        self.runner.log("$ " + " ".join(str(part) for part in command))
        self.runner.run(command, on_line=on_line, use_pty=True, quiet=True)
        self._log(f"{dest.name.removesuffix('.new')}: {dest.stat().st_size / 1024**2:.0f} MiB ({compression}).")

    def _write_manifest(self, session: ChrootSession, manifest: Path) -> None:
        """Refresh the package list the installer and release tools read."""
        if not manifest.exists():
            return
        packages = session.capture(["dpkg-query", "-W", "--showformat=${Package}\\t${Version}\\n"])
        manifest.write_text(packages + "\n", encoding="utf-8")

    def _write_size_file(self, squashfs: Path) -> None:
        """Update the unpacked-size metadata the installer plans with."""
        usage = self.runner.capture(["du", "-sx", "--block-size=1", self.workspace.edit_dir])
        new_size = int(usage.split()[0])
        size_file = squashfs.with_suffix(".size")
        old_size: int | None = None
        if size_file.exists():
            try:
                old_size = int(size_file.read_text(encoding="utf-8").split()[0])
            except (ValueError, IndexError):
                old_size = None
            size_file.write_text(f"{new_size}\n", encoding="utf-8")
        if old_size is not None:
            self._update_install_sources(squashfs, new_size - old_size)

    def _update_install_sources(self, squashfs: Path, delta: int) -> None:
        """Adjust Subiquity's ``install-sources.yaml`` after a layer changed.

        Its sizes are cumulative, so every source stacked on the edited
        layer grows (or shrinks) by the same amount.
        """
        sources_file = squashfs.with_name("install-sources.yaml")
        if delta == 0 or not sources_file.is_file():
            return
        try:
            document = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return
        entries = document.get("sources") if isinstance(document, dict) else document
        if not isinstance(entries, list):
            return
        changed = False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("size"), int):
                continue
            path = str(entry.get("path", ""))
            stack = [path, *(Path(p).name for p in layers.lower_layers(path, [squashfs.name]))]
            if squashfs.name in stack:
                entry["size"] = max(0, entry["size"] + delta)
                changed = True
        if changed:
            sources_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            self._log("install-sources.yaml sizes updated.")

    # -- maintenance -------------------------------------------------------------

    def cleanup_mounts(self) -> None:
        """Unmount anything a crashed session left behind."""
        self.require_root()
        unmount_tree(self.runner, self.workspace.root)

    def _remove_workspace_dirs(self) -> None:
        ws = self.workspace
        unmount_tree(self.runner, ws.root)
        if mount_points_under(ws.root):  # never rm -rf through a bind mount of /dev
            raise WorkspaceError("Refusing to delete: the workspace still has active mounts.")
        for path in (ws.iso_dir, ws.edit_dir, ws.rootfs_dir, ws.lower_dir, ws.overlay_work_dir, ws.boot_dir):
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        ws.state_file.unlink(missing_ok=True)
        (ws.root / ".session.lock").unlink(missing_ok=True)

    def delete_workspace(self) -> None:
        """Remove the extracted image. Downloaded ISOs are kept."""
        self.require_root()
        ws = self.workspace
        if not ws.state_file.exists() and not ws.iso_dir.exists() and not ws.edit_dir.exists():
            raise WorkspaceError(f"{ws.root} is not an OS Builder workspace.")
        self._log(f"Deleting extracted image in {ws.root}")
        self._remove_workspace_dirs()
