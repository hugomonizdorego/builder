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
from os_builder.core.autoinstall import AutoinstallError
from os_builder.core.autoinstall import parse as parse_autoinstall
from os_builder.core.branding import (
    APPEARANCE_OVERRIDE,
    LOGIN_OVERRIDE,
    SPLASH_THEME,
    TARGET_GRUB_DROPIN,
    appearance_override,
    apply_grub_appearance,
    grub_appearance_block,
    is_grub_color,
    is_grub_color_pair,
    login_override,
    plymouth_script,
    plymouth_theme_file,
    schema_ids,
    set_ini_value,
    target_grub_dropin,
)
from os_builder.core.chroot import ChrootSession
from os_builder.core.errors import BuilderError
from os_builder.core.images import image_kind, parse_color, solid_png
from os_builder.core.initrd import append_overlay, write_newc
from os_builder.core.runner import CommandRunner
from os_builder.core.themes import install_theme_archive, is_theme_archive
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


def _check_image(path: str, what: str, kinds: set[str]) -> None:
    if not path:
        return
    kind = image_kind(Path(path))
    if kind not in kinds:
        allowed = ", ".join(sorted(k.upper() for k in kinds))
        raise CustomizationError(f"{what} must be an image file ({allowed}): {path}")


def normalize_locale(locale: str) -> str:
    """``en_US`` → ``en_US.UTF-8``; raises for anything that is not a locale."""
    locale = locale.strip()
    if not _LOCALE.match(locale):
        raise CustomizationError(f"Not a valid locale: {locale!r} (expected e.g. en_US.UTF-8)")
    if "_" in locale and not locale.endswith(".UTF-8"):
        locale += ".UTF-8"
    return locale


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
        _check_image(config.wallpaper_path, "Wallpaper", {"png", "jpeg", "svg", "webp"})
        _check_image(config.login_background, "Login screen background", {"png", "jpeg"})
        _check_image(config.login_logo, "Login screen logo", {"png", "svg"})
        _check_image(config.splash_logo, "Boot splash logo", {"png"})
        _check_image(config.grub_background, "Boot menu background", {"png", "jpeg"})
        if config.splash_color:
            try:
                parse_color(config.splash_color)
            except ValueError as exc:
                raise CustomizationError(str(exc)) from exc
        if config.splash_logo and config.plymouth_theme and config.plymouth_theme != SPLASH_THEME:
            raise CustomizationError("Choose either a Plymouth theme or a custom splash logo, not both.")
        if config.grub_text_color and not is_grub_color(config.grub_text_color):
            raise CustomizationError(f"Unknown GRUB colour: {config.grub_text_color!r}")
        if config.grub_highlight_color and not is_grub_color_pair(config.grub_highlight_color):
            raise CustomizationError("The highlight colour must look like 'black/light-gray'.")
        if config.grub_cfg_text and "menuentry" not in config.grub_cfg_text:
            raise CustomizationError("The boot menu text has no menuentry; the ISO would not boot.")
        if config.target_grub_timeout is not None and not 0 <= config.target_grub_timeout <= 600:
            raise CustomizationError("The installed system's menu timeout must be between 0 and 600 seconds.")
        if re.search(r"[\n\r\"'`$\\]", config.target_grub_cmdline):
            raise CustomizationError("Kernel arguments must not contain quotes, backslashes or '$'.")
        if config.target_grub_theme not in ("", "starfield", "background"):
            raise CustomizationError(f"Unknown boot menu theme: {config.target_grub_theme!r}")
        if config.target_grub_theme == "background" and not config.grub_background:
            raise CustomizationError("Choose a boot menu background image to use it on the installed system.")
        if re.search(r"[\x00-\x1f\"'\\]", config.login_message):
            raise CustomizationError("The login message must be one line without quotes or backslashes.")
        if config.cursor_theme and not _THEME_NAME.match(config.cursor_theme):
            raise CustomizationError(f"Not a valid theme name: {config.cursor_theme!r}")
        for archive in config.theme_archives:
            if not is_theme_archive(archive) or not Path(archive).is_file():
                raise CustomizationError(f"Not a theme archive (.tar.gz, .tar.xz, .zip): {archive}")
        if config.autoinstall_yaml:
            try:
                parse_autoinstall(config.autoinstall_yaml)
            except AutoinstallError as exc:
                raise CustomizationError(f"Installer settings: {exc}") from exc
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
        extra = config.grub_extra_kernel_args
        if config.locale:
            # casper reads locale= at boot: the live session speaks it too.
            extra = f"{extra} locale={normalize_locale(config.locale)}"
        if config.autoinstall_unattended:
            extra = f"{extra} autoinstall"  # Subiquity installs without asking for confirmation
        if config.splash_logo or config.plymouth_theme:
            extra = f"{extra} quiet splash"  # show the splash on the live ISO
        if extra.strip() != config.grub_extra_kernel_args:
            config = CustomizationConfig.from_dict({**config.to_dict(), "grub_extra_kernel_args": extra.strip()})
        self._apply_boot_menu(config)
        if config.distro_name:
            self._apply_branding(config.distro_name, session.rootfs)
        if config.description:
            self._apply_description(config.description, session.rootfs)
        if config.welcome_message:
            self._apply_welcome(config.welcome_message, session.rootfs)
        if config.remove_windows_files:
            self._remove_windows_files()
        if config.autoinstall_yaml:
            (self._ws.iso_dir / "autoinstall.yaml").write_text(config.autoinstall_yaml, encoding="utf-8")
            self._runner.log("Installer settings written to /autoinstall.yaml on the ISO (read by Subiquity).")
        elif config.autoinstall_path:
            shutil.copyfile(config.autoinstall_path, self._ws.iso_dir / "autoinstall.yaml")
            self._runner.log("Installed autoinstall.yaml at the root of the ISO (read by Subiquity).")
        self._apply_packages(config, session)
        for archive in config.theme_archives:
            names = install_theme_archive(Path(archive), session.rootfs)
            self._runner.log(f"Installed from {Path(archive).name}: {', '.join(names)}")
        if config.wallpaper_path or config.gtk_theme or config.icon_theme or config.cursor_theme:
            self._apply_look(config, session)
        if config.login_background or config.login_logo or config.login_message:
            self._apply_login_screen(config, session)
        if config.splash_logo:
            self._create_splash_theme(config, session)
            self._apply_plymouth(SPLASH_THEME, session)
        elif config.plymouth_theme:
            self._apply_plymouth(config.plymouth_theme, session)
        if config.target_grub_timeout is not None or config.target_grub_cmdline or config.target_grub_theme:
            self._apply_target_grub(config, session)
        if config.locale:
            self._apply_locale(normalize_locale(config.locale), session)
        if config.commands.strip():
            self._run_commands(config.commands, session)

    # -- individual steps --------------------------------------------------

    def _boot_configs(self) -> list[Path]:
        candidates = ("boot/grub/grub.cfg", "boot/grub/loopback.cfg", "isolinux/txt.cfg")
        return [self._ws.iso_dir / c for c in candidates if (self._ws.iso_dir / c).is_file()]

    def _apply_boot_menu(self, config: CustomizationConfig) -> None:
        grub_cfg = self._ws.iso_dir / "boot/grub/grub.cfg"
        if config.grub_cfg_text and grub_cfg.is_file():
            # The user's own boot menu replaces the generated one; the other
            # boot menu settings still apply on top of it.
            grub_cfg.write_text(config.grub_cfg_text.replace("\r\n", "\n"), encoding="utf-8")
            self._runner.log("boot/grub/grub.cfg replaced with the edited boot menu.")
        background = ""
        if config.grub_background:
            source = Path(config.grub_background)
            suffix = ".jpg" if image_kind(source) == "jpeg" else ".png"
            target = self._ws.iso_dir / "boot/grub" / f"{APP_ID}-background{suffix}"
            shutil.copyfile(source, target)
            background = "/" + target.relative_to(self._ws.iso_dir).as_posix()
        appearance = ""
        if background or config.grub_text_color or config.grub_highlight_color:
            appearance = grub_appearance_block(background, config.grub_text_color, config.grub_highlight_color)
        if (config.grub_timeout is None and not config.grub_extra_kernel_args and not config.distro_name
                and not appearance):
            return
        for path in self._boot_configs():
            text = original = path.read_text(encoding="utf-8")
            is_grub = path.suffix == ".cfg" and path.parent.name == "grub"
            if is_grub and appearance and path.name == "grub.cfg":
                text = apply_grub_appearance(text, appearance)
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

    def _copy_into_image(self, source: str, rootfs: Path, directory: str, stem: str) -> str:
        """Copy a host file into the image; returns its absolute path inside."""
        path = Path(source)
        suffix = {"jpeg": ".jpg"}.get(image_kind(path) or "", path.suffix.lower())
        target = rootfs / directory / f"{stem}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o644)
        return "/" + target.relative_to(rootfs).as_posix()

    def _require_theme(self, rootfs: Path, name: str, kind: str) -> None:
        """Fail with a helpful list when a theme is not installed in the image."""
        base = rootfs / "usr/share" / ("themes" if kind == "GTK" else "icons")
        probe = base / name / ("cursors" if kind == "cursor" else "")
        if probe.is_dir():
            return
        if kind == "cursor":
            available = sorted(p.parent.name for p in base.glob("*/cursors"))
        elif kind == "icon":
            available = sorted(p.parent.name for p in base.glob("*/index.theme") if not (p.parent / "cursors").is_dir())
        else:
            available = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        raise CustomizationError(
            f"The {kind} theme {name!r} is not installed in the image. Available: {', '.join(available) or 'none'}. "
            "Install its package (Wizard → Look & feel) or a theme archive first."
        )

    def _apply_look(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Wallpaper, GTK, icon and cursor theme as defaults for new users.

        GNOME, Budgie, Unity, MATE and Cinnamon read them through GSettings;
        KDE Plasma gets the icon theme through kdeglobals; every desktop gets
        the cursor theme through the x-cursor-theme alternative.
        """
        rootfs = session.rootfs
        for name, kind in ((config.gtk_theme, "GTK"), (config.icon_theme, "icon"), (config.cursor_theme, "cursor")):
            if name:
                self._require_theme(rootfs, name, kind)
        wallpaper = ""
        if config.wallpaper_path:
            wallpaper = self._copy_into_image(
                config.wallpaper_path, rootfs, "usr/share/backgrounds", f"{APP_ID}-wallpaper"
            )
        schema_dir = rootfs / "usr/share/glib-2.0/schemas"
        override = appearance_override(
            schema_ids(schema_dir) if schema_dir.is_dir() else set(),
            wallpaper=wallpaper, gtk=config.gtk_theme, icons=config.icon_theme, cursor=config.cursor_theme,
        )  # fmt: skip
        if override:
            (rootfs / APPEARANCE_OVERRIDE).write_text(override, encoding="utf-8")
            self._compile_schemas(session)
        elif wallpaper or config.gtk_theme:
            self._runner.log("No GNOME, MATE or Cinnamon settings schemas in the image; wallpaper/theme not set.")
        if config.icon_theme and (rootfs / "usr/bin/plasmashell").exists():
            kdeglobals = rootfs / "etc/xdg/kdeglobals"
            text = kdeglobals.read_text(encoding="utf-8") if kdeglobals.is_file() else ""
            kdeglobals.parent.mkdir(parents=True, exist_ok=True)
            kdeglobals.write_text(set_ini_value(text, "Icons", "Theme", config.icon_theme), encoding="utf-8")
        if config.cursor_theme:
            cursor_file = f"/usr/share/icons/{config.cursor_theme}/cursor.theme"
            if not (rootfs / cursor_file.lstrip("/")).exists():
                (rootfs / cursor_file.lstrip("/")).write_text(
                    f"[Icon Theme]\nName={config.cursor_theme}\nInherits={config.cursor_theme}\n", encoding="utf-8"
                )
            session.run(["update-alternatives", "--install", "/usr/share/icons/default/index.theme",
                         "x-cursor-theme", cursor_file, "100"])  # fmt: skip
            session.run(["update-alternatives", "--set", "x-cursor-theme", cursor_file])
        self._runner.log("Look & feel defaults set.")

    def _compile_schemas(self, session: ChrootSession) -> None:
        if session.has_command("glib-compile-schemas"):
            session.run(["glib-compile-schemas", "/usr/share/glib-2.0/schemas/"])
        else:
            self._runner.log("WARNING: glib-compile-schemas not found in the image; settings not compiled.")

    def _apply_login_screen(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Background, logo and message of GDM, Slick Greeter, GTK Greeter, SDDM and tuigreet."""
        rootfs = session.rootfs
        background = logo = ""
        if config.login_background:
            background = self._copy_into_image(config.login_background, rootfs, "usr/share/backgrounds",
                                               f"{APP_ID}-login")  # fmt: skip
        if config.login_logo:
            logo = self._copy_into_image(config.login_logo, rootfs, "usr/share/pixmaps", f"{APP_ID}-login-logo")
        done: list[str] = []
        schema_dir = rootfs / "usr/share/glib-2.0/schemas"
        override = login_override(
            schema_ids(schema_dir) if schema_dir.is_dir() else set(), background=background, logo=logo,
            message=config.login_message, gtk=config.gtk_theme, icons=config.icon_theme, cursor=config.cursor_theme,
        )  # fmt: skip
        if override:
            (rootfs / LOGIN_OVERRIDE).write_text(override, encoding="utf-8")
            self._compile_schemas(session)
            if "login-screen]" in override:
                done.append("GDM")
            if "[x.dm.slick-greeter]" in override:
                done.append("Slick Greeter")
        gtk_greeter = rootfs / "etc/lightdm/lightdm-gtk-greeter.conf"
        if gtk_greeter.is_file():
            text = gtk_greeter.read_text(encoding="utf-8")
            for key, value in (("background", background), ("default-user-image", logo),
                               ("theme-name", config.gtk_theme), ("icon-theme-name", config.icon_theme),
                               ("cursor-theme-name", config.cursor_theme)):  # fmt: skip
                if value:
                    text = set_ini_value(text, "greeter", key, value)
            gtk_greeter.write_text(text, encoding="utf-8")
            done.append("LightDM GTK Greeter")
        breeze = rootfs / "usr/share/sddm/themes/breeze"
        if breeze.is_dir() and background:
            user_conf = breeze / "theme.conf.user"
            text = user_conf.read_text(encoding="utf-8") if user_conf.is_file() else ""
            user_conf.write_text(set_ini_value(text, "General", "background", background), encoding="utf-8")
            done.append("SDDM (Breeze)")
        if config.cursor_theme and (rootfs / "usr/bin/sddm").exists():
            conf = rootfs / "etc/sddm.conf.d" / f"90-{APP_ID}.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
            conf.write_text(set_ini_value(text, "Theme", "CursorTheme", config.cursor_theme), encoding="utf-8")
        greetd = rootfs / "etc/greetd/config.toml"
        if greetd.is_file() and config.login_message and "tuigreet" in greetd.read_text(encoding="utf-8"):
            text = re.sub(r" --greeting '[^']*'", "", greetd.read_text(encoding="utf-8"))
            text = re.sub(r'(command = "tuigreet)', rf"\1 --greeting '{config.login_message}'", text)
            greetd.write_text(text, encoding="utf-8")
            done.append("greetd (tuigreet)")
        if not done:
            self._runner.log("WARNING: no supported login manager found in the image; login screen not changed.")
        else:
            self._runner.log("Login screen updated: " + ", ".join(done) + ".")

    def _live_splash_overlay(self, session: ChrootSession) -> None:
        """Show the chosen splash in the live session too (see core/initrd.py)."""
        hook = session.rootfs / "usr/share/initramfs-tools/hooks/plymouth"
        live_initrds = sorted(p for p in (self._ws.iso_dir / "casper").glob("initrd*") if p.is_file())
        if not hook.exists() or not live_initrds:
            self._runner.log("The live boot keeps its splash (no Plymouth initramfs hook or live initrd).")
            return
        staging = session.rootfs / "tmp" / f"{APP_ID}-initrd"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        # Same merged-/usr layout as the initrd built by mkinitramfs.
        for directory in ("usr/bin", "usr/sbin", "usr/lib", "usr/lib64", "etc", "conf/conf.d", "scripts"):
            (staging / directory).mkdir(parents=True, exist_ok=True)
        for link in ("bin", "sbin", "lib", "lib64"):
            (staging / link).symlink_to(f"usr/{link}")
        try:
            session.run(["/bin/sh", "-c",
                         f"DESTDIR=/tmp/{staging.name} verbose=n; export DESTDIR verbose; "
                         ". /usr/share/initramfs-tools/hook-functions; "
                         "/usr/share/initramfs-tools/hooks/plymouth"])  # fmt: skip
            overlay = self._ws.boot_dir / "plymouth-overlay.cpio"
            self._ws.boot_dir.mkdir(parents=True, exist_ok=True)
            count = write_newc(staging, overlay)
            for initrd in live_initrds:
                append_overlay(initrd, overlay, self._ws.boot_dir)
            self._runner.log(
                f"Live boot splash added to {', '.join(p.name for p in live_initrds)} ({count} files)."
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _create_splash_theme(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Generate the "os-builder" Plymouth theme (script plugin) from a logo."""
        rootfs = session.rootfs
        if not list(rootfs.glob("usr/lib/*/plymouth/script.so")) or not session.has_command("plymouth"):
            session.run(["apt-get", "install", "-y", "plymouth"])
        if not list(rootfs.glob("usr/lib/*/plymouth/label*.so")):
            # Image.Text() (password prompt, messages) needs the label plugin.
            session.run(["apt-get", "install", "-y", "plymouth-label"])
        theme_dir = rootfs / "usr/share/plymouth/themes" / SPLASH_THEME
        theme_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.splash_logo, theme_dir / "logo.png")
        rgb = parse_color(config.splash_color) if config.splash_color else (0, 0, 0)
        (theme_dir / "progress_bg.png").write_bytes(solid_png(4, 4, (255, 255, 255, 60)))
        (theme_dir / "progress_fg.png").write_bytes(solid_png(4, 4, (255, 255, 255, 230)))
        (theme_dir / f"{SPLASH_THEME}.plymouth").write_text(plymouth_theme_file(), encoding="utf-8")
        (theme_dir / f"{SPLASH_THEME}.script").write_text(plymouth_script(rgb), encoding="utf-8")
        for path in theme_dir.iterdir():
            path.chmod(0o644)
        self._runner.log(f"Boot splash theme '{SPLASH_THEME}' created.")

    def _apply_target_grub(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Boot menu of the installed system (read by update-grub at install time)."""
        rootfs = session.rootfs
        theme = background = ""
        if config.target_grub_theme == "starfield":
            if not (rootfs / "usr/share/grub/themes/starfield/theme.txt").exists():
                session.run(["apt-get", "install", "-y", "grub-theme-starfield"])
            theme = "/usr/share/grub/themes/starfield/theme.txt"
        elif config.target_grub_theme == "background":
            background = self._copy_into_image(config.grub_background, rootfs, "usr/share/backgrounds",
                                               f"{APP_ID}-grub")  # fmt: skip
        dropin = rootfs / TARGET_GRUB_DROPIN
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text(
            target_grub_dropin(timeout=config.target_grub_timeout, cmdline=config.target_grub_cmdline,
                               background=background, theme=theme),
            encoding="utf-8",
        )  # fmt: skip
        self._runner.log(f"Installed system boot menu configured (/{TARGET_GRUB_DROPIN}).")

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
            # The kernel lives in another layer: extend the live initrd instead.
            self._runner.log("Plymouth theme set for the installed system.")
            self._live_splash_overlay(session)
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
            self._live_splash_overlay(session)
