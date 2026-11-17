"""Dialogs opened from the toolbar: Wizard, Build, Settings, Console, About."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from os_builder import APP_NAME, __version__
from os_builder.core.backend import COMPRESSIONS
from os_builder.core.iso import MAX_VOLUME_ID
from os_builder.core.preflight import ToolStatus, install_command
from os_builder.models import CustomizationConfig
from os_builder.views import resources
from os_builder.views.customize_page import CustomizePage
from os_builder.views.terminal import TerminalPage

RESOLUTIONS = ("1024x768", "1280x800", "1366x768", "1600x900", "1920x1080")


class WizardDialog(QDialog):
    """Batch customization, like the original Wizard: software, then commands."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wizard")
        self.resize(720, 640)
        self.form = CustomizePage()
        self.build_after = QCheckBox("Build the ISO when done")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Apply")
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(self.build_after)
        footer.addStretch(1)
        footer.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.form, 1)
        layout.addLayout(footer)

    def config(self) -> CustomizationConfig:
        return self.form.config()

    def _apply(self) -> None:
        if self.config().is_empty():
            QMessageBox.information(self, "Wizard", "Nothing to apply — the form is empty.")
            return
        self.accept()


class BuildDialog(QDialog):
    """Output options of the build."""

    def __init__(self, output: Path, volume_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Build ISO")
        self.setMinimumWidth(560)
        self.output_edit = QLineEdit(str(output))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)
        self.volume_edit = QLineEdit(volume_id)
        self.volume_edit.setMaxLength(MAX_VOLUME_ID)
        self.compression_combo = QComboBox()
        self.compression_combo.addItem("Same as the source image", "")
        hints = ("smallest", "fast, slightly larger", "legacy", "fastest, largest")
        for name, hint in zip(COMPRESSIONS, hints, strict=True):
            self.compression_combo.addItem(f"{name} — {hint}", name)
        self.boot_combo = QComboBox()
        self.boot_combo.addItem("Hybrid — legacy BIOS and UEFI (recommended)", "hybrid")
        self.boot_combo.addItem("UEFI only — modern PCs without legacy/CSM support", "uefi")
        form = QFormLayout()
        form.addRow("Output file:", output_row)
        form.addRow("Volume label:", self.volume_edit)
        form.addRow("Compression:", self.compression_combo)
        form.addRow("Firmware:", self.boot_combo)
        note = QLabel(
            "The Distribution settings are applied first. The file system is then repacked, "
            "md5sum.txt regenerated and the ISO written with xorriso. UEFI boot keeps the source "
            "image's signed shim, so it starts with Secure Boot enabled."
        )
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Build")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def output(self) -> Path:
        return Path(self.output_edit.text().strip()).expanduser()

    def volume_id(self) -> str:
        return self.volume_edit.text().strip()

    def compression(self) -> str:
        return self.compression_combo.currentData()

    def boot_mode(self) -> str:
        return self.boot_combo.currentData()

    def _accept(self) -> None:
        output = self.output()
        if not output.name or output.suffix != ".iso":
            QMessageBox.warning(self, "Build ISO", "Choose an output file ending in .iso.")
            return
        if output.exists():
            answer = QMessageBox.question(self, "Overwrite?", f"{output} exists. Overwrite it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def _choose_output(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Save ISO as", self.output_edit.text(), "ISO images (*.iso)")
        if chosen:
            self.output_edit.setText(chosen if chosen.endswith(".iso") else chosen + ".iso")


class HostRequirements(QWidget):
    """Table of host tools (the pre-flight check) with an install command."""

    recheck_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Tool", "Status", "Package", "Used for"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.command_edit = QLineEdit()
        self.command_edit.setReadOnly(True)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.command_edit.text()))
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self.recheck_requested)
        row = QHBoxLayout()
        row.addWidget(self.command_edit, 1)
        row.addWidget(self.copy_button)
        row.addWidget(recheck)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table, 1)
        layout.addLayout(row)

    def set_statuses(self, statuses: list[ToolStatus]) -> None:
        self.table.setRowCount(len(statuses))
        for row, status in enumerate(statuses):
            state = "Found" if status.found else ("MISSING" if status.tool.required else "Not installed (optional)")
            for column, text in enumerate((status.tool.name, state, status.tool.package, status.tool.purpose)):
                item = QTableWidgetItem(text)
                if column == 1 and not status.found and status.tool.required:
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        command = install_command(statuses, include_optional=True)
        self.command_edit.setText(command or "Everything is installed.")
        self.copy_button.setEnabled(bool(command))


class SettingsDialog(QDialog):
    """Workspace, appearance, nested desktop resolution and host check."""

    workspace_chosen = pyqtSignal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 560)
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setReadOnly(True)
        workspace_button = QPushButton("Choose…")
        workspace_button.clicked.connect(self._choose_workspace)
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(workspace_button)
        self.toolbar_combo = QComboBox()
        self.toolbar_combo.addItem("Dark", "dark")
        self.toolbar_combo.addItem("Light", "light")
        self.resolution_combo = QComboBox()
        self.resolution_combo.setEditable(True)
        self.resolution_combo.addItems(RESOLUTIONS)
        general = QGroupBox("General")
        form = QFormLayout(general)
        form.addRow("Workspace:", workspace_row)
        form.addRow("Toolbar style:", self.toolbar_combo)
        form.addRow("Desktop window size:", self.resolution_combo)

        self.requirements = HostRequirements()
        host_box = QGroupBox("Host requirements")
        host_layout = QVBoxLayout(host_box)
        host_layout.addWidget(self.requirements)

        about = QPushButton("About")
        about.clicked.connect(lambda: show_about(self))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer = QHBoxLayout()
        footer.addWidget(about)
        footer.addStretch(1)
        footer.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(general)
        layout.addWidget(host_box, 1)
        layout.addLayout(footer)

    def load(self, workspace: Path, toolbar: str, resolution: str) -> None:
        self.workspace_edit.setText(str(workspace))
        self.toolbar_combo.setCurrentIndex(max(0, self.toolbar_combo.findData(toolbar)))
        self.resolution_combo.setCurrentText(resolution)

    def toolbar(self) -> str:
        return self.toolbar_combo.currentData()

    def resolution(self) -> str:
        return self.resolution_combo.currentText().strip()

    def _choose_workspace(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace directory", self.workspace_edit.text())
        if chosen:
            self.workspace_edit.setText(chosen)
            self.workspace_chosen.emit(Path(chosen))


class ConsoleDialog(QDialog):
    """Chroot console, embedded (the original opened an external terminal)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Console")
        self.resize(820, 520)
        self.page = TerminalPage()
        layout = QVBoxLayout(self)
        layout.addWidget(self.page)

    def reject(self) -> None:
        """Closing the window ends the session (and unmounts the image)."""
        if self.page.terminal.is_running:
            self.page.terminal.stop()
        super().reject()


def show_about(parent: QWidget | None) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(f"About {APP_NAME}")
    box.setIconPixmap(resources.logo(80))
    box.setText(f"<h3>{APP_NAME} {__version__}</h3><p>Build your own Ubuntu based distribution.</p>")
    box.setInformativeText(
        "Based on Ubuntu Builder 2.5.2 (2010–2014) by Francesco Muriana.<br>"
        "Python / Qt rewrite with live release data, layered images and hybrid BIOS/UEFI ISOs.<br><br>"
        "License: GNU GPL version 3 or later."
    )
    box.exec()
