"""Page 4 — build the ISO and test it."""

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
    QVBoxLayout,
    QWidget,
)

from ubuntu_builder.core.backend import COMPRESSIONS
from ubuntu_builder.core.iso import MAX_VOLUME_ID


class BuildPage(QWidget):
    build_requested = pyqtSignal(Path, str, str)  # output, volume id, compression ("" = as source)
    test_requested = pyqtSignal(Path, bool)  # iso, UEFI mode

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.output_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)
        self.volume_edit = QLineEdit()
        self.volume_edit.setMaxLength(MAX_VOLUME_ID)
        self.compression_combo = QComboBox()
        self.compression_combo.addItem("Same as the source image", "")
        hints = ("smallest", "fast, slightly larger", "legacy", "fastest, largest")
        for name, hint in zip(COMPRESSIONS, hints, strict=True):
            self.compression_combo.addItem(f"{name} — {hint}", name)
        self.build_button = QPushButton("Build ISO")

        build_box = QGroupBox("Build")
        form = QFormLayout(build_box)
        form.addRow("Output file:", output_row)
        form.addRow("Volume label:", self.volume_edit)
        form.addRow("Squashfs compression:", self.compression_combo)
        form.addRow("", self.build_button)

        self.uefi_check = QCheckBox("Boot in UEFI mode (needs OVMF)")
        self.test_button = QPushButton("Test in QEMU")
        self.test_note = QLabel()
        self.test_note.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.addWidget(self.uefi_check)
        test_row.addStretch(1)
        test_row.addWidget(self.test_button)
        test_box = QGroupBox("Test")
        test_layout = QVBoxLayout(test_box)
        test_layout.addLayout(test_row)
        test_layout.addWidget(self.test_note)

        explanation = QLabel(
            "The build repacks the file system, regenerates md5sum.txt and writes a hybrid ISO that "
            "boots on legacy BIOS and UEFI. The signed boot loader of the source image is reused, so "
            "Secure Boot keeps working."
        )
        explanation.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(explanation)
        layout.addWidget(build_box)
        layout.addWidget(test_box)
        layout.addStretch(1)

        self.build_button.clicked.connect(self._emit_build)
        self.test_button.clicked.connect(lambda: self.test_requested.emit(self.output(), self.uefi_check.isChecked()))

    def output(self) -> Path:
        return Path(self.output_edit.text().strip()).expanduser()

    def set_defaults(self, output: Path, volume_id: str) -> None:
        if not self.output_edit.text():
            self.output_edit.setText(str(output))
        if not self.volume_edit.text():
            self.volume_edit.setText(volume_id)

    def set_state(self, *, can_build: bool, qemu: bool, ovmf: bool) -> None:
        self.build_button.setEnabled(can_build)
        self.test_button.setEnabled(qemu)
        self.uefi_check.setEnabled(qemu and ovmf)
        if not ovmf:
            self.uefi_check.setChecked(False)
        self.test_note.setText("" if qemu else "Install qemu-system-x86 (and ovmf for UEFI) to test images here.")

    def _emit_build(self) -> None:
        self.build_requested.emit(self.output(), self.volume_edit.text().strip(), self.compression_combo.currentData())

    def _choose_output(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(self, "Save ISO as", self.output_edit.text(), "ISO images (*.iso)")
        if chosen:
            self.output_edit.setText(chosen if chosen.endswith(".iso") else chosen + ".iso")
