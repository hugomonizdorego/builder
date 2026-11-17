"""Small input widgets used by the Wizard tabs."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.svg)"
PNG_FILTER = "PNG images (*.png)"
PNG_JPEG_FILTER = "PNG or JPEG images (*.png *.jpg *.jpeg)"


class FilePicker(QWidget):
    """Line edit with a "Browse…" button."""

    changed = pyqtSignal(str)

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
        self.edit.textChanged.connect(self.changed)

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, self._caption, str(Path.home()), self._filter)
        if chosen:
            self.edit.setText(chosen)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)


class ImagePicker(QWidget):
    """File picker with a thumbnail preview."""

    changed = pyqtSignal(str)

    def __init__(self, caption: str, name_filter: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.picker = FilePicker(caption, name_filter)
        self.preview = QLabel("No image")
        self.preview.setFixedSize(160, 90)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("QLabel { border: 1px solid palette(mid); color: palette(mid); }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.picker, 1)
        layout.addWidget(self.preview)
        self.picker.changed.connect(self._update)
        self.picker.changed.connect(self.changed)

    def _update(self, path: str) -> None:
        pixmap = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if pixmap.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText("No image" if not path else "Not an image")
        else:
            self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))  # fmt: skip

    def path(self) -> str:
        return self.picker.path()


class ColorButton(QPushButton):
    """Button showing a colour; opens a colour dialog. Value is "#RRGGBB"."""

    changed = pyqtSignal(str)

    def __init__(self, color: str = "#000000", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedWidth(110)
        self.clicked.connect(self._choose)
        self._refresh()

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(self._color, self, "Background colour")
        if chosen.isValid():
            self._color = chosen
            self._refresh()
            self.changed.emit(self.color())

    def _refresh(self) -> None:
        text = "white" if self._color.lightness() < 128 else "black"
        self.setText(self.color())
        self.setStyleSheet(f"QPushButton {{ background: {self.color()}; color: {text}; }}")

    def color(self) -> str:
        return self._color.name()


class PathList(QWidget):
    """A list of files with Add / Remove buttons."""

    def __init__(self, caption: str, name_filter: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._caption, self._filter = caption, name_filter
        self.list = QListWidget()
        self.list.setFixedHeight(70)
        add, remove = QPushButton("Add…"), QPushButton("Remove")
        add.clicked.connect(self._add)
        remove.clicked.connect(lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()])
        buttons = QVBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)

    def _add(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, self._caption, str(Path.home()), self._filter)
        for path in files:
            if not self.list.findItems(path, Qt.MatchFlag.MatchExactly):
                self.list.addItem(path)

    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]
