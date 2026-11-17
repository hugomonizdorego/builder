"""Page 3 — declarative customizations (boot, look, software, installer)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ubuntu_builder.models import CustomizationConfig, DesktopEnvironment


class FilePicker(QWidget):
    """Line edit with a "Browse…" button."""

    def __init__(self, caption: str, name_filter: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption, self._filter = caption, name_filter
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(True)
        button = QPushButton("Browse…")
        button.clicked.connect(self._browse)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(button)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, self._caption, str(Path.home()), self._filter)
        if chosen:
            self.edit.setText(chosen)

    def path(self) -> str:
        return self.edit.text().strip()


class CustomizePage(QWidget):
    """Form producing a :class:`CustomizationConfig`."""

    apply_requested = pyqtSignal(object)  # CustomizationConfig

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # -- identity & boot --------------------------------------------------
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Nusantara OS (leave empty to keep Ubuntu)")
        self.timeout_check = QCheckBox("Set boot menu timeout:")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(-1, 600)
        self.timeout_spin.setValue(10)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setEnabled(False)
        self.timeout_check.toggled.connect(self.timeout_spin.setEnabled)
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(self.timeout_check)
        timeout_row.addWidget(self.timeout_spin)
        timeout_row.addStretch(1)
        self.kernel_args_edit = QLineEdit()
        self.kernel_args_edit.setPlaceholderText("e.g. quiet splash")
        self.plymouth_combo = QComboBox()
        self.plymouth_combo.setEditable(True)
        self.plymouth_combo.lineEdit().setPlaceholderText("theme name, e.g. spinner (must be installed in the image)")
        boot_box = QGroupBox("Identity && boot")
        boot_form = QFormLayout(boot_box)
        boot_form.addRow("Distribution name:", self.name_edit)
        boot_form.addRow("GRUB:", timeout_row)
        boot_form.addRow("Extra kernel arguments:", self.kernel_args_edit)
        boot_form.addRow("Boot splash (Plymouth):", self.plymouth_combo)

        # -- appearance -------------------------------------------------------
        self.wallpaper_picker = FilePicker("Choose a wallpaper", "Images (*.png *.jpg *.jpeg *.webp *.svg)")
        self.gtk_theme_edit = QLineEdit()
        self.gtk_theme_edit.setPlaceholderText("e.g. Yaru-dark")
        self.icon_theme_edit = QLineEdit()
        self.icon_theme_edit.setPlaceholderText("e.g. Papirus")
        look_box = QGroupBox("Appearance (GNOME based desktops)")
        look_form = QFormLayout(look_box)
        look_form.addRow("Default wallpaper:", self.wallpaper_picker)
        look_form.addRow("GTK theme:", self.gtk_theme_edit)
        look_form.addRow("Icon theme:", self.icon_theme_edit)

        # -- software ---------------------------------------------------------
        self.desktop_combo = QComboBox()
        for desktop in DesktopEnvironment:
            self.desktop_combo.addItem(desktop.label, desktop)
        self.install_edit = QLineEdit()
        self.install_edit.setPlaceholderText("space separated, e.g. vlc gimp git")
        self.remove_edit = QLineEdit()
        self.remove_edit.setPlaceholderText("space separated")
        software_box = QGroupBox("Software")
        software_form = QFormLayout(software_box)
        software_form.addRow("Desktop environment:", self.desktop_combo)
        software_form.addRow("Install packages:", self.install_edit)
        software_form.addRow("Remove packages:", self.remove_edit)

        # -- installer --------------------------------------------------------
        self.autoinstall_picker = FilePicker("Choose an autoinstall file", "YAML (*.yaml *.yml)")
        self.calamares_check = QCheckBox("Install Calamares:")
        self.calamares_edit = QLineEdit(" ".join(CustomizationConfig().calamares_packages))
        self.calamares_edit.setEnabled(False)
        self.calamares_check.toggled.connect(self.calamares_edit.setEnabled)
        calamares_row = QHBoxLayout()
        calamares_row.addWidget(self.calamares_check)
        calamares_row.addWidget(self.calamares_edit, 1)
        installer_box = QGroupBox("Installer")
        installer_form = QFormLayout(installer_box)
        installer_form.addRow("Subiquity autoinstall.yaml:", self.autoinstall_picker)
        installer_form.addRow("Calamares:", calamares_row)

        self.apply_button = QPushButton("Apply customizations")
        note = QLabel("Package changes run apt inside the image and need an internet connection.")
        note.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(note, 1)
        footer.addWidget(self.apply_button)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        for box in (boot_box, look_box, software_box, installer_box):
            content_layout.addWidget(box)
        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addLayout(footer)

        self.apply_button.clicked.connect(lambda: self.apply_requested.emit(self.config()))

    def set_plymouth_themes(self, themes: list[str]) -> None:
        """Offer the themes found in the extracted image."""
        current = self.plymouth_combo.currentText()
        self.plymouth_combo.clear()
        self.plymouth_combo.addItems(["", *themes])
        self.plymouth_combo.setCurrentText(current)

    def config(self) -> CustomizationConfig:
        """Collect the form into a model object."""
        return CustomizationConfig(
            distro_name=self.name_edit.text().strip(),
            plymouth_theme=self.plymouth_combo.currentText().strip(),
            grub_timeout=self.timeout_spin.value() if self.timeout_check.isChecked() else None,
            grub_extra_kernel_args=self.kernel_args_edit.text().strip(),
            wallpaper_path=self.wallpaper_picker.path(),
            gtk_theme=self.gtk_theme_edit.text().strip(),
            icon_theme=self.icon_theme_edit.text().strip(),
            desktop=self.desktop_combo.currentData(),
            extra_packages=self.install_edit.text().split(),
            remove_packages=self.remove_edit.text().split(),
            autoinstall_path=self.autoinstall_picker.path(),
            install_calamares=self.calamares_check.isChecked(),
            calamares_packages=self.calamares_edit.text().split(),
        )
