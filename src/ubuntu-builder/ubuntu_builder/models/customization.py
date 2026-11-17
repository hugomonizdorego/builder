"""User choices made in the Distribution panel and the Wizard.

The fields mirror the configuration files of the original Ubuntu Builder
2.x (``configs/DIST``, ``DESCRIPTION``, ``LOCALE``, ``WIN``, ``PACKAGES``,
``DEBS``, ``AUTOCOMMANDS``) plus what a 2026 image needs on top.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DesktopEnvironment(Enum):
    """Desktop environments that can be added to the image.

    ``packages`` are the Ubuntu meta-packages pulled in through apt — the
    successors of the presets the original "Install" script offered.
    """

    KEEP = ("Keep what the image ships", ())
    GNOME = ("GNOME — Ubuntu (ubuntu-desktop-minimal)", ("ubuntu-desktop-minimal",))
    KDE = ("KDE Plasma — Kubuntu (kubuntu-desktop)", ("kubuntu-desktop",))
    XFCE = ("Xfce — Xubuntu (xubuntu-desktop)", ("xubuntu-desktop",))
    LXQT = ("LXQt — Lubuntu (lubuntu-desktop)", ("lubuntu-desktop",))
    MATE = ("MATE — Ubuntu MATE (ubuntu-mate-desktop)", ("ubuntu-mate-desktop",))
    BUDGIE = ("Budgie — Ubuntu Budgie (ubuntu-budgie-desktop)", ("ubuntu-budgie-desktop",))
    CINNAMON = ("Cinnamon — Ubuntu Cinnamon (ubuntucinnamon-desktop)", ("ubuntucinnamon-desktop",))

    def __init__(self, label: str, packages: tuple[str, ...]) -> None:
        self.label = label
        self.packages = packages


@dataclass(slots=True)
class CustomizationConfig:
    """Declarative description of the changes to apply to the image.

    Empty strings / ``None`` / empty lists mean "leave untouched", so a
    default constructed config is a no-op.
    """

    # -- identity (Distribution panel) ---------------------------------------
    distro_name: str = ""
    description: str = ""  # DISTRIB_DESCRIPTION / PRETTY_NAME
    welcome_message: str = ""  # shown at console login (motd)
    locale: str = ""  # e.g. "id_ID.UTF-8"; default language of the image
    remove_windows_files: bool = False  # autorun.inf, wubi.exe, … on the ISO
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
    local_debs: list[str] = field(default_factory=list)  # .deb files on the host
    commands: str = ""  # shell script run as root inside the image, last
    # -- installer ------------------------------------------------------------
    autoinstall_path: str = ""  # Subiquity autoinstall.yaml on the host
    install_calamares: bool = False
    calamares_packages: list[str] = field(
        default_factory=lambda: ["calamares", "calamares-settings-ubuntu-common"]
    )

    def is_empty(self) -> bool:
        return self == CustomizationConfig()

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
