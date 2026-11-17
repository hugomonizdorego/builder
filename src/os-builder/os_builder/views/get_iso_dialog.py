""""Select ISO" — use a local image or download an official one."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
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
    QVBoxLayout,
    QWidget,
)

from os_builder.models import Flavor, IsoImage, SupportStatus, UbuntuRelease
from os_builder.models.release import version_key


class GetIsoDialog(QDialog):
    """Two ways to get a source image. Emits a request and closes."""

    refresh_requested = pyqtSignal()
    selection_changed = pyqtSignal()  # release or flavor changed → reload image list
    download_requested = pyqtSignal(object)  # IsoImage
    extract_requested = pyqtSignal(Path)  # local ISO

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select ISO")
        self.setMinimumWidth(620)
        self._releases: list[UbuntuRelease] = []

        # -- local ISO (the original "Select ISO") ----------------------------
        self.iso_edit = QLineEdit()
        self.iso_edit.setPlaceholderText("/path/to/ubuntu.iso")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_iso)
        self.extract_button = QPushButton("Extract")
        self.extract_button.setDefault(True)
        local_row = QHBoxLayout()
        local_row.addWidget(self.iso_edit, 1)
        local_row.addWidget(browse)
        local_row.addWidget(self.extract_button)
        local_box = QGroupBox("Use an ISO file")
        local_box.setLayout(local_row)

        # -- official images ----------------------------------------------------
        self.release_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.older_check = QCheckBox("Show releases older than 20.04")
        self.flavor_combo = QComboBox()
        self.image_combo = QComboBox()
        self.release_note = QLabel()
        self.release_note.setWordWrap(True)
        self.catalog_label = QLabel("Loading release information from ubuntu.com…")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setStyleSheet("color: palette(mid);")
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(local_box)
        layout.addWidget(official_box)
        layout.addWidget(buttons)

        self.refresh_button.clicked.connect(self.refresh_requested)
        self.older_check.toggled.connect(lambda _on: self._fill_releases())
        self.release_combo.currentIndexChanged.connect(self._on_selection_changed)
        self.flavor_combo.currentIndexChanged.connect(self._on_selection_changed)
        self.download_button.clicked.connect(self._emit_download)
        self.extract_button.clicked.connect(self._emit_extract)
        self.iso_edit.textChanged.connect(lambda _text: self._update_buttons())
        self._update_buttons()

    # -- data in ------------------------------------------------------------

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

    def set_catalog_error(self, message: str) -> None:
        self.catalog_label.setText(
            f"<span style='color:#c7162b'>Could not load release information: {message}</span> "
            "You can still use a local ISO file."
        )

    def _fill_releases(self) -> None:
        previous = self.current_release()
        floor = (0,) if self.older_check.isChecked() else (20, 4)
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        for release in self._releases:
            if version_key(release.version) >= floor:
                self.release_combo.addItem(release.label(), release)
        if previous is not None:
            self.release_combo.setCurrentIndex(max(self.release_combo.findText(previous.label()), 0))
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
        notes = {
            SupportStatus.EOL: "<span style='color:#c7162b'>End of life: no security updates, and the "
            "package archive may be gone.</span>",
            SupportStatus.ESM: "<span style='color:#b85c00'>Standard support has ended; updates need "
            "Ubuntu Pro (ESM).</span>",
        }
        self.release_note.setText(notes.get(release.status(), "") if release else "")
        self.selection_changed.emit()

    def _update_buttons(self) -> None:
        self.download_button.setEnabled(self.current_image() is not None)
        self.extract_button.setEnabled(bool(self.iso_edit.text().strip()))

    def _emit_download(self) -> None:
        image = self.current_image()
        if image is not None:
            self.accept()
            self.download_requested.emit(image)

    def _emit_extract(self) -> None:
        self.accept()
        self.extract_requested.emit(Path(self.iso_edit.text().strip()).expanduser())

    def _choose_iso(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose an Ubuntu ISO", str(Path.home()), "ISO images (*.iso)")
        if chosen:
            self.iso_edit.setText(chosen)
