"""Wizard form: software, look & feel, login screen, boot splash,
bootloader, installer and commands — one tab each."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFontDatabase, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from os_builder.core.branding import GRUB_COLORS
from os_builder.core.themes import ARCHIVE_SUFFIXES
from os_builder.models import CustomizationConfig
from os_builder.models.themes import CURSOR_THEMES, GTK_THEMES, ICON_THEMES, ThemePackage, package_for
from os_builder.views.installer_page import InstallerPage
from os_builder.views.pickers import (
    IMAGE_FILTER,
    PNG_FILTER,
    PNG_JPEG_FILTER,
    ColorButton,
    FilePicker,
    ImagePicker,
    PathList,
)
from os_builder.views.widgets import DesktopPicker

HIGHLIGHTS = ("black/light-gray", "white/blue", "black/cyan", "white/red", "yellow/black", "black/white")


def _scroll(*widgets: QWidget, note: str = "") -> QScrollArea:
    content = QWidget()
    layout = QVBoxLayout(content)
    for widget in widgets:
        layout.addWidget(widget)
    if note:
        label = QLabel(note)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(mid);")
        layout.addWidget(label)
    layout.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    return scroll


def _theme_combo(catalog: tuple[ThemePackage, ...], placeholder: str) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItem("")
    for entry in catalog:
        for name in entry.names:
            combo.addItem(name)
            combo.setItemData(combo.count() - 1, f"package: {entry.package}", Qt.ItemDataRole.ToolTipRole)
    combo.lineEdit().setPlaceholderText(placeholder)
    return combo


class SplashPreview(QLabel):
    """Approximation of the generated splash: logo centred on the colour."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(240, 150)
        self._logo = ""
        self._color = "#000000"
        self._render()

    def update_preview(self, logo: str, color: str) -> None:
        self._logo, self._color = logo, color
        self._render()

    def _render(self) -> None:
        pixmap = QPixmap(self.size())
        pixmap.fill(QColor(self._color))
        painter = QPainter(pixmap)
        logo = QPixmap(self._logo) if self._logo and Path(self._logo).is_file() else QPixmap()
        if not logo.isNull():
            logo = logo.scaled(int(self.width() * 0.4), int(self.height() * 0.4), Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)  # fmt: skip
            painter.drawPixmap((self.width() - logo.width()) // 2, (self.height() - logo.height()) // 2, logo)
            bar_y = (self.height() + logo.height()) // 2 + 10
            painter.fillRect(self.width() * 3 // 8, bar_y, self.width() // 4, 2, QColor(255, 255, 255, 60))
            painter.fillRect(self.width() * 3 // 8, bar_y, self.width() // 10, 2, QColor(255, 255, 255, 230))
        painter.end()
        self.setPixmap(pixmap)


class CustomizePage(QWidget):
    """Form of the Wizard producing a :class:`CustomizationConfig`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._iso_grub_cfg: Path | None = None
        self.grub_cfg_text = ""
        self.tabs = QTabWidget()
        self.tabs.addTab(self._software_tab(), "Software")
        self.tabs.addTab(self._look_tab(), "Look && feel")
        self.tabs.addTab(self._login_tab(), "Login screen")
        self.tabs.addTab(self._splash_tab(), "Boot splash")
        self.tabs.addTab(self._bootloader_tab(), "Bootloader")
        self.installer = InstallerPage()
        self.tabs.addTab(self._installer_tab(), "Installer")
        self.tabs.addTab(self._commands_tab(), "Commands")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    # -- tabs -------------------------------------------------------------------------------

    def _software_tab(self) -> QWidget:
        self.desktop_picker = DesktopPicker(keep_option=True, parent=self)
        self.install_edit = QLineEdit()
        self.install_edit.setPlaceholderText("space separated, e.g. vlc gimp git")
        self.remove_edit = QLineEdit()
        self.remove_edit.setPlaceholderText("space separated")
        box = QGroupBox("Software")
        form = QFormLayout(box)
        form.addRow("Desktop / WM:", self.desktop_picker.desktop_combo)
        form.addRow("Login manager:", self.desktop_picker.dm_combo)
        form.addRow("Install packages:", self.install_edit)
        form.addRow("Remove packages:", self.remove_edit)
        return _scroll(box, note="Package changes run apt inside the image and need an internet connection.")

    def _look_tab(self) -> QWidget:
        self.wallpaper_picker = ImagePicker("Choose a wallpaper", IMAGE_FILTER)
        self.gtk_combo = _theme_combo(GTK_THEMES, "keep, or e.g. Yaru-dark")
        self.icon_combo = _theme_combo(ICON_THEMES, "keep, or e.g. Papirus")
        self.cursor_combo = _theme_combo(CURSOR_THEMES, "keep, or e.g. Bibata-Modern-Ice")
        self.theme_packages_check = QCheckBox("Install the packages of the chosen themes")
        self.theme_packages_check.setChecked(True)
        self.archives = PathList("Add theme archives", "Theme archives (" + " ".join(f"*{s}" for s in
                                                                                     ARCHIVE_SUFFIXES) + ")")
        box = QGroupBox("Desktop defaults for new users")
        form = QFormLayout(box)
        form.addRow("Wallpaper:", self.wallpaper_picker)
        form.addRow("GTK theme:", self.gtk_combo)
        form.addRow("Icon theme:", self.icon_combo)
        form.addRow("Cursor theme:", self.cursor_combo)
        form.addRow("", self.theme_packages_check)
        archive_box = QGroupBox("Theme archives (from gnome-look.org, GitHub, …)")
        QVBoxLayout(archive_box).addWidget(self.archives)
        return _scroll(box, archive_box, note=(
            "Wallpaper and GTK/icon themes apply to GNOME, Budgie, Unity, MATE and Cinnamon (GSettings); the icon "
            "theme also to KDE Plasma; the cursor theme to every desktop. Archives may hold GTK, icon or cursor "
            "themes; they are installed to /usr/share/themes or /usr/share/icons."
        ))  # fmt: skip

    def _login_tab(self) -> QWidget:
        self.login_bg_picker = ImagePicker("Login screen background", PNG_JPEG_FILTER)
        self.login_logo_picker = ImagePicker("Login screen logo", "PNG or SVG (*.png *.svg)")
        self.login_message_edit = QLineEdit()
        self.login_message_edit.setPlaceholderText("e.g. Welcome to DebION GNU/Linux")
        box = QGroupBox("Login screen")
        form = QFormLayout(box)
        form.addRow("Background:", self.login_bg_picker)
        form.addRow("Logo:", self.login_logo_picker)
        form.addRow("Message:", self.login_message_edit)
        return _scroll(box, note=(
            "Applied to every login manager in the image: GDM (background, logo, banner message), LightDM Slick "
            "and GTK greeters (background, logo, themes), SDDM Breeze (background, cursor) and greetd/tuigreet "
            "(message). The GTK/icon/cursor themes of the Look & feel tab are used by the greeters too."
        ))  # fmt: skip

    def _splash_tab(self) -> QWidget:
        self.splash_group = QButtonGroup(self)
        self.splash_keep = QRadioButton("Keep the image's boot splash")
        self.splash_theme_radio = QRadioButton("Use an installed Plymouth theme:")
        self.splash_custom = QRadioButton("Create a splash from my logo:")
        for index, radio in enumerate((self.splash_keep, self.splash_theme_radio, self.splash_custom)):
            self.splash_group.addButton(radio, index)
        self.splash_keep.setChecked(True)
        self.plymouth_combo = QComboBox()
        self.plymouth_combo.setEditable(True)
        self.splash_logo_picker = FilePicker("Splash logo (PNG with transparency)", PNG_FILTER)
        self.splash_color = ColorButton("#000000")
        self.splash_preview = SplashPreview()
        custom = QFormLayout()
        custom.addRow("Logo:", self.splash_logo_picker)
        custom.addRow("Background:", self.splash_color)
        custom.addRow("Preview:", self.splash_preview)
        box = QGroupBox("Boot splash (Plymouth)")
        layout = QVBoxLayout(box)
        layout.addWidget(self.splash_keep)
        row = QHBoxLayout()
        row.addWidget(self.splash_theme_radio)
        row.addWidget(self.plymouth_combo, 1)
        layout.addLayout(row)
        layout.addWidget(self.splash_custom)
        layout.addLayout(custom)
        refresh = lambda *_: self.splash_preview.update_preview(  # noqa: E731
            self.splash_logo_picker.path(), self.splash_color.color()
        )
        self.splash_logo_picker.changed.connect(refresh)
        self.splash_color.changed.connect(refresh)
        return _scroll(box, note=(
            "The splash is shown while the live ISO and the installed system start, including the disk "
            "encryption passphrase prompt. The live ISO's initrd is extended with the theme automatically."
        ))  # fmt: skip

    def _bootloader_tab(self) -> QWidget:
        self.timeout_check = QCheckBox("Menu timeout:")
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
        self.kernel_args_edit.setPlaceholderText("e.g. nomodeset (added to every entry)")
        self.grub_bg_picker = ImagePicker("Boot menu background", PNG_JPEG_FILTER)
        self.grub_text_combo = QComboBox()
        self.grub_text_combo.addItems(["", *GRUB_COLORS])
        self.grub_highlight_combo = QComboBox()
        self.grub_highlight_combo.setEditable(True)
        self.grub_highlight_combo.addItems(["", *HIGHLIGHTS])
        self.edit_menu_button = QPushButton("Edit boot menu (grub.cfg)…")
        self.edit_menu_button.clicked.connect(self._edit_grub_cfg)
        self.menu_state = QLabel("")
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.edit_menu_button)
        edit_row.addWidget(self.menu_state, 1)
        live = QGroupBox("Boot menu of the ISO (BIOS and UEFI)")
        live_form = QFormLayout(live)
        live_form.addRow("GRUB:", timeout_row)
        live_form.addRow("Kernel arguments:", self.kernel_args_edit)
        live_form.addRow("Background:", self.grub_bg_picker)
        live_form.addRow("Text colour:", self.grub_text_combo)
        live_form.addRow("Highlight:", self.grub_highlight_combo)
        live_form.addRow("", edit_row)

        self.target_timeout_check = QCheckBox("Show the menu for:")
        self.target_timeout_spin = QSpinBox()
        self.target_timeout_spin.setRange(0, 600)
        self.target_timeout_spin.setValue(5)
        self.target_timeout_spin.setSuffix(" s")
        self.target_timeout_spin.setEnabled(False)
        self.target_timeout_check.toggled.connect(self.target_timeout_spin.setEnabled)
        target_timeout_row = QHBoxLayout()
        target_timeout_row.addWidget(self.target_timeout_check)
        target_timeout_row.addWidget(self.target_timeout_spin)
        target_timeout_row.addStretch(1)
        self.target_cmdline_edit = QLineEdit()
        self.target_cmdline_edit.setPlaceholderText("GRUB_CMDLINE_LINUX_DEFAULT, e.g. quiet splash")
        self.target_theme_combo = QComboBox()
        self.target_theme_combo.addItem("Ubuntu default", "")
        self.target_theme_combo.addItem("Starfield theme", "starfield")
        self.target_theme_combo.addItem("Same background image as the ISO menu", "background")
        installed = QGroupBox("Boot menu of the installed system")
        installed_form = QFormLayout(installed)
        installed_form.addRow("GRUB:", target_timeout_row)
        installed_form.addRow("Kernel arguments:", self.target_cmdline_edit)
        installed_form.addRow("Look:", self.target_theme_combo)
        return _scroll(live, installed, note=(
            "The ISO keeps its signed GRUB, so Secure Boot still works with a background image. The installed "
            "system's settings go to /etc/default/grub.d and are used when the installer runs update-grub."
        ))  # fmt: skip

    def _installer_tab(self) -> QWidget:
        self.calamares_check = QCheckBox("Also install the Calamares installer:")
        self.calamares_edit = QLineEdit(" ".join(CustomizationConfig().calamares_packages))
        self.calamares_edit.setEnabled(False)
        self.calamares_check.toggled.connect(self.calamares_edit.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.calamares_check)
        row.addWidget(self.calamares_edit, 1)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.installer, 1)
        layout.addLayout(row)
        return widget

    def _commands_tab(self) -> QWidget:
        self.commands_edit = QPlainTextEdit()
        self.commands_edit.setPlaceholderText(
            "# Shell commands run as root inside the image, after everything else\n"
            "# e.g.\nadd-apt-repository -y universe\napt-get install -y htop"
        )
        self.commands_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        return self.commands_edit

    # -- grub.cfg editor ----------------------------------------------------------------------

    def set_context(self, iso_dir: Path, plymouth_themes: list[str]) -> None:
        self._iso_grub_cfg = iso_dir / "boot/grub/grub.cfg"
        current = self.plymouth_combo.currentText()
        self.plymouth_combo.clear()
        self.plymouth_combo.addItems(plymouth_themes)
        self.plymouth_combo.setCurrentText(current)
        self.edit_menu_button.setEnabled(self._iso_grub_cfg.is_file())

    def _edit_grub_cfg(self) -> None:
        if not self.grub_cfg_text:
            try:
                self.grub_cfg_text = self._iso_grub_cfg.read_text(encoding="utf-8") if self._iso_grub_cfg else ""
            except OSError as exc:
                QMessageBox.warning(self, "Boot menu", f"Cannot read grub.cfg: {exc}")
                return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit boot menu — boot/grub/grub.cfg")
        dialog.resize(760, 560)
        editor = QPlainTextEdit(self.grub_cfg_text)
        editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        hint = QLabel("Changes are written when the Wizard is applied. The timeout, kernel arguments, background "
                      "and colours above are applied on top of this text.")  # fmt: skip
        hint.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.Reset)  # fmt: skip
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(
            lambda: editor.setPlainText(self._iso_grub_cfg.read_text(encoding="utf-8") if self._iso_grub_cfg else "")
        )
        layout = QVBoxLayout(dialog)
        layout.addWidget(hint)
        layout.addWidget(editor, 1)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            original = self._iso_grub_cfg.read_text(encoding="utf-8") if self._iso_grub_cfg else ""
            self.grub_cfg_text = editor.toPlainText() if editor.toPlainText() != original else ""
            self.menu_state.setText("edited — will be written on Apply" if self.grub_cfg_text else "")

    # -- result -------------------------------------------------------------------------------

    def config(self) -> CustomizationConfig:
        """Collect every tab into a model object (may hash the installer password)."""
        gtk, icons, cursor = (c.currentText().strip() for c in (self.gtk_combo, self.icon_combo, self.cursor_combo))
        packages = self.install_edit.text().split()
        if self.theme_packages_check.isChecked():
            for name, catalog in ((gtk, GTK_THEMES), (icons, ICON_THEMES), (cursor, CURSOR_THEMES)):
                package = package_for(name, catalog) if name else None
                if package and package not in packages:
                    packages.append(package)
        splash_mode = self.splash_group.checkedId()
        return CustomizationConfig(
            desktop=self.desktop_picker.desktop_id(),
            display_manager=self.desktop_picker.display_manager_id(),
            extra_packages=packages,
            remove_packages=self.remove_edit.text().split(),
            wallpaper_path=self.wallpaper_picker.path(),
            gtk_theme=gtk,
            icon_theme=icons,
            cursor_theme=cursor,
            theme_archives=self.archives.paths(),
            login_background=self.login_bg_picker.path(),
            login_logo=self.login_logo_picker.path(),
            login_message=self.login_message_edit.text().strip(),
            plymouth_theme=self.plymouth_combo.currentText().strip() if splash_mode == 1 else "",
            splash_logo=self.splash_logo_picker.path() if splash_mode == 2 else "",
            splash_color=self.splash_color.color() if splash_mode == 2 else "",
            grub_timeout=self.timeout_spin.value() if self.timeout_check.isChecked() else None,
            grub_extra_kernel_args=self.kernel_args_edit.text().strip(),
            grub_background=self.grub_bg_picker.path(),
            grub_text_color=self.grub_text_combo.currentText(),
            grub_highlight_color=self.grub_highlight_combo.currentText().strip(),
            grub_cfg_text=self.grub_cfg_text,
            target_grub_timeout=self.target_timeout_spin.value() if self.target_timeout_check.isChecked() else None,
            target_grub_cmdline=self.target_cmdline_edit.text().strip(),
            target_grub_theme=self.target_theme_combo.currentData(),
            autoinstall_yaml=self.installer.yaml_text() if self.installer.is_enabled() else "",
            autoinstall_unattended=self.installer.is_enabled() and self.installer.automatic_radio.isChecked(),
            install_calamares=self.calamares_check.isChecked(),
            calamares_packages=self.calamares_edit.text().split(),
            commands=self.commands_edit.toPlainText() if self.commands_edit.toPlainText().strip() else "",
        )

