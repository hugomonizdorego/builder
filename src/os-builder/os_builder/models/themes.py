"""GTK, icon and cursor themes packaged in Ubuntu 26.04 LTS.

``names`` are the directory names the packages install (what GSettings and
the greeters expect), checked against the contents of the 26.04 packages.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThemePackage:
    package: str
    names: tuple[str, ...]


GTK_THEMES = (
    ThemePackage("yaru-theme-gtk", ("Yaru", "Yaru-dark", "Yaru-blue", "Yaru-blue-dark", "Yaru-purple-dark",
                                     "Yaru-sage-dark")),
    ThemePackage("gnome-themes-extra", ("Adwaita", "Adwaita-dark")),
    ThemePackage("arc-theme", ("Arc", "Arc-Dark", "Arc-Darker", "Arc-Lighter")),
    ThemePackage("orchis-gtk-theme", ("Orchis", "Orchis-Dark", "Orchis-Light")),
    ThemePackage("numix-gtk-theme", ("Numix",)),
    ThemePackage("breeze-gtk-theme", ("Breeze", "Breeze-Dark")),
    ThemePackage("greybird-gtk-theme", ("Greybird", "Greybird-dark")),
)

ICON_THEMES = (
    ThemePackage("yaru-theme-icon", ("Yaru", "Yaru-dark", "Yaru-blue", "Yaru-purple")),
    ThemePackage("papirus-icon-theme", ("Papirus", "Papirus-Dark", "Papirus-Light")),
    ThemePackage("adwaita-icon-theme", ("Adwaita",)),
    ThemePackage("kf6-breeze-icon-theme", ("breeze", "breeze-dark")),
    ThemePackage("numix-icon-theme-circle", ("Numix-Circle", "Numix-Circle-Light")),
    ThemePackage("numix-icon-theme", ("Numix", "Numix-Light")),
    ThemePackage("elementary-icon-theme", ("elementary",)),
    ThemePackage("elementary-xfce-icon-theme", ("elementary-xfce", "elementary-xfce-dark")),
    ThemePackage("suru-icon-theme", ("suru",)),
    ThemePackage("moka-icon-theme", ("Moka",)),
    ThemePackage("faba-icon-theme", ("Faba",)),
    ThemePackage("pop-icon-theme", ("Pop",)),
)

CURSOR_THEMES = (
    ThemePackage("yaru-theme-icon", ("Yaru",)),
    ThemePackage("adwaita-icon-theme", ("Adwaita",)),
    ThemePackage("breeze-cursor-theme", ("breeze_cursors", "Breeze_Light")),
    ThemePackage("bibata-cursor-theme", ("Bibata-Modern-Classic", "Bibata-Modern-Ice", "Bibata-Modern-Amber",
                                          "Bibata-Original-Classic", "Bibata-Original-Ice")),
    ThemePackage("dmz-cursor-theme", ("DMZ-White", "DMZ-Black")),
)


def package_for(name: str, catalog: tuple[ThemePackage, ...]) -> str | None:
    """The package that provides theme ``name``, if it is one we know."""
    return next((entry.package for entry in catalog if name in entry.names), None)
