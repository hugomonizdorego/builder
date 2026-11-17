"""Application controller (the "C" in MVC).

Owns the models, reacts to view signals and starts workers. Rule of the
house: anything that can take longer than a few milliseconds — network,
disk, subprocesses — runs on a worker thread and reports back via signals.
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QProcess, QSettings
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from ubuntu_builder import APP_ID
from ubuntu_builder.controllers.workers import (
    BaseWorker,
    PrivilegedWorker,
    TaskContext,
    TaskWorker,
    helper_command,
)
from ubuntu_builder.core import layers
from ubuntu_builder.core.backend import BuilderBackend, IsoInspection
from ubuntu_builder.core.customizer import Customizer
from ubuntu_builder.core.downloader import download_iso
from ubuntu_builder.core.errors import BuilderError
from ubuntu_builder.core.preflight import ToolStatus, check_host_tools
from ubuntu_builder.core.runner import CommandRunner
from ubuntu_builder.core.scraper import UbuntuScraper
from ubuntu_builder.models import Catalog, CustomizationConfig, IsoImage, ProjectState, Workspace
from ubuntu_builder.views.main_window import MainWindow
from ubuntu_builder.views.source_page import PreflightDialog

_OVMF_CANDIDATES = ("/usr/share/ovmf/OVMF.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd")


class MainController(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self._settings = QSettings(APP_ID, APP_ID)
        self._scraper = UbuntuScraper()
        self._catalog = Catalog()
        self._statuses: list[ToolStatus] = []
        self._task: BaseWorker | None = None  # the one foreground operation
        self._lookups: list[BaseWorker] = []  # short background queries
        self._image_request = 0  # discards replies to outdated image lookups
        default = Path.home() / "ubuntu-builder-workspace"
        self._workspace = Workspace(Path(str(self._settings.value("workspace", str(default)))))
        self._state = ProjectState()

        w = window
        w.cancel_requested.connect(self._cancel_task)
        w.closing.connect(self._on_close)
        w.source_page.workspace_changed.connect(self._set_workspace)
        w.source_page.refresh_requested.connect(self.refresh_catalog)
        w.source_page.selection_changed.connect(self._load_images)
        w.source_page.download_requested.connect(self._download_and_extract)
        w.source_page.extract_requested.connect(self._inspect_then_extract)
        w.source_page.delete_requested.connect(self._delete_workspace)
        w.source_page.preflight_details_requested.connect(self._show_preflight)
        w.terminal_page.open_requested.connect(self._open_terminal)
        w.terminal_page.external_requested.connect(self._open_external_terminal)
        w.terminal_page.terminal.session_finished.connect(lambda _code: self._refresh_project())
        w.customize_page.apply_requested.connect(self._apply_customizations)
        w.build_page.build_requested.connect(self._build)
        w.build_page.test_requested.connect(self._test_iso)

    def start(self) -> None:
        """Called once the window is shown."""
        self._run_preflight()
        self._refresh_project()
        self.refresh_catalog()

    # -- generic task plumbing ---------------------------------------------------

    @property
    def _busy(self) -> bool:
        return self._task is not None

    def _start_task(
        self, worker: BaseWorker, stage: str, on_success: Callable[[Any], None] | None = None
    ) -> None:
        """Run ``worker`` as the single foreground operation."""
        if self._busy:
            QMessageBox.information(self._window, "Busy", "Another operation is still running.")
            return
        self._task = worker
        worker.log.connect(self._window.append_log)
        worker.progress.connect(self._window.set_progress)

        def finished_ok(result: Any) -> None:
            self._end_task(f"{stage} — done")
            if on_success is not None:
                on_success(result)

        def finished_error(message: str, cancelled: bool) -> None:
            self._end_task(f"{stage} — {'cancelled' if cancelled else 'failed'}")
            self._window.append_log(("Cancelled: " if cancelled else "ERROR: ") + message)
            if not cancelled:
                QMessageBox.critical(self._window, stage, message)

        worker.succeeded.connect(finished_ok)
        worker.failed.connect(finished_error)
        worker.finished.connect(worker.deleteLater)
        self._window.set_busy(True, stage)
        self._update_actions()
        worker.start()

    def _end_task(self, stage: str) -> None:
        self._task = None
        self._window.set_busy(False, stage)
        self._refresh_project()

    def _cancel_task(self) -> None:
        if self._task is not None:
            self._window.stage_label.setText("Cancelling…")
            self._task.cancel()

    def _start_lookup(self, func: Callable[[TaskContext], Any], on_success: Callable[[Any], None],
                      on_error: Callable[[str], None]) -> None:  # fmt: skip
        """Background query that does not block other operations."""
        worker = TaskWorker(func)
        self._lookups.append(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda message, _cancelled: on_error(message))
        worker.finished.connect(lambda: self._lookups.remove(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # -- project state -------------------------------------------------------------

    def _run_preflight(self) -> None:
        self._statuses = check_host_tools()
        self._window.source_page.set_preflight(self._statuses)

    def _show_preflight(self) -> None:
        self._run_preflight()  # the user may have installed packages meanwhile
        PreflightDialog(self._statuses, self._window).exec()
        self._update_actions()

    def _tool_found(self, name: str) -> bool:
        return any(s.found for s in self._statuses if s.tool.name == name)

    def _set_workspace(self, path: Path) -> None:
        if self._busy or self._window.terminal_page.terminal.is_running:
            QMessageBox.information(self._window, "Busy", "Finish the running operation or session first.")
            return
        self._workspace = Workspace(path)
        self._settings.setValue("workspace", str(path))
        self._refresh_project()

    def _refresh_project(self) -> None:
        """Re-read ``project.json`` and update every page."""
        try:
            self._state = self._workspace.load_state()
        except (OSError, ValueError, TypeError) as exc:
            self._state = ProjectState()
            self._window.append_log(f"Could not read the workspace state: {exc}")
        w = self._window
        w.source_page.set_workspace(self._workspace.root, self._state)
        if self._state.extracted:
            name = (self._state.volume_id or "custom-ubuntu").lower().replace(" ", "-")
            w.build_page.set_defaults(Path.home() / f"{name}-custom.iso", self._state.volume_id)
            themes = self._workspace.edit_dir / "usr/share/plymouth/themes"
            try:
                found = sorted(p.name for p in themes.iterdir() if (p / f"{p.name}.plymouth").is_file())
            except OSError:
                found = []
            w.customize_page.set_plymouth_themes(found)
        self._update_actions()

    def _update_actions(self) -> None:
        w = self._window
        session_open = w.terminal_page.terminal.is_running
        ready = self._state.extracted and not self._busy and not session_open
        w.source_page.set_busy(self._busy or session_open)
        w.terminal_page.set_available(self._state.extracted and not self._busy)
        w.customize_page.apply_button.setEnabled(ready)
        w.build_page.set_state(
            can_build=ready,
            qemu=self._tool_found("qemu"),
            ovmf=any(Path(p).exists() for p in _OVMF_CANDIDATES),
        )

    # -- catalog ----------------------------------------------------------------------

    def refresh_catalog(self) -> None:
        self._window.source_page.catalog_label.setText("Loading release information from ubuntu.com…")
        self._start_lookup(lambda _ctx: self._scraper.fetch_catalog(), self._on_catalog, self._on_catalog_error)

    def _on_catalog(self, catalog: Catalog) -> None:
        self._catalog = catalog
        fetched = f"{catalog.fetched_at:%Y-%m-%d %H:%M}" if catalog.fetched_at else "unknown"
        note = f"Release data from ubuntu.com, fetched {fetched}."
        if catalog.warnings:
            note += " " + " ".join(catalog.warnings)
        self._window.source_page.set_catalog(catalog.releases, catalog.flavors, note)

    def _on_catalog_error(self, message: str) -> None:
        self._window.source_page.catalog_label.setText(
            f"<span style='color:#c7162b'>Could not load release information: {message}</span> "
            "You can still use a local ISO file."
        )

    def _load_images(self) -> None:
        page = self._window.source_page
        release, flavor = page.current_release(), page.current_flavor()
        if release is None or flavor is None:
            return
        self._image_request += 1
        request = self._image_request
        page.set_images_loading()

        def done(images: list[IsoImage]) -> None:
            if request == self._image_request:  # ignore answers to older selections
                page.set_images(images, f"No {flavor.name} images for {release.version}")

        def failed(message: str) -> None:
            if request == self._image_request:
                page.set_images([], "Not available")
                self._window.append_log(message)

        self._start_lookup(lambda _ctx: self._scraper.fetch_iso_images(release, flavor), done, failed)

    # -- download & extract ---------------------------------------------------------------

    def _download_and_extract(self, image: IsoImage) -> None:
        downloads = self._workspace.downloads_dir

        def task(ctx: TaskContext) -> Path:
            def progress(done: int, total: int) -> None:
                if total:
                    stage = f"Downloading {image.filename} ({done / 1024**2:.0f} / {total / 1024**2:.0f} MiB)"
                    ctx.progress(int(done * 100 / total), stage)

            return download_iso(image, downloads, progress=progress, log=ctx.log, cancel_event=ctx.cancel_event)

        self._start_task(TaskWorker(task), f"Download {image.filename}", self._inspect_then_extract)

    def _inspect_then_extract(self, iso_path: Path) -> None:
        """Look into the ISO (background), then ask which layer to edit."""
        if not iso_path.is_file():
            QMessageBox.warning(self._window, "Extract", f"File not found: {iso_path}")
            return

        def task(ctx: TaskContext) -> IsoInspection:
            return BuilderBackend(self._workspace, log=ctx.log, cancel_event=ctx.cancel_event).inspect_iso(iso_path)

        self._start_task(TaskWorker(task), "Inspect ISO", lambda inspection: self._extract(iso_path, inspection))

    def _extract(self, iso_path: Path, inspection: IsoInspection) -> None:
        target = inspection.default_target
        if inspection.is_layered and len(inspection.squashfs) > 1:
            names = list(inspection.squashfs)
            labels = [layers.describe_layer(name, names) for name in names]
            default = names.index(target) if target in names else 0
            choice, accepted = QInputDialog.getItem(
                self._window,
                "Choose the file system to customise",
                "This image is layered. Changes are stored in one layer; pick the one that is installed\n"
                "to disk (the installer's default is preselected):",
                labels, default, False,
            )  # fmt: skip
            if not accepted:
                return
            target = names[labels.index(choice)]
        arguments = ["extract", str(iso_path)]
        if target:
            arguments += ["--layer", target]
        if self._state.extracted:
            answer = QMessageBox.question(
                self._window, "Replace image?",
                "The workspace already holds an extracted image. Delete it (including all "
                "changes made to it) and extract the new one?",
            )  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                return
            arguments.append("--replace")
        self._start_task(PrivilegedWorker(self._workspace.root, arguments), "Extract ISO")

    def _delete_workspace(self) -> None:
        answer = QMessageBox.question(
            self._window, "Delete extracted image?",
            "This deletes the extracted image and every change made to it. Downloaded ISO files are kept.",
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Yes:
            self._start_task(PrivilegedWorker(self._workspace.root, ["delete"]), "Delete extracted image")

    # -- terminal ---------------------------------------------------------------------------

    def _open_terminal(self) -> None:
        if self._busy:
            return
        command = helper_command(self._workspace.root, ["shell"], events=False, extra_env={"TERM": "dumb"})
        self._window.terminal_page.terminal.start(command)
        self._update_actions()

    def _open_external_terminal(self) -> None:
        emulator = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole")
        if emulator is None:
            QMessageBox.warning(self._window, "Terminal", "No terminal emulator found on this system.")
            return
        command = helper_command(self._workspace.root, ["shell"], events=False, extra_env={"TERM": "xterm-256color"})
        separator = "--" if emulator.endswith("gnome-terminal") else "-e"
        if not QProcess.startDetached(emulator, [separator, *command]):
            QMessageBox.warning(self._window, "Terminal", f"Could not start {emulator}.")
        else:
            self._window.append_log("Chroot session opened in an external terminal: " + shlex.join(command))

    # -- customize & build ---------------------------------------------------------------------

    def _apply_customizations(self, config: CustomizationConfig) -> None:
        if config == CustomizationConfig():
            QMessageBox.information(self._window, "Customize", "Nothing to apply — the form is empty.")
            return
        try:  # cheap validation up front; the helper validates again as root
            Customizer(self._workspace, CommandRunner()).validate(config)
        except BuilderError as exc:
            QMessageBox.warning(self._window, "Customize", str(exc))
            return
        with tempfile.NamedTemporaryFile("w", suffix=".json", prefix=f"{APP_ID}-", delete=False) as handle:
            json.dump(config.to_dict(), handle)
            config_file = Path(handle.name)
        worker = PrivilegedWorker(self._workspace.root, ["customize", str(config_file)])
        worker.finished.connect(lambda: config_file.unlink(missing_ok=True))
        self._start_task(worker, "Apply customizations")

    def _build(self, output: Path, volume_id: str, compression: str) -> None:
        if not output.name:
            QMessageBox.warning(self._window, "Build", "Choose an output file first.")
            return
        if output.exists():
            answer = QMessageBox.question(self._window, "Overwrite?", f"{output} exists. Overwrite it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        arguments = ["build", str(output)]
        if volume_id:
            arguments += ["--volume-id", volume_id]
        if compression:
            arguments += ["--compression", compression]

        def done(result: dict[str, Any]) -> None:
            QMessageBox.information(self._window, "Build", f"ISO written to {result.get('output', output)}")

        self._start_task(PrivilegedWorker(self._workspace.root, arguments), "Build ISO", done)

    def _test_iso(self, iso_path: Path, uefi: bool) -> None:
        if not iso_path.is_file():
            QMessageBox.warning(self._window, "Test", f"Build the ISO first — {iso_path} does not exist.")
            return
        arguments = ["-m", "4096", "-smp", "2", "-cdrom", str(iso_path), "-boot", "d", "-vga", "virtio"]
        if Path("/dev/kvm").exists():
            arguments += ["-enable-kvm", "-cpu", "host"]
        if uefi:
            firmware = next((p for p in _OVMF_CANDIDATES if Path(p).exists()), None)
            if firmware is None:
                QMessageBox.warning(self._window, "Test", "OVMF firmware not found (install the ovmf package).")
                return
            arguments += ["-bios", firmware]
        if not QProcess.startDetached("qemu-system-x86_64", arguments):
            QMessageBox.warning(self._window, "Test", "Could not start qemu-system-x86_64.")

    # -- shutdown ---------------------------------------------------------------------------------

    def _on_close(self, event: QCloseEvent) -> None:
        terminal = self._window.terminal_page.terminal
        if self._busy or terminal.is_running:
            answer = QMessageBox.question(
                self._window, "Quit?",
                "An operation or chroot session is still running. Stop it and quit?",
            )  # fmt: skip
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if terminal.is_running:
                terminal.kill()
            if self._task is not None:
                # Closing stdin makes the root helper cancel and unmount; give
                # it a moment so no mounts are left behind.
                self._task.cancel()
                self._task.wait(15000)
        for worker in list(self._lookups):
            worker.wait(2000)
        event.accept()
