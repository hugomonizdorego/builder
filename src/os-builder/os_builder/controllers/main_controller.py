"""Application controller (the "C" in MVC).

Owns the models, reacts to view signals and starts workers. Rule of the
house: anything that can take longer than a few milliseconds — network,
disk, subprocesses — runs on a worker thread and reports back via signals.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QProcess, QSettings, QStandardPaths, QUrl
from PyQt6.QtGui import QCloseEvent, QDesktopServices
from PyQt6.QtWidgets import QDialog, QFileDialog, QInputDialog, QMessageBox

from os_builder import APP_ID
from os_builder.controllers.workers import (
    BaseWorker,
    PrivilegedWorker,
    TaskContext,
    TaskWorker,
    helper_command,
)
from os_builder.core import layers
from os_builder.core.backend import BuilderBackend, IsoInspection
from os_builder.core.customizer import Customizer
from os_builder.core.downloader import download_iso
from os_builder.core.errors import BuilderError
from os_builder.core.iso import boot_report
from os_builder.core.preflight import ToolStatus, check_host_tools, missing_required
from os_builder.core.runner import CommandRunner
from os_builder.core.scraper import UbuntuScraper
from os_builder.models import DESKTOPS_BY_ID, CustomizationConfig, IsoImage, ProjectState, Workspace
from os_builder.views.dialogs import BuildDialog, ConsoleDialog, SettingsDialog, WizardDialog
from os_builder.views.get_iso_dialog import GetIsoDialog
from os_builder.views.main_window import MainWindow

_OVMF_SECURE = ("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd")
_OVMF_CANDIDATES = ("/usr/share/ovmf/OVMF.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_CODE.fd")

#: (program, arguments before the ISO path) — first one installed wins.
USB_WRITERS = (
    ("gnome-disks", ["--restore-disk-image"]),
    ("usb-creator-gtk", ["-i"]),
    ("usb-creator-kde", ["-i"]),
    ("mintstick", ["-m", "iso", "-i"]),
    ("isoimagewriter", []),
)
DISC_BURNERS = (("brasero", ["--image"]), ("k3b", ["--image"]), ("xfburn", ["-i"]))


def describe_disk(info: str) -> str:
    """Shorten ``.disk/info``: 'Ubuntu 26.04.1 LTS "Resolute Raccoon" - Release amd64 (20260826)'."""
    return re.sub(r"\s*\(\d{8}(?:\.\d+)?\)\s*$", "", info.strip())


class MainController(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self._settings = QSettings(APP_ID, APP_ID)
        self._scraper = UbuntuScraper()
        self._statuses: list[ToolStatus] = []
        self._task: BaseWorker | None = None  # the one foreground operation
        self._lookups: list[BaseWorker] = []  # short background queries
        self._image_request = 0  # discards replies to outdated image lookups
        default = Path.home() / "os-builder-workspace"
        self._workspace = Workspace(Path(str(self._settings.value("workspace", str(default)))))
        self._state = ProjectState()
        self._iso_dialog = GetIsoDialog(window)
        self._console: ConsoleDialog | None = None

        w = window
        w.set_toolbar_style(str(self._settings.value("toolbar", "dark")))
        w.cancel_requested.connect(self._cancel_task)
        w.closing.connect(self._on_close)
        w.select_button.clicked.connect(self._iso_dialog.show)
        w.wizard_button.clicked.connect(self._open_wizard)
        w.build_button.clicked.connect(self._open_build)
        w.qemu_bios_action.triggered.connect(lambda: self._test_iso(self._last_iso(), uefi=False))
        w.qemu_uefi_action.triggered.connect(lambda: self._test_iso(self._last_iso(), uefi=True))
        w.qemu_secure_action.triggered.connect(
            lambda: self._test_iso(self._last_iso(), uefi=True, secure_boot=True)
        )
        w.qemu_other_action.triggered.connect(self._test_other_iso)
        w.export_folder_action.triggered.connect(self._show_in_folder)
        w.export_usb_action.triggered.connect(lambda: self._export_with(USB_WRITERS, "USB writer"))
        w.export_burn_action.triggered.connect(lambda: self._export_with(DISC_BURNERS, "disc burning program"))
        w.remove_button.clicked.connect(self._delete_workspace)
        w.settings_button.clicked.connect(self._open_settings)
        w.console_button.clicked.connect(self._open_console)
        w.synaptic_button.clicked.connect(self._run_synaptic)
        w.desktop_button.clicked.connect(self._run_desktop)
        w.deb_button.clicked.connect(self._install_debs)
        w.desktop_install_button.clicked.connect(self._install_desktop)

        d = self._iso_dialog
        d.refresh_requested.connect(self.refresh_catalog)
        d.selection_changed.connect(self._load_images)
        d.download_requested.connect(self._download_and_extract)
        d.extract_requested.connect(self._inspect_then_extract)

    def start(self) -> None:
        """Called once the window is shown."""
        self._load_distribution()
        self._run_preflight()
        self._refresh_project()
        self.refresh_catalog()
        missing = missing_required(self._statuses)
        if missing:
            self._window.append_log(
                "Missing host tools: " + ", ".join(s.tool.name for s in missing) + " — see Settings."
            )
            QMessageBox.warning(
                self._window, "Host requirements",
                "Some required tools are not installed: "
                + ", ".join(s.tool.name for s in missing)
                + ".\nOpen Settings to see how to install them.",
            )  # fmt: skip

    # -- generic task plumbing ---------------------------------------------------

    @property
    def _busy(self) -> bool:
        return self._task is not None or (self._console is not None and self._console.page.terminal.is_running)

    def _start_task(self, worker: BaseWorker, stage: str, on_success: Callable[[Any], None] | None = None) -> None:
        """Run ``worker`` as the single foreground operation."""
        if self._busy:
            QMessageBox.information(self._window, "Busy", "Another operation or the console is still running.")
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
        self._window.append_log(f"\n=== {stage} ===")
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

    def _start_lookup(
        self, func: Callable[[TaskContext], Any], on_success: Callable[[Any], None], on_error: Callable[[str], None]
    ) -> None:
        """Background query that does not block other operations."""
        worker = TaskWorker(func)
        self._lookups.append(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda message, _cancelled: on_error(message))
        worker.finished.connect(lambda: self._lookups.remove(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _privileged(self, arguments: list[str], stage: str, on_success: Callable[[Any], None] | None = None,
                    temp_file: Path | None = None) -> None:  # fmt: skip
        worker = PrivilegedWorker(self._workspace.root, arguments)
        if temp_file is not None:
            worker.finished.connect(lambda: temp_file.unlink(missing_ok=True))
        self._start_task(worker, stage, on_success)

    @staticmethod
    def _write_config(config: CustomizationConfig) -> Path:
        """Hand a configuration to the root helper through a private temp file."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", prefix=f"{APP_ID}-", delete=False) as handle:
            json.dump(config.to_dict(), handle)
        return Path(handle.name)

    # -- project state -------------------------------------------------------------

    def _run_preflight(self) -> list[ToolStatus]:
        self._statuses = check_host_tools()
        return self._statuses

    def _tool_found(self, name: str) -> bool:
        return any(s.found for s in self._statuses if s.tool.name == name)

    def _distribution_key(self) -> str:
        return f"distribution/{self._workspace.root}"

    def _save_distribution(self) -> None:
        self._settings.setValue(self._distribution_key(), json.dumps(self._window.distribution_config().to_dict()))

    def _load_distribution(self) -> None:
        raw = self._settings.value(self._distribution_key(), "")
        try:
            values = json.loads(str(raw)) if raw else {}
        except ValueError:
            values = {}
        self._window.load_distribution(values)

    def _set_workspace(self, path: Path) -> None:
        if self._busy:
            QMessageBox.information(self._window, "Busy", "Finish the running operation first.")
            return
        self._save_distribution()
        self._workspace = Workspace(path)
        self._settings.setValue("workspace", str(path))
        self._load_distribution()
        self._refresh_project()

    def _refresh_project(self) -> None:
        """Re-read ``project.json`` and update the window."""
        try:
            self._state = self._workspace.load_state()
        except (OSError, ValueError, TypeError) as exc:
            self._state = ProjectState()
            self._window.append_log(f"Could not read the workspace state: {exc}")
        if self._state.extracted:
            try:
                info = describe_disk((self._workspace.iso_dir / ".disk" / "info").read_text(encoding="utf-8"))
            except OSError:
                info = self._state.volume_id or "Ubuntu image"
            report = boot_report(self._state.boot_options, self._workspace.iso_dir)
            text = (
                f"<b>{info}</b><br>ISO: {Path(self._state.source_iso).name}<br>"
                f"Customising: <code>{self._state.target_squashfs}</code><br>"
                f"Boot: {report.summary()}"
            )
        else:
            text = "<b>No ISO extracted.</b><br>Click <i>Select ISO</i> to start."
        self._window.set_project_info(text)
        self._update_actions()

    def _update_actions(self) -> None:
        self._window.set_actions_enabled(
            extracted=self._state.extracted,
            idle=not self._busy,
            built=self._last_iso() is not None,
            qemu=self._tool_found("qemu"),
        )

    def _last_iso(self) -> Path | None:
        value = self._settings.value("last_iso", "")
        path = Path(str(value)) if value else None
        return path if path is not None and path.is_file() else None

    # -- Select ISO ----------------------------------------------------------------------

    def refresh_catalog(self) -> None:
        self._iso_dialog.catalog_label.setText("Loading release information from ubuntu.com…")
        self._start_lookup(
            lambda _ctx: self._scraper.fetch_catalog(), self._on_catalog, self._iso_dialog.set_catalog_error
        )

    def _on_catalog(self, catalog: Any) -> None:
        fetched = f"{catalog.fetched_at:%Y-%m-%d %H:%M}" if catalog.fetched_at else "unknown"
        note = f"Release data from ubuntu.com, fetched {fetched}."
        if catalog.warnings:
            note += " " + " ".join(catalog.warnings)
        self._iso_dialog.set_catalog(catalog.releases, catalog.flavors, note)

    def _load_images(self) -> None:
        dialog = self._iso_dialog
        release, flavor = dialog.current_release(), dialog.current_flavor()
        if release is None or flavor is None:
            return
        self._image_request += 1
        request = self._image_request
        dialog.set_images_loading()

        def done(images: list[IsoImage]) -> None:
            if request == self._image_request:  # ignore answers to older selections
                dialog.set_images(images, f"No {flavor.name} images for {release.version}")

        def failed(message: str) -> None:
            if request == self._image_request:
                dialog.set_images([], "Not available")
                self._window.append_log(message)

        self._start_lookup(lambda _ctx: self._scraper.fetch_iso_images(release, flavor), done, failed)

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
            QMessageBox.warning(self._window, "Select ISO", f"File not found: {iso_path}")
            return

        def task(ctx: TaskContext) -> IsoInspection:
            return BuilderBackend(self._workspace, log=ctx.log, cancel_event=ctx.cancel_event).inspect_iso(iso_path)

        self._start_task(TaskWorker(task), "Checking ISO", lambda inspection: self._extract(iso_path, inspection))

    def _extract(self, iso_path: Path, inspection: IsoInspection) -> None:
        target = inspection.default_target
        if inspection.is_layered and len(inspection.squashfs) > 1:
            names = list(inspection.squashfs)
            labels = [layers.describe_layer(name, names) for name in names]
            default = names.index(target) if target in names else 0
            choice, accepted = QInputDialog.getItem(
                self._window, "Choose the file system to customise",
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
        self._privileged(arguments, "Extracting ISO")

    def _delete_workspace(self) -> None:
        answer = QMessageBox.question(
            self._window, "Remove",
            "Delete the extracted image and every change made to it? Downloaded ISO files are kept.",
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Yes:
            self._privileged(["delete"], "Removing extracted image")

    # -- Customize panel ------------------------------------------------------------------

    def _customize(self, config: CustomizationConfig, stage: str,
                   on_success: Callable[[Any], None] | None = None) -> None:  # fmt: skip
        try:  # cheap validation up front; the helper validates again as root
            Customizer(self._workspace, CommandRunner()).validate(config)
        except BuilderError as exc:
            QMessageBox.warning(self._window, stage, str(exc))
            return
        config_file = self._write_config(config)
        self._privileged(["customize", str(config_file)], stage, on_success, temp_file=config_file)

    def _open_console(self) -> None:
        if self._task is not None:
            return
        if self._console is None:
            self._console = ConsoleDialog(self._window)
            page = self._console.page
            page.set_available(True)
            page.open_requested.connect(self._start_console_session)
            page.external_requested.connect(self._open_external_terminal)
            page.terminal.session_finished.connect(lambda _code: self._refresh_project())
        self._console.show()
        self._console.raise_()
        if not self._console.page.terminal.is_running:
            self._start_console_session()

    def _start_console_session(self) -> None:
        assert self._console is not None
        command = helper_command(self._workspace.root, ["shell"], events=False, extra_env={"TERM": "dumb"})
        self._console.page.terminal.start(command)
        self._console.page.refresh_buttons()
        self._update_actions()

    def _open_external_terminal(self) -> None:
        emulator = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole")
        if emulator is None:
            QMessageBox.warning(self._window, "Console", "No terminal emulator found on this system.")
            return
        command = helper_command(self._workspace.root, ["shell"], events=False, extra_env={"TERM": "xterm-256color"})
        separator = "--" if emulator.endswith("gnome-terminal") else "-e"
        if not QProcess.startDetached(emulator, [separator, *command]):
            QMessageBox.warning(self._window, "Console", f"Could not start {emulator}.")

    def _run_synaptic(self) -> None:
        self._privileged(["synaptic"], "Synaptic")

    def _run_desktop(self) -> None:
        resolution = str(self._settings.value("resolution", "1280x800"))
        self._privileged(["desktop", "--resolution", resolution], "Desktop")

    def _install_debs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self._window, "Install .deb packages into the image", str(Path.home()), "Debian packages (*.deb)"
        )
        if files:
            self._customize(CustomizationConfig(local_debs=files), "Installing .deb packages")

    def _install_desktop(self) -> None:
        picker = self._window.desktop_picker
        desktop = DESKTOPS_BY_ID.get(picker.desktop_id())
        if desktop is None:
            return
        config = CustomizationConfig(desktop=desktop.id, display_manager=picker.display_manager_id())
        manager = Customizer.chosen_display_manager(config)
        login = f" with {manager.name}" if manager else ""
        answer = QMessageBox.question(
            self._window, "Desktop environment",
            f"Install {desktop.name}{login} into the image? This downloads a lot of packages.",
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Yes:
            self._customize(config, f"Installing {desktop.name}")

    # -- toolbar -------------------------------------------------------------------------------

    def _open_wizard(self) -> None:
        dialog = WizardDialog(self._window)
        edit = self._workspace.edit_dir / "usr/share/plymouth/themes"
        try:
            themes = sorted(p.name for p in edit.iterdir() if (p / f"{p.name}.plymouth").is_file())
        except OSError:
            themes = []
        dialog.form.set_plymouth_themes(themes)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        then = (lambda _result: self._open_build()) if dialog.build_after.isChecked() else None
        self._customize(dialog.config(), "Wizard", then)

    def _open_build(self) -> None:
        distribution = self._window.distribution_config()
        try:
            Customizer(self._workspace, CommandRunner()).validate(distribution)
        except BuilderError as exc:
            QMessageBox.warning(self._window, "Distribution", str(exc))
            return
        self._save_distribution()
        version = re.search(r"\d+\.\d+(?:\.\d+)*", self._state.volume_id)
        name = distribution.distro_name or "Ubuntu"
        file_stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", name).strip("-")  # "DebION GNU/Linux" → "DebION-GNU-Linux"
        default_name = "-".join(filter(None, [file_stem, version.group(0) if version else ""]))
        label_name = name.replace("/", "-")
        default_label = (
            f"{label_name} {version.group(0)}" if version and distribution.distro_name else self._state.volume_id
        )
        last = self._last_iso()
        output = (last.parent if last else Path.home()) / f"{default_name}-custom.iso"
        dialog = BuildDialog(output, default_label, self._window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        arguments = ["build", str(dialog.output())]
        if dialog.volume_id():
            arguments += ["--volume-id", dialog.volume_id()]
        if dialog.compression():
            arguments += ["--compression", dialog.compression()]
        arguments += ["--boot-mode", dialog.boot_mode()]
        config_file = None
        if not distribution.is_empty():
            config_file = self._write_config(distribution)
            arguments += ["--config", str(config_file)]

        def done(result: dict[str, Any]) -> None:
            built = str(result.get("output", dialog.output()))
            self._settings.setValue("last_iso", built)
            self._update_actions()
            QMessageBox.information(
                self._window, "Build", f"{Path(built).name} successfully created.\nYou can test it with QEMU."
            )

        self._privileged(arguments, "Building ISO", done, temp_file=config_file)

    def _test_other_iso(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self._window, "Test an ISO", str(Path.home()), "ISO images (*.iso)")
        if chosen:
            uefi = QMessageBox.question(self._window, "QEMU", "Boot in UEFI mode?") == QMessageBox.StandardButton.Yes
            self._test_iso(Path(chosen), uefi=uefi)

    def _test_iso(self, iso_path: Path | None, *, uefi: bool, secure_boot: bool = False) -> None:
        if iso_path is None or not iso_path.is_file():
            QMessageBox.warning(self._window, "QEMU", "Build an ISO first.")
            return
        arguments = ["-m", "4096", "-smp", "2", "-cdrom", str(iso_path), "-boot", "d", "-vga", "virtio",
                     "-name", f"OS Builder — {iso_path.name}"]  # fmt: skip
        if Path("/dev/kvm").exists():
            arguments += ["-enable-kvm", "-cpu", "host"]
        if secure_boot:
            code, template = Path(_OVMF_SECURE[0]), Path(_OVMF_SECURE[1])
            if not code.exists() or not template.exists():
                QMessageBox.warning(self._window, "QEMU", "Secure Boot firmware not found (install the ovmf package).")
                return
            # A private copy of the variable store with Microsoft's keys enrolled.
            variables = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
            variables.mkdir(parents=True, exist_ok=True)
            variables = variables / "OVMF_VARS_secureboot.fd"
            shutil.copyfile(template, variables)
            arguments += [
                "-machine", "q35,smm=on", "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive", f"if=pflash,format=raw,file={variables}",
            ]  # fmt: skip
        elif uefi:
            firmware = next((p for p in _OVMF_CANDIDATES if Path(p).exists()), None)
            if firmware is None:
                QMessageBox.warning(self._window, "QEMU", "OVMF firmware not found (install the ovmf package).")
                return
            arguments += ["-bios", firmware]
        if not QProcess.startDetached("qemu-system-x86_64", arguments):
            QMessageBox.warning(self._window, "QEMU", "Could not start qemu-system-x86_64.")

    def _show_in_folder(self) -> None:
        iso = self._last_iso()
        if iso is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(iso.parent)))

    def _export_with(self, programs: tuple[tuple[str, list[str]], ...], kind: str) -> None:
        iso = self._last_iso()
        if iso is None:
            return
        for program, arguments in programs:
            path = shutil.which(program)
            if path is not None:
                if arguments and arguments[-1].startswith("--") and arguments[-1].endswith("image"):
                    argv = [*arguments[:-1], f"{arguments[-1]}={iso}"]  # --restore-disk-image=ISO, --image=ISO
                else:
                    argv = [*arguments, str(iso)]
                if QProcess.startDetached(path, argv):
                    self._window.append_log(f"Started {program} with {iso.name}.")
                    return
        names = ", ".join(program for program, _ in programs)
        QMessageBox.information(self._window, "Export", f"No {kind} found. Install one of: {names}.")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._window)
        dialog.load(
            self._workspace.root,
            str(self._settings.value("toolbar", "dark")),
            str(self._settings.value("resolution", "1280x800")),
        )
        dialog.requirements.set_statuses(self._run_preflight())
        dialog.requirements.recheck_requested.connect(
            lambda: dialog.requirements.set_statuses(self._run_preflight())
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._settings.setValue("toolbar", dialog.toolbar())
        self._settings.setValue("resolution", dialog.resolution())
        self._window.set_toolbar_style(dialog.toolbar())
        chosen = Path(dialog.workspace_edit.text())
        if chosen != self._workspace.root:
            self._set_workspace(chosen)
        self._update_actions()

    # -- shutdown ---------------------------------------------------------------------------------

    def _on_close(self, event: QCloseEvent) -> None:
        terminal = self._console.page.terminal if self._console is not None else None
        if self._busy:
            answer = QMessageBox.question(
                self._window, "Quit?", "An operation or the console is still running. Stop it and quit?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if terminal is not None and terminal.is_running:
                terminal.kill()
            if self._task is not None:
                # Closing stdin makes the root helper cancel and unmount; give
                # it a moment so no mounts are left behind.
                self._task.cancel()
                self._task.wait(15000)
        self._save_distribution()
        for worker in list(self._lookups):
            worker.wait(2000)
        event.accept()
