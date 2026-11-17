"""Desktop environments, window managers and login managers on offer.

Every package name below was checked against the Ubuntu 26.04 LTS
("resolute") archive. Desktops come as the flavor meta-packages, so they
pull in the full, integrated experience of that flavor; compositors and
window managers come with a small, working toolkit (terminal, launcher,
bar, wallpaper, notifications) so the resulting session is usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionKind(Enum):
    DESKTOP = "Desktop environments"
    WAYLAND = "Wayland compositors"
    X11 = "X11 window managers"


@dataclass(frozen=True, slots=True)
class Desktop:
    id: str
    name: str
    kind: SessionKind
    packages: tuple[str, ...]
    #: Login manager installed with it unless the user picks another one.
    display_manager: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.packages[0]})"


# Session bits every bare compositor / window manager needs on a server or
# minimal base: audio, network applet, authentication agent, portals.
_COMMON = ("pipewire-audio", "wireplumber", "network-manager-gnome", "policykit-1-gnome", "xdg-desktop-portal-gtk")
_WAYLAND = (*_COMMON, "xwayland", "foot", "fuzzel", "waybar", "mako-notifier", "swaybg", "grim", "slurp", "kanshi")
_X11 = (*_COMMON, "xorg", "xterm", "suckless-tools", "picom", "feh", "dunst")

DESKTOPS: tuple[Desktop, ...] = (
    # -- full desktops (flavor meta-packages) ----------------------------------
    Desktop("gnome", "GNOME 50 — Ubuntu", SessionKind.DESKTOP, ("ubuntu-desktop-minimal",), "gdm3",
            "Wayland only since GNOME 49."),
    Desktop("gnome-full", "GNOME 50 — Ubuntu, full", SessionKind.DESKTOP, ("ubuntu-desktop",), "gdm3"),
    Desktop("gnome-flashback", "GNOME Flashback", SessionKind.DESKTOP, ("gnome-session-flashback",), "gdm3"),
    Desktop("kde", "KDE Plasma 6 — Kubuntu", SessionKind.DESKTOP, ("kubuntu-desktop",), "sddm"),
    Desktop("kde-minimal", "KDE Plasma 6, minimal", SessionKind.DESKTOP,
            ("plasma-desktop", "kwin-wayland", "sddm-theme-breeze", "konsole", "dolphin"), "sddm"),
    Desktop("xfce", "Xfce 4.20 — Xubuntu", SessionKind.DESKTOP, ("xubuntu-desktop",), "lightdm-gtk"),
    Desktop("xfce-minimal", "Xfce 4.20, minimal", SessionKind.DESKTOP, ("xfce4", "xfce4-terminal", *_COMMON),
            "lightdm-gtk"),
    Desktop("lxqt", "LXQt — Lubuntu", SessionKind.DESKTOP, ("lubuntu-desktop",), "sddm"),
    Desktop("mate", "MATE — Ubuntu MATE", SessionKind.DESKTOP, ("ubuntu-mate-desktop",), "lightdm-slick"),
    Desktop("budgie", "Budgie 10.10 — Ubuntu Budgie", SessionKind.DESKTOP, ("ubuntu-budgie-desktop",),
            "lightdm-slick"),
    Desktop("cinnamon", "Cinnamon 6.4 — Ubuntu Cinnamon", SessionKind.DESKTOP, ("ubuntucinnamon-desktop",),
            "lightdm-slick"),
    Desktop("unity", "Unity — Ubuntu Unity", SessionKind.DESKTOP, ("ubuntu-unity-desktop",), "lightdm-slick"),
    Desktop("edubuntu", "Edubuntu", SessionKind.DESKTOP, ("edubuntu-desktop",), "gdm3"),
    Desktop("studio", "Ubuntu Studio", SessionKind.DESKTOP, ("ubuntustudio-desktop",), "sddm"),
    Desktop("kylin", "Ubuntu Kylin (UKUI)", SessionKind.DESKTOP, ("ubuntukylin-desktop",), "lightdm-gtk"),
    Desktop("lxde", "LXDE", SessionKind.DESKTOP, ("lxde", *_COMMON), "lxdm"),
    Desktop("enlightenment", "Enlightenment 0.27", SessionKind.DESKTOP, ("enlightenment", "xterm", *_COMMON),
            "lightdm-gtk"),
    # -- Wayland compositors ----------------------------------------------------
    Desktop("hyprland", "Hyprland 0.53", SessionKind.WAYLAND,
            ("hyprland", "hyprpaper", "hyprlock", "hypridle", "xdg-desktop-portal-hyprland", *_WAYLAND), "greetd"),
    Desktop("sway", "Sway 1.11", SessionKind.WAYLAND,
            ("sway", "swayidle", "swaylock", "xdg-desktop-portal-wlr", *_WAYLAND), "greetd"),
    Desktop("labwc", "labwc 0.9 (Openbox-like)", SessionKind.WAYLAND,
            ("labwc", "swaylock", "xdg-desktop-portal-wlr", *_WAYLAND), "greetd"),
    Desktop("wayfire", "Wayfire 0.10", SessionKind.WAYLAND,
            ("wayfire", "wf-shell", "wcm", "xdg-desktop-portal-wlr", *_WAYLAND), "greetd"),
    # -- X11 window managers --------------------------------------------------
    Desktop("i3", "i3 4.25", SessionKind.X11, ("i3", "i3status", "i3lock", *_X11), "lightdm-gtk"),
    Desktop("openbox", "Openbox 3.6", SessionKind.X11, ("openbox", "obconf", "tint2", *_X11), "lightdm-gtk"),
    Desktop("awesome", "awesome 4.3", SessionKind.X11, ("awesome", *_X11), "lightdm-gtk"),
    Desktop("bspwm", "bspwm 0.9", SessionKind.X11, ("bspwm", "sxhkd", "polybar", *_X11), "lightdm-gtk"),
    Desktop("qtile", "Qtile 0.34", SessionKind.X11, ("qtile", *_X11), "lightdm-gtk"),
    Desktop("xmonad", "xmonad 0.18", SessionKind.X11, ("xmonad", *_X11), "lightdm-gtk"),
    Desktop("herbstluftwm", "herbstluftwm 0.9", SessionKind.X11, ("herbstluftwm", *_X11), "lightdm-gtk"),
    Desktop("icewm", "IceWM 4.0", SessionKind.X11, ("icewm", *_X11), "lightdm-gtk"),
    Desktop("fluxbox", "Fluxbox 1.3", SessionKind.X11, ("fluxbox", *_X11), "lightdm-gtk"),
)

DESKTOPS_BY_ID = {desktop.id: desktop for desktop in DESKTOPS}


@dataclass(frozen=True, slots=True)
class DisplayManager:
    """A login manager and how to make it the system's default."""

    id: str
    name: str
    packages: tuple[str, ...]
    service: str  # systemd unit linked as display-manager.service
    binary: str  # written to /etc/X11/default-display-manager
    #: Autologin in the live session is handled by casper for these.
    live_autologin: bool = True


DISPLAY_MANAGERS: tuple[DisplayManager, ...] = (
    DisplayManager("gdm3", "GDM 50 (GNOME)", ("gdm3",), "gdm3.service", "/usr/sbin/gdm3"),
    DisplayManager("sddm", "SDDM 0.21 (KDE, LXQt)", ("sddm", "sddm-theme-breeze"), "sddm.service", "/usr/bin/sddm"),
    DisplayManager("lightdm-slick", "LightDM + Slick Greeter", ("lightdm", "slick-greeter"), "lightdm.service",
                   "/usr/sbin/lightdm"),
    DisplayManager("lightdm-gtk", "LightDM + GTK Greeter", ("lightdm", "lightdm-gtk-greeter"), "lightdm.service",
                   "/usr/sbin/lightdm"),
    DisplayManager("greetd", "greetd + tuigreet (Wayland friendly)", ("greetd", "tuigreet"), "greetd.service",
                   "/usr/sbin/greetd", live_autologin=False),
    DisplayManager("lxdm", "LXDM", ("lxdm",), "lxdm.service", "/usr/sbin/lxdm"),
    DisplayManager("xdm", "XDM", ("xdm",), "xdm.service", "/usr/bin/xdm", live_autologin=False),
)

DISPLAY_MANAGERS_BY_ID = {dm.id: dm for dm in DISPLAY_MANAGERS}
KEEP_DISPLAY_MANAGER = "keep"  # install no login manager, keep the image's

#: /etc/greetd/config.toml: tuigreet lists the Wayland and X11 sessions.
GREETD_CONFIG = """[terminal]
vt = 7

[default_session]
command = "tuigreet --time --remember --remember-user-session --asterisks"
user = "_greetd"
"""
