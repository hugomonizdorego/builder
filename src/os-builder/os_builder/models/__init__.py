"""Plain data models (the "M" in MVC). No I/O beyond JSON state, no Qt."""

from os_builder.models.customization import CustomizationConfig
from os_builder.models.desktops import (
    DESKTOPS,
    DESKTOPS_BY_ID,
    DISPLAY_MANAGERS,
    DISPLAY_MANAGERS_BY_ID,
    Desktop,
    DisplayManager,
    SessionKind,
)
from os_builder.models.release import Catalog, Flavor, IsoImage, SupportStatus, UbuntuRelease
from os_builder.models.workspace import ProjectState, Workspace

__all__ = [
    "Catalog",
    "CustomizationConfig",
    "DESKTOPS",
    "DESKTOPS_BY_ID",
    "DISPLAY_MANAGERS",
    "DISPLAY_MANAGERS_BY_ID",
    "Desktop",
    "DisplayManager",
    "SessionKind",
    "Flavor",
    "IsoImage",
    "ProjectState",
    "SupportStatus",
    "UbuntuRelease",
    "Workspace",
]
