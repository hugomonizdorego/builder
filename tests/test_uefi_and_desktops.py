"""UEFI boot support and the desktop / login manager catalog."""

import struct
from pathlib import Path

import pytest
from os_builder.core import customizer, iso
from os_builder.core.errors import BuilderError
from os_builder.core.runner import CommandRunner
from os_builder.models import DESKTOPS, DESKTOPS_BY_ID, DISPLAY_MANAGERS_BY_ID, CustomizationConfig, Workspace
from os_builder.models.desktops import KEEP_DISPLAY_MANAGER

HYBRID = [
    "--grub2-mbr", "--interval:local_fs:0d-63d:zero_mbrpt,zero_gpt:@BOOT@/boot-0.img",
    "--protective-msdos-label", "-partition_offset", "16", "--mbr-force-bootable",
    "-append_partition", "2", "28732ac11ff8d211ba4b00a0c93ec93b", "@BOOT@/boot-1.img", "-appended_part_as_gpt",
    "-c", "/boot.catalog",
    "-b", "/boot/grub/i386-pc/eltorito.img", "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
    "--grub2-boot-info",
    "-eltorito-alt-boot",
    "-e", "--interval:appended_partition_2:all::", "-no-emul-boot", "-boot-load-size", "10296",
]  # fmt: skip


def fake_pe(signed: bool, pe32_plus: bool = True) -> bytes:
    """Minimal PE/COFF header with or without a certificate table entry."""
    data = bytearray(1024)
    data[:2] = b"MZ"
    data[0x3C:0x40] = struct.pack("<I", 0x80)
    data[0x80:0x84] = b"PE\0\0"
    optional = 0x80 + 24
    data[optional : optional + 2] = struct.pack("<H", 0x20B if pe32_plus else 0x10B)
    security = optional + (112 if pe32_plus else 96) + 4 * 8
    if signed:
        data[security : security + 8] = struct.pack("<II", 0x300, 0x100)
    return bytes(data)


def test_authenticode_detection():
    assert iso.pe_is_signed(fake_pe(True))
    assert iso.pe_is_signed(fake_pe(True, pe32_plus=False))
    assert not iso.pe_is_signed(fake_pe(False))
    assert not iso.pe_is_signed(b"not a PE file")


def test_uefi_only_drops_every_bios_part():
    uefi = iso.uefi_only_options(HYBRID)
    for gone in ("--grub2-mbr", "-b", "/boot/grub/i386-pc/eltorito.img", "-boot-info-table", "--grub2-boot-info",
                 "--mbr-force-bootable", "-eltorito-alt-boot"):
        assert gone not in uefi
    assert uefi[uefi.index("-e") :] == ["-e", "--interval:appended_partition_2:all::", "-no-emul-boot",
                                        "-boot-load-size", "10296"]  # fmt: skip
    assert "-appended_part_as_gpt" in uefi and "@BOOT@/boot-1.img" in uefi
    with pytest.raises(BuilderError):
        iso.uefi_only_options(["-b", "/isolinux/isolinux.bin", "-no-emul-boot"])


def test_boot_report(tmp_path):
    efi = tmp_path / "EFI" / "boot"
    efi.mkdir(parents=True)
    (efi / "bootx64.efi").write_bytes(fake_pe(True))
    (efi / "grubx64.efi").write_bytes(fake_pe(True))
    (efi / "mmx64.efi").write_bytes(fake_pe(True))
    report = iso.boot_report(HYBRID, tmp_path)
    assert (report.bios, report.uefi, report.secure_boot) == (True, True, True)
    assert "Secure Boot ✔" in report.summary()
    uefi_only = iso.boot_report(iso.uefi_only_options(HYBRID), tmp_path)
    assert (uefi_only.bios, uefi_only.uefi) == (False, True)
    (efi / "bootx64.efi").write_bytes(fake_pe(False))
    assert not iso.boot_report(HYBRID, tmp_path).secure_boot


def test_catalog_is_consistent():
    assert len(DESKTOPS_BY_ID) == len(DESKTOPS)
    for desktop in DESKTOPS:
        assert desktop.packages and desktop.display_manager in DISPLAY_MANAGERS_BY_ID, desktop.id
        customizer.validate_packages(list(desktop.packages))
    for manager in DISPLAY_MANAGERS_BY_ID.values():
        customizer.validate_packages(list(manager.packages))
        assert manager.service.endswith(".service") and manager.binary.startswith("/usr/")


def test_display_manager_choice():
    pick = customizer.Customizer.chosen_display_manager
    assert pick(CustomizationConfig()) is None
    assert pick(CustomizationConfig(desktop="kde")).id == "sddm"
    assert pick(CustomizationConfig(desktop="kde", display_manager="greetd")).id == "greetd"
    assert pick(CustomizationConfig(desktop="sway", display_manager=KEEP_DISPLAY_MANAGER)) is None
    assert pick(CustomizationConfig(display_manager="gdm3")).id == "gdm3"


@pytest.mark.parametrize("changes", [{"desktop": "windows-11"}, {"display_manager": "kdm"}])
def test_unknown_desktop_or_manager_is_rejected(tmp_path, changes):
    with pytest.raises(customizer.CustomizationError):
        customizer.Customizer(Workspace(tmp_path), CommandRunner()).validate(CustomizationConfig(**changes))


class FakeSession:
    def __init__(self, rootfs: Path) -> None:
        self.rootfs = rootfs


@pytest.mark.parametrize("manager_id", ["lightdm-slick", "greetd", "sddm"])
def test_set_display_manager(tmp_path, manager_id):
    manager = DISPLAY_MANAGERS_BY_ID[manager_id]
    rootfs = tmp_path / "root"
    unit = rootfs / "usr/lib/systemd/system" / manager.service
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    tool = customizer.Customizer(Workspace(tmp_path / "ws"), CommandRunner())
    tool._set_display_manager(manager, FakeSession(rootfs))
    assert (rootfs / "etc/X11/default-display-manager").read_text() == manager.binary + "\n"
    link = rootfs / "etc/systemd/system/display-manager.service"
    assert link.is_symlink() and str(link.readlink()) == f"/usr/lib/systemd/system/{manager.service}"
    if manager_id == "lightdm-slick":
        assert "greeter-session=slick-greeter" in (rootfs / "etc/lightdm/lightdm.conf.d/50-os-builder.conf").read_text()
    if manager_id == "greetd":
        assert "tuigreet" in (rootfs / "etc/greetd/config.toml").read_text()


def test_missing_unit_is_an_error(tmp_path):
    tool = customizer.Customizer(Workspace(tmp_path / "ws"), CommandRunner())
    with pytest.raises(customizer.CustomizationError):
        tool._set_display_manager(DISPLAY_MANAGERS_BY_ID["gdm3"], FakeSession(tmp_path / "empty"))


def test_old_config_files_still_load():
    assert CustomizationConfig.from_dict({"desktop": "KDE"}).desktop == "kde"
    assert CustomizationConfig.from_dict({"desktop": "KEEP"}).desktop == ""
