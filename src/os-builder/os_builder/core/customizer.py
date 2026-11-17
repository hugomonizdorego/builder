"""Apply a :class:`CustomizationConfig` to the extracted image.

File level changes (boot menu, wallpaper, autoinstall …) are made directly;
everything involving packages runs through apt inside a
:class:`~os_builder.core.chroot.ChrootSession`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from os_builder import APP_ID
from os_builder.core.chroot import ChrootSession
from os_builder.core.errors import BuilderError
from os_builder.core.runner import CommandRunner
from os_builder.models.customization import CustomizationConfig
from os_builder.models.desktops import (
    DESKTOPS_BY_ID,
    DISPLAY_MANAGERS_BY_ID,
    GREETD_CONFIG,
    KEEP_DISPLAY_MANAGER,
    DisplayManager,
)
from os_builder.models.workspace import Workspace

_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+(:[a-z0-9]+)?$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+/-]*$")  # "/" for names like "DebION GNU/Linux"
_THEME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]*$")
_UBUNTU_WORD = re.compile(r"\b\w*[Uu]buntu\b(?:[ -](?:Server|Studio|MATE|Budgie|Cinnamon|Kylin|Unity)\b)?")
_MENU_TITLE = re.compile(r"^(\s*(?:menuentry|submenu)\s+)(['\"])(.*?)\2", re.MULTILINE)
_LINUX_LINE = re.compile(r"^(\s*linux(?:efi)?\s+\S+)(.*)$", re.MULTILINE)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
_LOCALE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?(@[a-z]+)?(\.UTF-8)?$")
_FREE_TEXT_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: Files of the Windows autorun / Wubi era that the original Ubuntu Builder
#: offered to strip from the ISO. Modern images rarely carry them.
WINDOWS_FILES = (
    "autorun.inf", "start.bmp", "start.exe", "start.ini", "ubuntu.ico", "kubuntu.ico", "xubuntu.ico",
    "lubuntu.ico", "wubi-cdboot.exe", "wubi.exe", "umenu.exe", "usb-creator.exe", "mint4win.exe",
)  # fmt: skip
WINDOWS_DIRS = ("bin", "disctree", "pics", "programs")
WELCOME_FILE = f"etc/{APP_ID}/welcome"
_WELCOME_SCRIPT = f"""#!/bin/sh
# Installed by {APP_ID}: replaces Ubuntu's help text with the distribution's welcome message.
[ -r /{WELCOME_FILE} ] && {{ echo; cat /{WELCOME_FILE}; echo; }}
exit 0
"""


def normalize_locale(locale: str) -> str:
    """``en_US`` → ``en_US.UTF-8``; raises for anything that is not a locale."""
    locale = locale.strip()
    if not _LOCALE.match(locale):
        raise CustomizationError(f"Not a valid locale: {locale!r} (expected e.g. en_US.UTF-8)")
    if "_" in locale and not locale.endswith(".UTF-8"):
        locale += ".UTF-8"
    return locale
OVERRIDE_FILE = f"usr/share/glib-2.0/schemas/90_{APP_ID}.gschema.override"


class CustomizationError(BuilderError):
    """A customization setting is invalid or could not be applied."""


def validate_packages(packages: list[str]) -> list[str]:
    """Reject anything that is not a plain Debian package name."""
    for name in packages:
        if not _PACKAGE.match(name):
            raise CustomizationError(f"Not a valid package name: {name!r}")
    return packages


# -- boot menu (pure text transformations, unit tested) ----------------------


def set_grub_timeout(config_text: str, seconds: int) -> str:
    """Set (or add) ``set timeout=`` in a grub.cfg."""
    if re.search(r"^\s*set timeout=", config_text, re.MULTILINE):
        return re.sub(r"^(\s*)set timeout=.*$", rf"\g<1>set timeout={seconds}", config_text, flags=re.MULTILINE)
    return f"set timeout={seconds}\n{config_text}"


def add_kernel_args(config_text: str, extra: str) -> str:
    """Append ``extra`` to every ``linux`` line, before the ``---`` marker.

    Arguments before ``---`` only affect the live session, which is what a
    remaster wants. Arguments already present are not added twice.
    """
    wanted = extra.split()

    def patch(match: re.Match[str]) -> str:
        head, _, tail = match.group(2).partition("---")
        present = head.split()
        missing = [arg for arg in wanted if arg not in present]
        if not missing:
            return match.group(0)
        head = " " + " ".join(present + missing)
        return match.group(1) + head + (" ---" + tail if "---" in match.group(2) else "")

    return _LINUX_LINE.sub(patch, config_text)


def rename_menu_entries(config_text: str, distro_name: str) -> str:
    """Replace the Ubuntu product name in boot menu titles."""

    def patch(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{_UBUNTU_WORD.sub(distro_name, match.group(3))}{match.group(2)}"

    return _MENU_TITLE.sub(patch, config_text)


def _set_key(text: str, key: str, value: str) -> str:
    """Set ``KEY="value"`` in an os-release style file."""
    line = f'{key}="{value}"'
    if re.search(rf"^{key}=", text, re.MULTILINE):
        return re.sub(rf"^{key}=.*$", line, text, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n{line}\n"


class Customizer:
    """Applies one configuration to one workspace."""

    def __init__(self, workspace: Workspace, runner: CommandRunner) -> None:
        self._ws = workspace
        self._runner = runner

    def validate(self, config: CustomizationConfig) -> None:
        """Fail early — before anything is mounted or changed."""
        if config.distro_name and not _SAFE_NAME.match(config.distro_name):
            raise CustomizationError("The distribution name may only contain letters, digits, spaces and . _ + - /")
        for name in (config.plymouth_theme, config.gtk_theme, config.icon_theme):
            if name and not _THEME_NAME.match(name):
                raise CustomizationError(f"Not a valid theme name: {name!r}")
        if config.grub_timeout is not None and not -1 <= config.grub_timeout <= 600:
            raise CustomizationError("The boot menu timeout must be between -1 and 600 seconds.")
        if re.search(r"[\n\r\"'`$\\]", config.grub_extra_kernel_args):
            raise CustomizationError("Kernel arguments must not contain quotes, backslashes or '$'.")
        if config.wallpaper_path:
            wallpaper = Path(config.wallpaper_path)
            if not wallpaper.is_file() or wallpaper.suffix.lower() not in _IMAGE_SUFFIXES:
                raise CustomizationError(f"Wallpaper is not an image file: {wallpaper}")
        if config.autoinstall_path:
            try:
                document = yaml.safe_load(Path(config.autoinstall_path).read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise CustomizationError(f"Cannot read autoinstall file: {exc}") from exc
            if not isinstance(document, dict):
                raise CustomizationError("The autoinstall file must be a YAML mapping.")
        validate_packages(config.extra_packages + config.remove_packages)
        if config.desktop and config.desktop not in DESKTOPS_BY_ID:
            raise CustomizationError(f"Unknown desktop: {config.desktop!r}")
        if config.display_manager not in ("", KEEP_DISPLAY_MANAGER, *DISPLAY_MANAGERS_BY_ID):
            raise CustomizationError(f"Unknown login manager: {config.display_manager!r}")
        if config.description and (_FREE_TEXT_FORBIDDEN.search(config.description) or '"' in config.description
                                   or "\n" in config.description):
            raise CustomizationError("The description must be a single line without double quotes.")
        if _FREE_TEXT_FORBIDDEN.search(config.welcome_message):
            raise CustomizationError("The welcome message contains control characters.")
        if config.locale:
            normalize_locale(config.locale)
        for deb in config.local_debs:
            if not deb.endswith(".deb") or not Path(deb).is_file():
                raise CustomizationError(f"Not a .deb file: {deb}")
        if config.install_calamares:
            validate_packages(config.calamares_packages)

    def apply(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Apply ``config``. ``session`` must already be mounted.

        The order follows the original tool: identity first, software next,
        the user's own commands last so they can adjust everything else.
        """
        self.validate(config)
        if config.locale:
            # casper reads locale= at boot: the live session speaks it too.
            locale = normalize_locale(config.locale)
            extra = f"{config.grub_extra_kernel_args} locale={locale}".strip()
            config = CustomizationConfig.from_dict({**config.to_dict(), "grub_extra_kernel_args": extra})
        self._apply_boot_menu(config)
        if config.distro_name:
            self._apply_branding(config.distro_name, session.rootfs)
        if config.description:
            self._apply_description(config.description, session.rootfs)
        if config.welcome_message:
            self._apply_welcome(config.welcome_message, session.rootfs)
        if config.remove_windows_files:
            self._remove_windows_files()
        if config.autoinstall_path:
            shutil.copyfile(config.autoinstall_path, self._ws.iso_dir / "autoinstall.yaml")
            self._runner.log("Installed autoinstall.yaml at the root of the ISO (read by Subiquity).")
        self._apply_packages(config, session)
        if config.wallpaper_path or config.gtk_theme or config.icon_theme:
            self._apply_appearance(config, session)
        if config.plymouth_theme:
            self._apply_plymouth(config.plymouth_theme, session)
        if config.locale:
            self._apply_locale(normalize_locale(config.locale), session)
        if config.commands.strip():
            self._run_commands(config.commands, session)

    # -- individual steps --------------------------------------------------

    def _boot_configs(self) -> list[Path]:
        candidates = ("boot/grub/grub.cfg", "boot/grub/loopback.cfg", "isolinux/txt.cfg")
        return [self._ws.iso_dir / c for c in candidates if (self._ws.iso_dir / c).is_file()]

    def _apply_boot_menu(self, config: CustomizationConfig) -> None:
        if config.grub_timeout is None and not config.grub_extra_kernel_args and not config.distro_name:
            return
        for path in self._boot_configs():
            text = original = path.read_text(encoding="utf-8")
            is_grub = path.suffix == ".cfg" and path.parent.name == "grub"
            if is_grub and config.grub_timeout is not None:
                text = set_grub_timeout(text, config.grub_timeout)
            if is_grub and config.grub_extra_kernel_args:
                text = add_kernel_args(text, config.grub_extra_kernel_args)
            if is_grub and config.distro_name:
                text = rename_menu_entries(text, config.distro_name)
            if text != original:
                path.write_text(text, encoding="utf-8")
                self._runner.log(f"Updated {path.relative_to(self._ws.iso_dir)}")

    def _apply_branding(self, name: str, rootfs: Path) -> None:
        """Rename the distribution where users see it.

        ``ID=ubuntu`` / ``DISTRIB_ID`` are left alone on purpose: apt, PPAs
        and third-party installers rely on them.
        """
        os_release = rootfs / "usr/lib/os-release"
        if os_release.is_file():
            text = os_release.read_text(encoding="utf-8")
            version = re.search(r'^VERSION="?([^"\n]*)"?$', text, re.MULTILINE)
            pretty = f"{name} {version.group(1)}" if version else name
            text = _set_key(_set_key(text, "NAME", name), "PRETTY_NAME", pretty)
            os_release.write_text(text, encoding="utf-8")
        lsb = rootfs / "etc/lsb-release"
        if lsb.is_file():
            text = lsb.read_text(encoding="utf-8")
            release = re.search(r"^DISTRIB_RELEASE=(.*)$", text, re.MULTILINE)
            described = f"{name} {release.group(1)}" if release else name
            lsb.write_text(_set_key(text, "DISTRIB_DESCRIPTION", described), encoding="utf-8")
        for issue, suffix in (("etc/issue", " \\n \\l\n\n"), ("etc/issue.net", "\n")):
            path = rootfs / issue
            if path.is_file():
                first = path.read_text(encoding="utf-8").split("\\n")[0].strip()
                path.write_text(_UBUNTU_WORD.sub(name, first, count=1) + suffix, encoding="utf-8")
        disk_info = self._ws.iso_dir / ".disk" / "info"
        if disk_info.is_file():
            info = disk_info.read_text(encoding="utf-8")
            disk_info.write_text(_UBUNTU_WORD.sub(name, info, count=1), encoding="utf-8")
        self._runner.log(f"Distribution renamed to {name!r}.")

    def _apply_description(self, description: str, rootfs: Path) -> None:
        for path, key in (("usr/lib/os-release", "PRETTY_NAME"), ("etc/lsb-release", "DISTRIB_DESCRIPTION")):
            target = rootfs / path
            if target.is_file() and not target.is_symlink():
                target.write_text(_set_key(target.read_text(encoding="utf-8"), key, description), encoding="utf-8")
        self._runner.log(f"Description set to {description!r}.")

    def _apply_welcome(self, message: str, rootfs: Path) -> None:
        """Console welcome text, as the original wrote into motd."""
        welcome = rootfs / WELCOME_FILE
        welcome.parent.mkdir(parents=True, exist_ok=True)
        welcome.write_text(message.rstrip("\n") + "\n", encoding="utf-8")
        motd_dir = rootfs / "etc/update-motd.d"
        if motd_dir.is_dir():
            script = motd_dir / "10-help-text"
            script.write_text(_WELCOME_SCRIPT, encoding="utf-8")
            script.chmod(0o755)
        else:
            (rootfs / "etc/motd").write_text(message.rstrip("\n") + "\n\n", encoding="utf-8")
        self._runner.log("Welcome message installed.")

    def _remove_windows_files(self) -> None:
        removed = []
        for name in WINDOWS_FILES:
            path = self._ws.iso_dir / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        for name in WINDOWS_DIRS:
            path = self._ws.iso_dir / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed.append(name + "/")
        self._runner.log("Windows files removed: " + (", ".join(removed) if removed else "none present"))

    def _apply_locale(self, locale: str, session: ChrootSession) -> None:
        if not session.has_command("locale-gen"):
            session.run(["apt-get", "install", "-y", "locales"])
        session.run(["locale-gen", locale])
        session.run(["update-locale", f"LANG={locale}"])
        self._runner.log(f"Default locale set to {locale}.")

    def _run_commands(self, commands: str, session: ChrootSession) -> None:
        """Run the user's script (the original AUTOCOMMANDS) inside the image."""
        script = session.rootfs / "tmp" / f"{APP_ID}-commands.sh"
        script.write_text("set -e\n" + commands.replace("\r\n", "\n") + "\n", encoding="utf-8")
        try:
            session.run(["/bin/bash", f"/tmp/{script.name}"])
        finally:
            script.unlink(missing_ok=True)

    @staticmethod
    def chosen_display_manager(config: CustomizationConfig) -> DisplayManager | None:
        """The login manager to install: explicit choice, else the desktop's default."""
        dm_id = config.display_manager
        if dm_id == KEEP_DISPLAY_MANAGER:
            return None
        if not dm_id and config.desktop:
            dm_id = DESKTOPS_BY_ID[config.desktop].display_manager
        return DISPLAY_MANAGERS_BY_ID.get(dm_id) if dm_id else None

    def _apply_packages(self, config: CustomizationConfig, session: ChrootSession) -> None:
        desktop = DESKTOPS_BY_ID[config.desktop].packages if config.desktop else ()
        manager = self.chosen_display_manager(config)
        if manager is not None:
            # Answer the "default display manager" question before the
            # packages ask it, so the postinst scripts agree with us.
            session.run(["/bin/sh", "-c", 'echo "$1 shared/default-x-display-manager select $1" | '
                         "debconf-set-selections", "sh", manager.packages[0]])  # fmt: skip
        install = [*desktop, *(manager.packages if manager else ()), *config.extra_packages]
        if config.install_calamares:
            install += config.calamares_packages
        if not install and not config.remove_packages and not config.local_debs:
            return
        apt = ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold"]
        if install or config.local_debs:
            session.run(["apt-get", "update"])
        if install:
            session.run([*apt, "install", "--", *install])
        if config.local_debs:
            staging = session.rootfs / "tmp" / f"{APP_ID}-debs"
            staging.mkdir(parents=True, exist_ok=True)
            try:
                inside = []
                for deb in config.local_debs:
                    shutil.copyfile(deb, staging / Path(deb).name)
                    inside.append(f"/tmp/{staging.name}/{Path(deb).name}")
                # apt resolves the dependencies of local files ("apt install ./x.deb").
                session.run([*apt, "install", "--", *inside])
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        if manager is not None:
            self._set_display_manager(manager, session)
        if config.remove_packages:
            session.run([*apt, "purge", "--", *config.remove_packages])
            session.run([*apt, "autoremove", "--purge"])

    def _set_display_manager(self, manager: DisplayManager, session: ChrootSession) -> None:
        """Make ``manager`` the login screen of the installed and live system."""
        rootfs = session.rootfs
        unit = next(
            (rootfs / base / manager.service for base in ("usr/lib/systemd/system", "lib/systemd/system")
             if (rootfs / base / manager.service).exists()),
            None,
        )  # fmt: skip
        if unit is None:
            raise CustomizationError(f"{manager.service} was not installed; cannot enable {manager.name}.")
        (rootfs / "etc/X11").mkdir(parents=True, exist_ok=True)
        (rootfs / "etc/X11/default-display-manager").write_text(manager.binary + "\n", encoding="utf-8")
        link = rootfs / "etc/systemd/system/display-manager.service"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to("/" + unit.relative_to(rootfs).as_posix())
        if manager.id.startswith("lightdm-"):
            greeter = manager.packages[1]
            conf = rootfs / "etc/lightdm/lightdm.conf.d" / f"50-{APP_ID}.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            conf.write_text(f"[Seat:*]\ngreeter-session={greeter}\n", encoding="utf-8")
        elif manager.id == "greetd":
            config_file = rootfs / "etc/greetd/config.toml"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(GREETD_CONFIG, encoding="utf-8")
        note = "" if manager.live_autologin else " (the live session shows its login prompt: no autologin)"
        self._runner.log(f"Login manager set to {manager.name}{note}.")

    def _apply_appearance(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Default wallpaper and themes through a GSettings override.

        Covers GNOME based desktops (Ubuntu, Budgie, Cinnamon read the same
        ``org.gnome.desktop`` keys for themes). KDE Plasma and Xfce keep
        their defaults elsewhere and are not touched.
        """
        rootfs = session.rootfs
        schemas = rootfs / "usr/share/glib-2.0/schemas"
        if not schemas.is_dir():
            self._runner.log("WARNING: the image has no GSettings schemas; appearance settings skipped.")
            return
        background: list[str] = []
        if config.wallpaper_path:
            source = Path(config.wallpaper_path)
            target = rootfs / "usr/share/backgrounds" / f"{APP_ID}-wallpaper{source.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o644)
            uri = f"file:///{target.relative_to(rootfs).as_posix()}"
            background = [f"picture-uri='{uri}'", f"picture-uri-dark='{uri}'"]
        interface = [f"gtk-theme='{config.gtk_theme}'"] if config.gtk_theme else []
        interface += [f"icon-theme='{config.icon_theme}'"] if config.icon_theme else []
        sections: list[str] = []
        # The ":ubuntu" variants outrank Ubuntu's own session defaults.
        for suffix in ("", ":ubuntu"):
            if background:
                sections.append(f"[org.gnome.desktop.background{suffix}]\n" + "\n".join(background))
                sections.append(f"[org.gnome.desktop.screensaver{suffix}]\n{background[0]}")
            if interface:
                sections.append(f"[org.gnome.desktop.interface{suffix}]\n" + "\n".join(interface))
        (rootfs / OVERRIDE_FILE).write_text("\n\n".join(sections) + "\n", encoding="utf-8")
        if session.has_command("glib-compile-schemas"):
            session.run(["glib-compile-schemas", "/usr/share/glib-2.0/schemas/"])
        else:
            self._runner.log("WARNING: glib-compile-schemas not found in the image; override not compiled.")

    def _apply_plymouth(self, theme: str, session: ChrootSession) -> None:
        rootfs = session.rootfs
        theme_file = f"/usr/share/plymouth/themes/{theme}/{theme}.plymouth"
        if not (rootfs / theme_file.lstrip("/")).is_file():
            available = sorted(p.name for p in (rootfs / "usr/share/plymouth/themes").glob("*") if p.is_dir())
            raise CustomizationError(
                f"Plymouth theme {theme!r} is not installed in the image. "
                f"Available: {', '.join(available) or 'none'}. Install its package first."
            )
        session.run(["update-alternatives", "--install", "/usr/share/plymouth/themes/default.plymouth",
                     "default.plymouth", theme_file, "200"])  # fmt: skip
        session.run(["update-alternatives", "--set", "default.plymouth", theme_file])
        initrds = sorted((rootfs / "boot").glob("initrd.img-*"))
        if not initrds:
            self._runner.log(
                "Plymouth theme set. This layer has no kernel/initrd, so the splash changes on the "
                "installed system only; the live boot keeps the original splash."
            )
            return
        session.run(["update-initramfs", "-u", "-k", "all"])
        # Only an initrd built where casper is installed can boot the live
        # medium; anything else would leave the ISO unbootable.
        live_initrd = next((p for p in (self._ws.iso_dir / "casper").glob("initrd*") if p.is_file()), None)
        if live_initrd is not None and (rootfs / "usr/share/initramfs-tools/scripts/casper").exists():
            newest = max((rootfs / "boot").glob("initrd.img-*"), key=lambda p: p.stat().st_mtime)
            shutil.copyfile(newest, live_initrd)
            self._runner.log(f"Live boot splash updated ({live_initrd.name} regenerated from {newest.name}).")
        else:
            self._runner.log("Plymouth theme applies to the installed system; live initrd left unchanged.")
