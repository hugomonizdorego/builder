"""Plain data models (the "M" in MVC). No I/O beyond JSON state, no Qt."""

from ubuntu_builder.models.customization import CustomizationConfig, DesktopEnvironment
from ubuntu_builder.models.release import Catalog, Flavor, IsoImage, SupportStatus, UbuntuRelease
from ubuntu_builder.models.workspace import ProjectState, Workspace

__all__ = [
    "Catalog",
    "CustomizationConfig",
    "DesktopEnvironment",
    "Flavor",
    "IsoImage",
    "ProjectState",
    "SupportStatus",
    "UbuntuRelease",
    "Workspace",
]
