"""User choices made on the "Customize" page."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DesktopEnvironment(Enum):
    """Desktop environments that can be added to the image.

    ``packages`` are the Ubuntu meta-packages pulled in through apt.
    """

    KEEP = ("Keep what the image ships", ())
    GNOME = ("GNOME (ubuntu-desktop-minimal)", ("ubuntu-desktop-minimal",))
    KDE = ("KDE Plasma (kubuntu-desktop)", ("kubuntu-desktop",))
    XFCE = ("Xfce (xubuntu-desktop)", ("xubuntu-desktop",))

    def __init__(self, label: str, packages: tuple[str, ...]) -> None:
        self.label = label
        self.packages = packages


@dataclass(slots=True)
class CustomizationConfig:
    """Declarative description of the changes to apply to the image.

    Empty strings / ``None`` mean "leave untouched", so applying a default
    constructed config is a no-op.
    """

    # -- identity -----------------------------------------------------------
    distro_name: str = ""
    # -- boot ---------------------------------------------------------------
    plymouth_theme: str = ""  # name of a theme installed in the image
    grub_timeout: int | None = None  # seconds; boot menu of the live ISO
    grub_extra_kernel_args: str = ""  # appended to every linux line
    # -- look & feel ----------------------------------------------------------
    wallpaper_path: str = ""  # image file on the host
    gtk_theme: str = ""
    icon_theme: str = ""
    # -- software -------------------------------------------------------------
    desktop: DesktopEnvironment = DesktopEnvironment.KEEP
    extra_packages: list[str] = field(default_factory=list)
    remove_packages: list[str] = field(default_factory=list)
    # -- installer ------------------------------------------------------------
    autoinstall_path: str = ""  # Subiquity autoinstall.yaml on the host
    install_calamares: bool = False
    calamares_packages: list[str] = field(
        default_factory=lambda: ["calamares", "calamares-settings-ubuntu-common"]
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["desktop"] = self.desktop.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomizationConfig:
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "desktop" in data:
            data["desktop"] = DesktopEnvironment[data["desktop"]]
        return cls(**data)
