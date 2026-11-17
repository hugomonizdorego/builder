"""User choices made in the Distribution panel and the Wizard.

The fields mirror the configuration files of the original Ubuntu Builder
2.x (``configs/DIST``, ``DESCRIPTION``, ``LOCALE``, ``WIN``, ``PACKAGES``,
``DEBS``, ``AUTOCOMMANDS``) plus what a 2026 image needs on top.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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
    locale: str = ""  # e.g. "en_US.UTF-8"; default language of the image
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
    desktop: str = ""  # id from models.desktops.DESKTOPS; "" keeps the image's
    display_manager: str = ""  # id from models.desktops.DISPLAY_MANAGERS, "" = the desktop's default, "keep"
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
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomizationConfig:
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "desktop" in data:
            desktop = str(data["desktop"] or "").lower()
            data["desktop"] = "" if desktop == "keep" else desktop  # 3.0 files used enum names
        return cls(**data)
