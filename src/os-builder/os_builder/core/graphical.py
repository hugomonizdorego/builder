"""Graphical programs inside the image: Synaptic and a nested desktop.

The original Ubuntu Builder offered two buttons that ran X11 programs from
the chroot: *Synaptic* (on the host's display) and *Desktop* (the image's
full desktop session inside a Xephyr window). Both still work in 2026:

* on X11 and on Wayland (through XWayland) the host's X socket is
  bind-mounted into the image and the host's X authority is handed over;
* Xephyr provides a nested X server for the desktop session, so the image's
  session manager never touches the host session.

Everything runs as root inside a :class:`ChrootSession`, which keeps the
usual guarantees: private ``/run``, no service starts, full unmount.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from os_builder import APP_ID
from os_builder.core.chroot import ChrootSession, stop_processes_in
from os_builder.core.errors import BuilderError, OperationCancelledError
from os_builder.core.runner import CommandRunner

X11_SOCKETS = Path("/tmp/.X11-unix")
_RESOLUTION = re.compile(r"^\d{3,5}x\d{3,5}$")

#: Session commands tried in order, like the original "Desktop" script.
#: (marker file in the image, command, extra environment). The wlroots
#: compositors and Wayfire run nested in the Xephyr window through their X11
#: backend with software rendering. GNOME 49+ and Hyprland have no nested
#: X11 mode: test those images with QEMU.
_WLROOTS_NESTED = {"WLR_BACKENDS": "x11", "WLR_RENDERER": "pixman", "WLR_NO_HARDWARE_CURSORS": "1"}
DESKTOP_SESSIONS: tuple[tuple[str, tuple[str, ...], dict[str, str]], ...] = (
    ("/usr/share/xsessions/ubuntu-xorg.desktop", ("gnome-session", "--session=ubuntu"), {}),
    ("/usr/bin/startplasma-x11", ("startplasma-x11",), {}),
    ("/usr/bin/xfce4-session", ("xfce4-session",), {}),
    ("/usr/bin/startlxqt", ("startlxqt",), {}),
    ("/usr/bin/mate-session", ("mate-session",), {}),
    ("/usr/bin/budgie-desktop", ("budgie-desktop",), {}),
    ("/usr/bin/cinnamon-session", ("cinnamon-session",), {}),
    ("/usr/share/xsessions/gnome-flashback-metacity.desktop",
     ("gnome-session", "--session=gnome-flashback-metacity"), {}),
    ("/usr/bin/startlxde", ("startlxde",), {}),
    ("/usr/bin/enlightenment_start", ("enlightenment_start",), {}),
    ("/usr/bin/sway", ("sway", "--unsupported-gpu"), _WLROOTS_NESTED),
    ("/usr/bin/labwc", ("labwc",), _WLROOTS_NESTED),
    ("/usr/bin/wayfire", ("wayfire",), _WLROOTS_NESTED),
    ("/usr/bin/i3", ("i3",), {}),
    ("/usr/bin/awesome", ("awesome",), {}),
    ("/usr/bin/bspwm", ("sh", "-c", "sxhkd & exec bspwm"), {}),
    ("/usr/bin/qtile", ("qtile", "start"), {}),
    ("/usr/bin/xmonad", ("xmonad",), {}),
    ("/usr/bin/herbstluftwm", ("herbstluftwm",), {}),
    ("/usr/bin/icewm-session", ("icewm-session",), {}),
    ("/usr/bin/startfluxbox", ("startfluxbox",), {}),
    ("/usr/bin/openbox-session", ("openbox-session",), {}),
)
#: Installed but impossible to nest: explain instead of failing silently.
UNNESTABLE = (
    ("/usr/bin/gnome-shell", "GNOME 49 and later run on Wayland only"),
    ("/usr/bin/Hyprland", "Hyprland cannot run inside an X11 window"),
    ("/usr/bin/startplasma-wayland", "this Plasma installation has no X11 session"),
)


class GraphicalError(BuilderError):
    """No display available, or nothing to show."""


def _display_number(display: str) -> str:
    match = re.fullmatch(r"(?:unix)?:(\d+)(?:\.\d+)?", display)
    if not match:
        raise GraphicalError(f"Only local X displays are supported (DISPLAY={display!r}).")
    return match.group(1)


def _share_x_socket(session: ChrootSession, runner: CommandRunner) -> None:
    """Make the host's X sockets visible inside the image."""
    target = session.rootfs / "tmp" / ".X11-unix"
    target.mkdir(parents=True, exist_ok=True)
    runner.run(["mount", "--bind", X11_SOCKETS, target], quiet=True)


def _x_environment(session: ChrootSession, display: str, xauthority: str | None) -> dict[str, str]:
    env = {
        "DISPLAY": display,
        "GDK_BACKEND": "x11",  # never try the host's Wayland socket from the chroot
        "QT_QPA_PLATFORM": "xcb",
        "NO_AT_BRIDGE": "1",
        "XDG_RUNTIME_DIR": "/run/user/0",
    }
    (session.rootfs / "run/user/0").mkdir(parents=True, exist_ok=True, mode=0o700)
    if xauthority and Path(xauthority).is_file():
        copy = session.rootfs / "run" / f"{APP_ID}.xauth"  # /run is the session's private tmpfs
        shutil.copyfile(xauthority, copy)
        copy.chmod(0o600)
        env["XAUTHORITY"] = f"/run/{copy.name}"
    return env


def run_synaptic(session: ChrootSession, runner: CommandRunner) -> None:
    """Install Synaptic in the image if needed and open it on the host display."""
    display = os.environ.get("DISPLAY", "")
    if not display:
        raise GraphicalError("No X display available (DISPLAY is not set).")
    _display_number(display)
    if not session.has_command("synaptic"):
        runner.log("Synaptic is not installed in the image — installing it.")
        session.run(["apt-get", "update"])
        session.run(["apt-get", "install", "-y", "--no-install-recommends", "synaptic"])
    _share_x_socket(session, runner)
    env = _x_environment(session, display, os.environ.get("XAUTHORITY"))
    runner.log("Synaptic is running. Close it to continue.")
    runner.run(session.argv(["synaptic"], extra_env=env), check=False, display="(image) # synaptic")


def find_desktop_session(rootfs: Path) -> tuple[tuple[str, ...], dict[str, str]] | None:
    for marker, command, env in DESKTOP_SESSIONS:
        if (rootfs / marker.lstrip("/")).exists():
            return command, env
    return None


def _free_display() -> int:
    for number in range(9, 64):
        if not (X11_SOCKETS / f"X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return number
    raise GraphicalError("No free X display number found.")


def run_desktop(session: ChrootSession, runner: CommandRunner, resolution: str = "1280x800") -> None:
    """Start the image's desktop session in a Xephyr window on the host."""
    if not _RESOLUTION.match(resolution):
        raise GraphicalError(f"Invalid resolution: {resolution!r} (expected e.g. 1280x800)")
    if not os.environ.get("DISPLAY"):
        raise GraphicalError("No X display available (DISPLAY is not set).")
    xephyr = shutil.which("Xephyr")
    if xephyr is None:
        raise GraphicalError("Xephyr is not installed. Install the package xserver-xephyr.")
    found = find_desktop_session(session.rootfs)
    if found is None:
        for marker, reason in UNNESTABLE:
            if (session.rootfs / marker.lstrip("/")).exists():
                raise GraphicalError(
                    f"The desktop in this image cannot be shown in a window: {reason}. "
                    "Build the ISO and test it with QEMU instead."
                )
        raise GraphicalError(
            "No desktop session was found in the image. Install a desktop environment first "
            "(Customize → Desktop environment)."
        )
    command, session_env = found

    number = _free_display()
    runner.log(f"$ Xephyr :{number} -screen {resolution}")
    server = subprocess.Popen(  # noqa: S603 - fixed argv
        [xephyr, f":{number}", "-ac", "-br", "-noreset", "-resizeable", "-screen", resolution,
         "-title", "OS Builder — Desktop"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 15
        while not (X11_SOCKETS / f"X{number}").exists():
            runner.check_cancelled()
            if server.poll() is not None:
                raise GraphicalError("Xephyr exited immediately; is the host display reachable?")
            if time.monotonic() > deadline:
                raise GraphicalError("Xephyr did not start within 15 seconds.")
            time.sleep(0.2)
        _share_x_socket(session, runner)
        env = {**_x_environment(session, f":{number}", None), **session_env}  # -ac: no authority needed
        runner.log(f"Desktop session {command[0]} started. Log out (or close the window) to continue.")
        session_cmd = ["dbus-run-session", "--", *command] if session.has_command("dbus-run-session") else list(command)
        try:
            runner.run(session.argv(session_cmd, extra_env=env), check=False, display=f"(image) # {command[0]}")
        except OperationCancelledError:
            runner.log("Desktop session stopped.")
            raise
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        stop_processes_in(runner, session.rootfs)
        runner.log("Desktop session ended.")

