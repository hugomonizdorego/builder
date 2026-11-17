"""Page 1 — choose a workspace and a source image."""

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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ubuntu_builder.core.preflight import ToolStatus, install_command, missing_required
from ubuntu_builder.models import Flavor, IsoImage, ProjectState, SupportStatus, UbuntuRelease


class PreflightDialog(QDialog):
    """Table of host tools with what is missing and how to install it."""

    def __init__(self, statuses: list[ToolStatus], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Host requirements")
        self.resize(760, 420)
        table = QTableWidget(len(statuses), 4)
        table.setHorizontalHeaderLabels(["Tool", "Status", "Package", "Used for"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, status in enumerate(statuses):
            state = "Found" if status.found else ("MISSING" if status.tool.required else "Not installed (optional)")
            for column, text in enumerate((status.tool.name, state, status.tool.package, status.tool.purpose)):
                item = QTableWidgetItem(text)
                if column == 1 and not status.found and status.tool.required:
                    item.setForeground(Qt.GlobalColor.red)
                table.setItem(row, column, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)

        command = install_command(statuses, include_optional=True)
        command_edit = QLineEdit(command or "Everything is installed.")
        command_edit.setReadOnly(True)
        copy_button = QPushButton("Copy")
        copy_button.setEnabled(bool(command))
        copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(command))
        command_row = QHBoxLayout()
        command_row.addWidget(command_edit, 1)
        command_row.addWidget(copy_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(table, 1)
        layout.addWidget(QLabel("Install what is missing with:"))
        layout.addLayout(command_row)
        layout.addWidget(buttons)


class SourcePage(QWidget):
    """Workspace selection, release catalog and ISO extraction."""

    workspace_changed = pyqtSignal(Path)
    refresh_requested = pyqtSignal()
    selection_changed = pyqtSignal()  # release or flavor changed → reload image list
    download_requested = pyqtSignal(object)  # IsoImage
    extract_requested = pyqtSignal(Path)  # local ISO
    delete_requested = pyqtSignal()
    preflight_details_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._releases: list[UbuntuRelease] = []

        # -- host check banner ------------------------------------------------
        self.preflight_label = QLabel()
        self.preflight_label.setWordWrap(True)
        self.preflight_button = QPushButton("Details…")
        self.preflight_button.clicked.connect(self.preflight_details_requested)
        banner = QHBoxLayout()
        banner.addWidget(self.preflight_label, 1)
        banner.addWidget(self.preflight_button)

        # -- workspace --------------------------------------------------------
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setReadOnly(True)
        workspace_button = QPushButton("Choose…")
        workspace_button.clicked.connect(self._choose_workspace)
        self.project_label = QLabel()
        self.project_label.setWordWrap(True)
        self.delete_button = QPushButton("Delete extracted image")
        self.delete_button.clicked.connect(self.delete_requested)
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(workspace_button)
        project_row = QHBoxLayout()
        project_row.addWidget(self.project_label, 1)
        project_row.addWidget(self.delete_button)
        workspace_box = QGroupBox("Workspace")
        workspace_layout = QVBoxLayout(workspace_box)
        workspace_layout.addLayout(workspace_row)
        workspace_layout.addLayout(project_row)

        # -- official images --------------------------------------------------
        self.release_combo = QComboBox()
        self.flavor_combo = QComboBox()
        self.image_combo = QComboBox()
        self.older_check = QCheckBox("Show releases older than 20.04")
        self.refresh_button = QPushButton("Refresh")
        self.catalog_label = QLabel("Loading release information…")
        self.catalog_label.setWordWrap(True)
        self.release_note = QLabel()
        self.release_note.setWordWrap(True)
        self.download_button = QPushButton("Download && extract")
        release_row = QHBoxLayout()
        release_row.addWidget(self.release_combo, 1)
        release_row.addWidget(self.refresh_button)
        form = QFormLayout()
        form.addRow("Release:", release_row)
        form.addRow("", self.older_check)
        form.addRow("Flavor:", self.flavor_combo)
        form.addRow("Image:", self.image_combo)
        download_row = QHBoxLayout()
        download_row.addWidget(self.release_note, 1)
        download_row.addWidget(self.download_button)
        official_box = QGroupBox("Download an official image")
        official_layout = QVBoxLayout(official_box)
        official_layout.addLayout(form)
        official_layout.addLayout(download_row)
        official_layout.addWidget(self.catalog_label)

        # -- local ISO --------------------------------------------------------
        self.iso_edit = QLineEdit()
        self.iso_edit.setPlaceholderText("/path/to/ubuntu.iso")
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._choose_iso)
        self.extract_button = QPushButton("Extract")
        local_row = QHBoxLayout()
        local_row.addWidget(self.iso_edit, 1)
        local_row.addWidget(browse_button)
        local_row.addWidget(self.extract_button)
        local_box = QGroupBox("Or use an ISO file you already have")
        local_box.setLayout(local_row)

        layout = QVBoxLayout(self)
        layout.addLayout(banner)
        layout.addWidget(workspace_box)
        layout.addWidget(official_box)
        layout.addWidget(local_box)
        layout.addStretch(1)

        self.refresh_button.clicked.connect(self.refresh_requested)
        self.older_check.toggled.connect(lambda _on: self._fill_releases())
        self.release_combo.currentIndexChanged.connect(self._on_selection_changed)
        self.flavor_combo.currentIndexChanged.connect(self._on_selection_changed)
        self.download_button.clicked.connect(self._emit_download)
        self.extract_button.clicked.connect(self._emit_extract)
        self.iso_edit.textChanged.connect(lambda _text: self._update_buttons())
        self._busy = False
        self._tools_ok = True
        self._update_buttons()

    # -- data in ------------------------------------------------------------

    def set_preflight(self, statuses: list[ToolStatus]) -> None:
        missing = missing_required(statuses)
        optional = [s for s in statuses if not s.found and not s.tool.required]
        self._tools_ok = not missing
        if missing:
            names = ", ".join(s.tool.name for s in missing)
            self.preflight_label.setText(f"<b style='color:#c7162b'>Missing required host tools:</b> {names}")
        elif optional:
            self.preflight_label.setText(
                f"Host check passed. Optional tools not installed: {', '.join(s.tool.name for s in optional)}."
            )
        else:
            self.preflight_label.setText("Host check passed: all tools are installed.")
        self._update_buttons()

    def set_workspace(self, path: Path, state: ProjectState) -> None:
        self.workspace_edit.setText(str(path))
        if state.extracted:
            layer = f", customising <code>{state.target_squashfs}</code>"
            self.project_label.setText(f"Extracted image: <b>{state.volume_id or 'unnamed'}</b>{layer}")
        else:
            self.project_label.setText("No image extracted yet.")
        self.delete_button.setVisible(state.extracted)

    def set_catalog(self, releases: list[UbuntuRelease], flavors: list[Flavor], note: str) -> None:
        self._releases = releases
        self.catalog_label.setText(note)
        self.flavor_combo.blockSignals(True)
        self.flavor_combo.clear()
        for flavor in flavors:
            self.flavor_combo.addItem(flavor.name, flavor)
            row = self.flavor_combo.count() - 1
            self.flavor_combo.setItemData(row, flavor.description, Qt.ItemDataRole.ToolTipRole)
        self.flavor_combo.blockSignals(False)
        self._fill_releases()

    def _fill_releases(self) -> None:
        previous = self.current_release()
        floor = (0,) if self.older_check.isChecked() else (20, 4)
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        for release in self._releases:
            if tuple(int(p) for p in release.version.split(".")) >= floor:
                self.release_combo.addItem(release.label(), release)
        if previous is not None:
            index = self.release_combo.findText(previous.label())
            self.release_combo.setCurrentIndex(max(index, 0))
        self.release_combo.blockSignals(False)
        self._on_selection_changed()

    def set_images(self, images: list[IsoImage], note: str = "") -> None:
        self.image_combo.clear()
        for image in images:
            self.image_combo.addItem(image.label(), image)
        if not images:
            self.image_combo.addItem(note or "No images found")
        self._update_buttons()

    def set_images_loading(self) -> None:
        self.image_combo.clear()
        self.image_combo.addItem("Looking for images…")
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    # -- data out -----------------------------------------------------------

    def current_release(self) -> UbuntuRelease | None:
        return self.release_combo.currentData()

    def current_flavor(self) -> Flavor | None:
        return self.flavor_combo.currentData()

    def current_image(self) -> IsoImage | None:
        return self.image_combo.currentData()

    # -- internals ----------------------------------------------------------

    def _on_selection_changed(self) -> None:
        release = self.current_release()
        if release is None:
            self.release_note.setText("")
        else:
            status = release.status()
            notes = {
                SupportStatus.EOL: "<span style='color:#c7162b'>This release is end of life: it receives no "
                "security updates and its package archive may be gone.</span>",
                SupportStatus.ESM: "<span style='color:#b85c00'>Standard support has ended; updates need "
                "Ubuntu Pro (ESM).</span>",
            }
            self.release_note.setText(notes.get(status, ""))
        self.selection_changed.emit()

    def _update_buttons(self) -> None:
        ready = self._tools_ok and not self._busy
        self.download_button.setEnabled(ready and self.current_image() is not None)
        self.extract_button.setEnabled(ready and bool(self.iso_edit.text().strip()))
        self.delete_button.setEnabled(not self._busy)
        self.refresh_button.setEnabled(not self._busy)

    def _emit_download(self) -> None:
        image = self.current_image()
        if image is not None:
            self.download_requested.emit(image)

    def _emit_extract(self) -> None:
        self.extract_requested.emit(Path(self.iso_edit.text().strip()).expanduser())

    def _choose_workspace(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose workspace directory", self.workspace_edit.text())
        if chosen:
            self.workspace_changed.emit(Path(chosen))

    def _choose_iso(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose an Ubuntu ISO", str(Path.home()), "ISO images (*.iso)")
        if chosen:
            self.iso_edit.setText(chosen)
