"""Branding generators, theme archives, initrd overlay and the installer editor."""

import io
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml
from os_builder.core import autoinstall, branding, customizer, images, initrd, themes
from os_builder.core.runner import CommandRunner
from os_builder.models import CustomizationConfig, Workspace
from os_builder.models.installer import InstallerSettings

GNOME = {"org.gnome.desktop.background", "org.gnome.desktop.screensaver", "org.gnome.desktop.interface"}


# -- GSettings overrides --------------------------------------------------------------


def test_appearance_override_only_for_present_schemas():
    text = branding.appearance_override(GNOME, wallpaper="/usr/share/backgrounds/w.png", gtk="Yaru-dark", cursor="Yaru")
    assert "[org.gnome.desktop.background]\npicture-uri='file:///usr/share/backgrounds/w.png'" in text
    assert "[org.gnome.desktop.interface:ubuntu]" in text and "cursor-theme='Yaru'" in text
    assert "org.mate" not in text and "cinnamon" not in text and "icon-theme" not in text
    mate = branding.appearance_override({"org.mate.background"}, wallpaper="/w.png")
    assert mate == "[org.mate.background]\npicture-filename='/w.png'\n"
    assert branding.appearance_override(set(), wallpaper="/w.png") == ""


def test_gvariant_quoting():
    assert branding.gvariant_string("it's \\ fine") == "'it\\'s \\\\ fine'"


def test_login_override():
    schemas = {"com.ubuntu.login-screen", "org.gnome.login-screen", "x.dm.slick-greeter"}
    text = branding.login_override(schemas, background="/bg.jpg", logo="/logo.png", message="Hi", icons="Papirus")
    assert "background-picture-uri='file:///bg.jpg'" in text
    assert "banner-message-enable=true" in text and "banner-message-text='Hi'" in text
    assert "[x.dm.slick-greeter]\nbackground='/bg.jpg'\ndraw-user-backgrounds=false\nlogo='/logo.png'" in text
    assert "icon-theme-name='Papirus'" in text


def test_set_ini_value():
    original = "[greeter]\n#background=\n#theme-name=\n\n[other]\nx=1\n"
    text = branding.set_ini_value(original, "greeter", "background", "/bg.png")
    text = branding.set_ini_value(text, "greeter", "cursor-theme-name", "Yaru")
    assert text == "[greeter]\nbackground=/bg.png\n#theme-name=\ncursor-theme-name=Yaru\n\n[other]\nx=1\n"
    assert branding.set_ini_value("", "General", "background", "/a") == "[General]\nbackground=/a\n"


# -- GRUB ----------------------------------------------------------------------------------

CFG = "set timeout=30\n\nloadfont unicode\n\nmenuentry \"Try or Install Ubuntu\" {\n\tlinux /casper/vmlinuz ---\n}\n"


def test_grub_appearance_is_inserted_once():
    block = branding.grub_appearance_block("/boot/grub/os-builder-background.jpg", "white", "black/light-gray")
    assert "insmod jpeg" in block and "background_image /boot/grub/os-builder-background.jpg" in block
    once = branding.apply_grub_appearance(CFG, block)
    twice = branding.apply_grub_appearance(once, block)
    assert once == twice and once.count("### BEGIN") == 1
    assert once.index("background_image") < once.index("menuentry")


def test_grub_colors_and_dropin():
    assert branding.is_grub_color("light-gray") and not branding.is_grub_color("pink")
    assert branding.is_grub_color_pair("black/light-gray") and not branding.is_grub_color_pair("black")
    dropin = branding.target_grub_dropin(timeout=5, cmdline="quiet splash", background="/b.png", theme="")
    assert "GRUB_TIMEOUT_STYLE=menu\nGRUB_TIMEOUT=5" in dropin and 'GRUB_BACKGROUND="/b.png"' in dropin
    themed = branding.target_grub_dropin(timeout=None, cmdline="", background="/b.png", theme="/t/theme.txt")
    assert 'GRUB_THEME="/t/theme.txt"' in themed and "GRUB_BACKGROUND" not in themed


# -- Plymouth --------------------------------------------------------------------------------


def test_plymouth_theme_files():
    assert "ModuleName=script" in branding.plymouth_theme_file()
    script = branding.plymouth_script((26, 35, 126))
    assert "Window.SetBackgroundTopColor(0.102, 0.137, 0.494);" in script
    for hook in ("SetBootProgressFunction", "SetDisplayPasswordFunction", "SetDisplayNormalFunction",
                 "SetMessageFunction"):  # fmt: skip
        assert f"Plymouth.{hook}(" in script
    assert script.count("{") == script.count("}")


def test_images(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(images.solid_png(3, 2, (1, 2, 3, 4)))
    assert images.image_kind(png) == "png" and images.png_size(png) == (3, 2)
    jpeg = tmp_path / "a.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 20)
    assert images.image_kind(jpeg) == "jpeg"
    assert images.parse_color("#1A237e") == (26, 35, 126)
    with pytest.raises(ValueError):
        images.parse_color("blue")


# -- initrd overlay ----------------------------------------------------------------------------


def test_newc_archive_and_overlay(tmp_path):
    root = tmp_path / "root"
    (root / "usr/share/plymouth/themes/x").mkdir(parents=True)
    (root / "usr/share/plymouth/themes/x/x.plymouth").write_text("[Plymouth Theme]\n")
    (root / "bin").symlink_to("usr/bin")
    archive = tmp_path / "overlay.cpio"
    assert initrd.write_newc(root, archive) == 7  # 5 directories, 1 file, 1 symlink
    data = archive.read_bytes()
    assert data.startswith(b"070701") and b"TRAILER!!!" in data and len(data) % 512 == 0
    if shutil.which("cpio"):
        listing = subprocess.run(["cpio", "-it"], input=data, capture_output=True, check=True).stdout.decode()
        assert "usr/share/plymouth/themes/x/x.plymouth" in listing.split() and "bin" in listing.split()
    live = tmp_path / "initrd"
    live.write_bytes(b"A" * 1000)
    initrd.append_overlay(live, archive, tmp_path / "keep")
    initrd.append_overlay(live, archive, tmp_path / "keep")  # applying twice does not stack
    assert live.read_bytes() == b"A" * 1000 + b"\0" * 24 + data
    assert (tmp_path / "keep/initrd.orig").read_bytes() == b"A" * 1000


# -- theme archives ------------------------------------------------------------------------------


def _tar(path: Path, files: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return path


def test_theme_archive_installs_gtk_icon_and_cursor_themes(tmp_path):
    archive = _tar(tmp_path / "pack.tar.gz", {
        "pack/My-GTK/gtk-3.0/gtk.css": b"*{}",
        "pack/My-Icons/index.theme": b"[Icon Theme]\nName=My-Icons\n",
        "My-Cursor/cursors/left_ptr": b"x",
        "README.md": b"hello",
    })  # fmt: skip
    rootfs = tmp_path / "root"
    installed = themes.install_theme_archive(archive, rootfs)
    assert sorted(installed) == ["icons/My-Cursor", "icons/My-Icons", "themes/My-GTK"]
    assert (rootfs / "usr/share/themes/My-GTK/gtk-3.0/gtk.css").read_bytes() == b"*{}"


def test_theme_archive_rejects_traversal_and_junk(tmp_path):
    evil = _tar(tmp_path / "evil.tar.gz", {"Evil/gtk-3.0/../../../../etc/cron.d/x": b"boom"})
    with pytest.raises(themes.ThemeArchiveError):
        themes.install_theme_archive(evil, tmp_path / "root")
    assert not (tmp_path / "etc").exists()
    zipped = tmp_path / "evil.zip"
    with zipfile.ZipFile(zipped, "w") as bundle:
        bundle.writestr("../outside.txt", "x")
    with pytest.raises(themes.ThemeArchiveError):
        themes.install_theme_archive(zipped, tmp_path / "root")
    junk = _tar(tmp_path / "junk.tar.gz", {"notes/readme.txt": b"x"})
    with pytest.raises(themes.ThemeArchiveError):
        themes.install_theme_archive(junk, tmp_path / "root")


# -- installer (Subiquity autoinstall) ----------------------------------------------------------


def test_installer_modes():
    guided = autoinstall.build_document(InstallerSettings())
    assert guided["interactive-sections"] == ["*"] and "identity" not in guided
    partial = autoinstall.build_document(InstallerSettings(
        mode="partial", interactive=["identity"], storage_encrypted=True))  # fmt: skip
    assert partial["interactive-sections"] == ["identity", "storage"]  # the passphrase is asked
    with pytest.raises(autoinstall.AutoinstallError):  # fully automatic needs a user
        autoinstall.build_document(InstallerSettings(mode="automatic"))
    automatic = autoinstall.build_document(InstallerSettings(
        mode="automatic", hostname="debion", username="debion", password_hash="$6$x$y", realname="D"))  # fmt: skip
    assert "interactive-sections" not in automatic and automatic["identity"]["username"] == "debion"


@pytest.mark.parametrize("changes", [
    {"mode": "partial", "interactive": ["bogus"]},
    {"hostname": "Bad_Host", "username": "debion", "password_hash": "$6$x$y"},
    {"hostname": "debion", "username": "Root User", "password_hash": "$6$x$y"},
    {"hostname": "debion"},
    {"storage_layout": "direct", "storage_encrypted": True},
    {"packages": ["ok", "rm -rf"]},
    {"snaps": ["Bad Snap"]},
    {"extra_yaml": "- not a mapping"},
])  # fmt: skip
def test_installer_rejects_bad_input(changes):
    with pytest.raises(autoinstall.AutoinstallError):
        autoinstall.build_document(InstallerSettings(**changes))


def test_installer_render_parse_round_trip():
    settings = InstallerSettings(snaps=["firefox", "code --classic", "lxd/5.21/stable"], packages=["htop"],
                                 late_commands=["curtin in-target -- true"], extra_yaml="early-commands: [echo hi]")
    document = autoinstall.parse(autoinstall.render(settings))
    assert document["snaps"] == [{"name": "firefox"}, {"name": "code", "classic": True},
                                 {"name": "lxd", "channel": "5.21/stable"}]  # fmt: skip
    assert document["early-commands"] == ["echo hi"] and document["version"] == 1
    with pytest.raises(autoinstall.AutoinstallError):
        autoinstall.parse("autoinstall:\n  locale: en_US\n")  # no version


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_password_hash():
    hashed = autoinstall.hash_password("secret")
    assert hashed.startswith("$6$") and "secret" not in hashed
    with pytest.raises(autoinstall.AutoinstallError):
        autoinstall.hash_password("")


# -- customizer validation of the new settings ------------------------------------------------


@pytest.fixture
def tool(tmp_path):
    return customizer.Customizer(Workspace(tmp_path / "ws"), CommandRunner())


def test_branding_inputs_are_validated(tool, tmp_path):
    png = tmp_path / "logo.png"
    png.write_bytes(images.solid_png(2, 2, (0, 0, 0, 255)))
    text = tmp_path / "notes.png"
    text.write_text("not really a png")
    ok = CustomizationConfig(splash_logo=str(png), splash_color="#102030", grub_background=str(png),
                             grub_text_color="white", grub_highlight_color="black/light-gray",
                             login_background=str(png), login_message="Welcome", target_grub_theme="background",
                             autoinstall_yaml=autoinstall.render(InstallerSettings()))  # fmt: skip
    tool.validate(ok)
    for bad in (
        {"splash_logo": str(text)}, {"splash_color": "navy"}, {"grub_text_color": "pink"},
        {"grub_highlight_color": "black"}, {"grub_cfg_text": "set timeout=5\n"}, {"login_message": "it's"},
        {"target_grub_theme": "background"}, {"target_grub_cmdline": "quiet $(reboot)"},
        {"theme_archives": [str(png)]}, {"autoinstall_yaml": "autoinstall: {}"},
        {"splash_logo": str(png), "plymouth_theme": "spinner"},
    ):  # fmt: skip
        with pytest.raises(customizer.CustomizationError):
            tool.validate(CustomizationConfig(**bad))


def test_require_theme_lists_what_exists(tool, tmp_path):
    rootfs = tmp_path / "root"
    (rootfs / "usr/share/icons/Papirus").mkdir(parents=True)
    (rootfs / "usr/share/icons/Papirus/index.theme").write_text("[Icon Theme]\n")
    tool._require_theme(rootfs, "Papirus", "icon")
    with pytest.raises(customizer.CustomizationError, match="Papirus"):
        tool._require_theme(rootfs, "Numix", "icon")


def test_generated_yaml_is_plain_yaml():
    text = autoinstall.render(InstallerSettings(realname="Zoë"))
    assert yaml.safe_load(text)["autoinstall"]["version"] == 1
