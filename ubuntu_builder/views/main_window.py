"""Main window: navigation, pages, progress bar and live log."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QFontDatabase
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ubuntu_builder import APP_NAME, __version__
from ubuntu_builder.views.build_page import BuildPage
from ubuntu_builder.views.customize_page import CustomizePage
from ubuntu_builder.views.source_page import SourcePage
from ubuntu_builder.views.terminal import TerminalPage


class MainWindow(QMainWindow):
    """Pure view: owns the widgets, knows nothing about the backend."""

    cancel_requested = pyqtSignal()
    closing = pyqtSignal(object)  # QCloseEvent — the controller may veto

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1040, 760)

        self.source_page = SourcePage()
        self.terminal_page = TerminalPage()
        self.customize_page = CustomizePage()
        self.build_page = BuildPage()

        self.navigation = QListWidget()
        self.navigation.setFixedWidth(170)
        self.pages = QStackedWidget()
        for title, page in (
            ("1. Source image", self.source_page),
            ("2. Terminal (chroot)", self.terminal_page),
            ("3. Customize", self.customize_page),
            ("4. Build ISO", self.build_page),
        ):
            self.navigation.addItem(title)
            self.pages.addWidget(page)
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)

        # -- progress row -------------------------------------------------------
        self.stage_label = QLabel("Ready")
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

        # -- live log -----------------------------------------------------------
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)  # bounded memory for long builds
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText("Command output appears here.")

        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.navigation)
        top_layout.addWidget(self.pages, 1)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addLayout(progress_row)
        bottom_layout.addWidget(self.log_view, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.addWidget(splitter)
        self.setCentralWidget(container)

    # -- slots used by the controller ------------------------------------------

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def set_progress(self, percent: int, stage: str) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(percent)
        if stage:
            self.stage_label.setText(stage)

    def set_busy(self, busy: bool, stage: str = "") -> None:
        """Toggle the "operation running" state of the status row."""
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress_bar.setRange(0, 0)  # indeterminate until progress arrives
            self.stage_label.setText(stage or "Working…")
        else:
            self.progress_bar.setRange(0, 100)
            self.stage_label.setText(stage or "Ready")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self.closing.emit(event)
