"""Main window, laid out after Ubuntu Builder 2.x.

    ┌────────────────────────────────────────────────────────────────────────┐
    │ (logo) Ubuntu Builder   Select ISO  Wizard  Build  QEMU  Export  Remove  Settings │
    ├────────────────────────────────────────────────────────────────────────┤
    │ ┌ Distribution ──────────────┐  ┌ Customize ─────────────────────────┐ │
    │ │ ISO / version / arch       │  │ Console          Synaptic          │ │
    │ │ Name, Description, Locale  │  │ Desktop          Install .deb      │ │
    │ │ Welcome message            │  │ Desktop environment  [..] Install  │ │
    │ │ [x] Remove Windows files   │  │                                    │ │
    │ └────────────────────────────┘  └────────────────────────────────────┘ │
    │ stage ……………………  [=========progress=========]  Cancel                 │
    │ log …                                                                  │
    └────────────────────────────────────────────────────────────────────────┘

The window is a pure view: it exposes widgets and signals, the controller
does the rest.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ubuntu_builder import APP_NAME
from ubuntu_builder.models import CustomizationConfig, DesktopEnvironment
from ubuntu_builder.views import resources

#: Toolbar entries: attribute, label, icon of the 2.x icon set, tooltip.
TOOLBAR = (
    ("select_button", "Select ISO", "import", "Choose a local ISO or download an official one"),
    ("wizard_button", "Wizard", "wizard", "Install software, change boot and look, run commands"),
    ("build_button", "Build", "build", "Apply the Distribution settings and build the ISO"),
    ("qemu_button", "QEMU", "qemu", "Test the built ISO in a virtual machine"),
    ("export_button", "Export", "export", "Show, write to USB or burn the built ISO"),
    ("remove_button", "Remove", "remove", "Delete the extracted image"),
    ("settings_button", "Settings", "settings", "Workspace, appearance and host requirements"),
)

COMMON_LOCALES = (
    "", "en_US.UTF-8", "en_GB.UTF-8", "id_ID.UTF-8", "ms_MY.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8",
    "fr_FR.UTF-8", "it_IT.UTF-8", "pt_BR.UTF-8", "ru_RU.UTF-8", "tr_TR.UTF-8", "zh_CN.UTF-8", "ja_JP.UTF-8",
)  # fmt: skip


class MainWindow(QMainWindow):
    cancel_requested = pyqtSignal()
    closing = pyqtSignal(object)  # QCloseEvent — the controller may veto

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(resources.app_icon())
        self.resize(900, 640)
        self.setMinimumSize(780, 560)
        self.setStyleSheet(resources.APP_STYLE)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 6)
        panels = QHBoxLayout()
        panels.addWidget(self._build_distribution_panel(), 1)
        panels.addWidget(self._build_customize_panel(), 1)

        self.stage_label = QLabel("Ready")
        self.stage_label.setMinimumWidth(220)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.stage_label, 2)
        progress_row.addWidget(self.progress_bar, 3)
        progress_row.addWidget(self.cancel_button)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText("Ubuntu Builder log — command output appears here.")

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addLayout(panels)
        top_layout.addLayout(progress_row)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.log_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        body_layout.addWidget(splitter)
        outer.addWidget(body, 1)
        self.setCentralWidget(container)
        self.set_toolbar_style("dark")

    # -- construction ------------------------------------------------------------

    def _build_header(self) -> QFrame:
        self.header = QFrame()
        self.header.setObjectName("header")
        layout = QHBoxLayout(self.header)
        layout.setContentsMargins(12, 6, 8, 6)
        logo = QLabel()
        logo.setPixmap(resources.logo(48))
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("Build your own Ubuntu based distribution")
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        layout.addWidget(logo)
        layout.addLayout(titles)
        layout.addStretch(1)
        self._toolbar_buttons: list[tuple[QToolButton, str]] = []
        for attribute, label, icon, tooltip in TOOLBAR:
            button = QToolButton()
            button.setText(label)
            button.setToolTip(tooltip)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(32, 32))
            button.setAutoRaise(True)
            setattr(self, attribute, button)
            self._toolbar_buttons.append((button, icon))
            layout.addWidget(button)
        self.qemu_menu = QMenu(self)
        self.qemu_bios_action = self.qemu_menu.addAction("Boot in legacy BIOS mode")
        self.qemu_uefi_action = self.qemu_menu.addAction("Boot in UEFI mode")
        self.qemu_other_action = self.qemu_menu.addAction("Test another ISO file…")
        self.qemu_button.setMenu(self.qemu_menu)
        self.qemu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.export_menu = QMenu(self)
        self.export_folder_action = self.export_menu.addAction("Show in file manager")
        self.export_usb_action = self.export_menu.addAction("Write to USB stick…")
        self.export_burn_action = self.export_menu.addAction("Burn to disc…")
        self.export_button.setMenu(self.export_menu)
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return self.header

    def _build_distribution_panel(self) -> QGroupBox:
        box = QGroupBox("Distribution")
        self.iso_label = QLabel()
        self.iso_label.setWordWrap(True)
        self.iso_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Nusantara OS")
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("e.g. Nusantara OS 26.04 — made with Ubuntu Builder")
        self.locale_combo = QComboBox()
        self.locale_combo.setEditable(True)
        self.locale_combo.addItems(COMMON_LOCALES)
        self.locale_combo.lineEdit().setPlaceholderText("keep the image's default")
        self.welcome_edit = QPlainTextEdit()
        self.welcome_edit.setPlaceholderText("Welcome message shown at console login")
        self.welcome_edit.setFixedHeight(54)
        self.windows_check = QCheckBox("Remove Windows related files from the ISO")
        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Locale:", self.locale_combo)
        form.addRow("Welcome:", self.welcome_edit)
        form.addRow("", self.windows_check)
        layout = QVBoxLayout(box)
        layout.addWidget(self.iso_label)
        layout.addLayout(form)
        layout.addStretch(1)
        return box

    def _build_customize_panel(self) -> QGroupBox:
        box = QGroupBox("Customize")
        style = self.style()

        def action(text: str, icon, tooltip: str) -> QPushButton:  # noqa: ANN001
            button = QPushButton(icon, text)
            button.setObjectName("action")
            button.setIconSize(QSize(22, 22))
            button.setToolTip(tooltip)
            button.setMinimumHeight(38)
            return button

        self.console_button = action(
            "Console", style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon),
            "Root shell inside the image (apt, add-apt-repository, …)",
        )  # fmt: skip
        self.synaptic_button = action(
            "Synaptic", style.standardIcon(QStyle.StandardPixmap.SP_DriveCDIcon),
            "Graphical package manager running inside the image",
        )  # fmt: skip
        self.desktop_button = action(
            "Desktop", resources.toolbar_icon("style", "Dark"),
            "Start the image's desktop in a window (Xephyr)",
        )  # fmt: skip
        self.deb_button = action(
            "Install .deb", style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
            "Install local .deb packages into the image",
        )  # fmt: skip
        self.desktop_combo = QComboBox()
        for desktop in DesktopEnvironment:
            if desktop is not DesktopEnvironment.KEEP:
                self.desktop_combo.addItem(desktop.label, desktop)
        self.desktop_install_button = QPushButton("Install")
        grid = QGridLayout()
        grid.addWidget(self.console_button, 0, 0)
        grid.addWidget(self.synaptic_button, 0, 1)
        grid.addWidget(self.desktop_button, 1, 0)
        grid.addWidget(self.deb_button, 1, 1)
        desktop_row = QHBoxLayout()
        desktop_row.addWidget(self.desktop_combo, 1)
        desktop_row.addWidget(self.desktop_install_button)
        desktop_box = QGroupBox("Desktop environment")
        desktop_box.setLayout(desktop_row)
        hint = QLabel("More options — boot menu, splash, wallpaper, installer, commands — are in the Wizard.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout = QVBoxLayout(box)
        layout.addLayout(grid)
        layout.addWidget(desktop_box)
        layout.addWidget(hint)
        layout.addStretch(1)
        return box

    # -- slots used by the controller ------------------------------------------

    def set_toolbar_style(self, style: str) -> None:
        dark = style != "light"
        self.header.setStyleSheet(resources.header_style(dark))
        for widget in (self.header, *self.header.findChildren(QWidget)):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.header.update()
        variant = "Light" if dark else "Dark"  # light glyphs on the dark bar
        for button, icon in self._toolbar_buttons:
            button.setIcon(resources.toolbar_icon(icon, variant))

    def set_project_info(self, text: str) -> None:
        self.iso_label.setText(text)

    def distribution_config(self) -> CustomizationConfig:
        return CustomizationConfig(
            distro_name=self.name_edit.text().strip(),
            description=self.description_edit.text().strip(),
            locale=self.locale_combo.currentText().strip(),
            welcome_message=self.welcome_edit.toPlainText().strip(),
            remove_windows_files=self.windows_check.isChecked(),
        )

    def load_distribution(self, values: dict[str, object]) -> None:
        self.name_edit.setText(str(values.get("distro_name", "")))
        self.description_edit.setText(str(values.get("description", "")))
        self.locale_combo.setCurrentText(str(values.get("locale", "")))
        self.welcome_edit.setPlainText(str(values.get("welcome_message", "")))
        self.windows_check.setChecked(bool(values.get("remove_windows_files", False)))

    def set_actions_enabled(self, *, extracted: bool, idle: bool, built: bool, qemu: bool) -> None:
        ready = extracted and idle
        self.select_button.setEnabled(idle)
        self.remove_button.setEnabled(idle and extracted)
        self.settings_button.setEnabled(True)
        for widget in (
            self.wizard_button, self.build_button, self.console_button, self.synaptic_button,
            self.desktop_button, self.deb_button, self.desktop_install_button,
        ):  # fmt: skip
            widget.setEnabled(ready)
        self.qemu_button.setEnabled(qemu)
        self.qemu_bios_action.setEnabled(built)
        self.qemu_uefi_action.setEnabled(built)
        self.export_button.setEnabled(built)

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def set_progress(self, percent: int, stage: str) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(percent)
        if stage:
            self.stage_label.setText(stage)

    def set_busy(self, busy: bool, stage: str = "") -> None:
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress_bar.setRange(0, 0)  # indeterminate until progress arrives
            self.stage_label.setText(stage or "Working…")
        else:
            self.progress_bar.setRange(0, 100)
            self.stage_label.setText(stage or "Ready")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self.closing.emit(event)
