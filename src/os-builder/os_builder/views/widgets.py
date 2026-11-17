"""Small reusable widgets."""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QComboBox, QWidget

from os_builder.models import DESKTOPS, DESKTOPS_BY_ID, DISPLAY_MANAGERS, SessionKind


class DesktopPicker(QObject):
    """Two linked combo boxes: desktop / window manager, and login manager.

    Picking a desktop preselects its usual login manager; the user can
    still choose another one.
    """

    def __init__(self, *, keep_option: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.desktop_combo = QComboBox(parent)
        self.dm_combo = QComboBox(parent)
        model = QStandardItemModel(self.desktop_combo)
        if keep_option:
            keep = QStandardItem("Keep what the image ships")
            keep.setData("", Qt.ItemDataRole.UserRole)
            model.appendRow(keep)
        for kind in SessionKind:
            header = QStandardItem(f"— {kind.value} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)  # a non selectable group title
            model.appendRow(header)
            for desktop in (d for d in DESKTOPS if d.kind is kind):
                item = QStandardItem(f"   {desktop.label}")
                item.setData(desktop.id, Qt.ItemDataRole.UserRole)
                item.setToolTip(" ".join(desktop.packages) + (f"\n{desktop.note}" if desktop.note else ""))
                model.appendRow(item)
        self.desktop_combo.setModel(model)
        self.desktop_combo.setCurrentIndex(0 if keep_option else 1)

        self.dm_combo.addItem("Automatic (the desktop's default)", "")
        for manager in DISPLAY_MANAGERS:
            self.dm_combo.addItem(manager.name, manager.id)
        self.dm_combo.addItem("Keep the image's login manager", "keep")
        self.desktop_combo.currentIndexChanged.connect(self._sync_hint)
        self._sync_hint()

    def desktop_id(self) -> str:
        return str(self.desktop_combo.currentData(Qt.ItemDataRole.UserRole) or "")

    def display_manager_id(self) -> str:
        """A login manager id, "" for the desktop's default, or "keep"."""
        return str(self.dm_combo.currentData() or "")

    def _sync_hint(self) -> None:
        desktop = DESKTOPS_BY_ID.get(self.desktop_id())
        default = next((m.name for m in DISPLAY_MANAGERS if desktop and m.id == desktop.display_manager), "none")
        self.dm_combo.setItemText(0, f"Automatic ({default})" if desktop else "Automatic")
