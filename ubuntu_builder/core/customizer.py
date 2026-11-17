"""Apply a :class:`CustomizationConfig` to the extracted image.

File level changes (boot menu, wallpaper, autoinstall …) are made directly;
everything involving packages runs through apt inside a
:class:`~ubuntu_builder.core.chroot.ChrootSession`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from ubuntu_builder import APP_ID
from ubuntu_builder.core.chroot import ChrootSession
from ubuntu_builder.core.errors import BuilderError
from ubuntu_builder.core.runner import CommandRunner
from ubuntu_builder.models.customization import CustomizationConfig
from ubuntu_builder.models.workspace import Workspace

_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+(:[a-z0-9]+)?$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]*$")
_UBUNTU_WORD = re.compile(r"\b\w*[Uu]buntu\b(?:[ -](?:Server|Studio|MATE|Budgie|Cinnamon|Kylin|Unity)\b)?")
_MENU_TITLE = re.compile(r"^(\s*(?:menuentry|submenu)\s+)(['\"])(.*?)\2", re.MULTILINE)
_LINUX_LINE = re.compile(r"^(\s*linux(?:efi)?\s+\S+)(.*)$", re.MULTILINE)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
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
            raise CustomizationError("The distribution name may only contain letters, digits, spaces and . _ + -")
        for name in (config.plymouth_theme, config.gtk_theme, config.icon_theme):
            if name and not _SAFE_NAME.match(name):
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
        if config.install_calamares:
            validate_packages(config.calamares_packages)

    def apply(self, config: CustomizationConfig, session: ChrootSession) -> None:
        """Apply ``config``. ``session`` must already be mounted."""
        self.validate(config)
        self._apply_boot_menu(config)
        if config.distro_name:
            self._apply_branding(config.distro_name, session.rootfs)
        if config.autoinstall_path:
            shutil.copyfile(config.autoinstall_path, self._ws.iso_dir / "autoinstall.yaml")
            self._runner.log("Installed autoinstall.yaml at the root of the ISO (read by Subiquity).")
        self._apply_packages(config, session)
        if config.wallpaper_path or config.gtk_theme or config.icon_theme:
            self._apply_appearance(config, session)
        if config.plymouth_theme:
            self._apply_plymouth(config.plymouth_theme, session)

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

    def _apply_packages(self, config: CustomizationConfig, session: ChrootSession) -> None:
        install = [*config.desktop.packages, *config.extra_packages]
        if config.install_calamares:
            install += config.calamares_packages
        if not install and not config.remove_packages:
            return
        apt = ["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold"]
        if install:
            session.run(["apt-get", "update"])
            session.run([*apt, "install", "--", *install])
        if config.remove_packages:
            session.run([*apt, "purge", "--", *config.remove_packages])
            session.run([*apt, "autoremove", "--purge"])

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
