"""Backend tests that need neither root nor network."""

import threading
import time
from pathlib import Path

import pytest
from os_builder.core import customizer, iso, layers
from os_builder.core.errors import CommandError, InsufficientDiskSpaceError, OperationCancelledError
from os_builder.core.runner import CommandRunner, parse_squashfs_progress
from os_builder.core.system import require_free_space
from os_builder.models import CustomizationConfig, ProjectState, Workspace
from os_builder.models.workspace import BOOT_DIR_TOKEN

# -- runner ---------------------------------------------------------------------


def test_runner_streams_lines_and_treats_carriage_return_as_break():
    lines: list[str] = []
    CommandRunner().run(["sh", "-c", r"printf 'one\rtwo\nthree'"], on_line=lines.append)
    assert lines == ["one", "two", "three"]


def test_runner_raises_with_output_tail():
    with pytest.raises(CommandError) as info:
        CommandRunner().run(["sh", "-c", "echo boom; exit 3"])
    assert info.value.returncode == 3 and "boom" in info.value.output_tail


def test_runner_missing_binary():
    with pytest.raises(CommandError):
        CommandRunner().run(["definitely-not-a-command-xyz"])


def test_runner_cancel_kills_the_process_group():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelledError):
        CommandRunner(cancel_event=cancel).run(["sh", "-c", "sleep 30 & wait"])
    assert time.monotonic() - started < 10


def test_runner_pty_makes_child_see_a_terminal():
    assert CommandRunner().capture(["sh", "-c", "echo plain"]) == "plain"
    lines: list[str] = []
    CommandRunner().run(["sh", "-c", "test -t 1 && echo tty"], on_line=lines.append, use_pty=True)
    assert lines == ["tty"]


def test_squashfs_progress_ignores_summary_percentages():
    assert parse_squashfs_progress("[=====/      ] 1200/4800  25%") == 25
    assert parse_squashfs_progress("Filesystem size 123.4 Kbytes (14.50% of uncompressed)") is None


# -- disk space -------------------------------------------------------------------


def test_disk_space_check(tmp_path):
    require_free_space(tmp_path / "not" / "yet" / "there", 1)
    with pytest.raises(InsufficientDiskSpaceError) as info:
        require_free_space(tmp_path, 10**18)
    assert "GiB needed" in str(info.value)


# -- layers -----------------------------------------------------------------------

DESKTOP = [
    "casper/minimal.squashfs",
    "casper/minimal.de.squashfs",
    "casper/minimal.standard.squashfs",
    "casper/minimal.standard.live.squashfs",
]


def test_lower_layers_follow_the_dotted_name():
    assert layers.lower_layers("casper/minimal.standard.live.squashfs", DESKTOP) == [
        "casper/minimal.squashfs",
        "casper/minimal.standard.squashfs",
    ]
    assert layers.lower_layers("casper/minimal.squashfs", DESKTOP) == []
    assert layers.lower_layers("casper/filesystem.squashfs", ["casper/filesystem.squashfs"]) == []


def test_default_target():
    assert layers.default_target(["casper/filesystem.squashfs"]) == "casper/filesystem.squashfs"
    assert layers.default_target(DESKTOP) == "casper/minimal.squashfs"
    v2 = b"version: 2\nsources:\n- path: minimal.squashfs\n- path: minimal.standard.squashfs\n  default: true\n"
    assert layers.default_target(DESKTOP, v2) == "casper/minimal.standard.squashfs"
    v1 = b"- path: minimal.squashfs\n  default: true\n"
    assert layers.default_target(DESKTOP, v1) == "casper/minimal.squashfs"
    assert layers.default_target(DESKTOP, b"{not yaml") == "casper/minimal.squashfs"
    assert layers.default_target([]) is None


# -- ISO helpers --------------------------------------------------------------------

REPORT = """-V 'Ubuntu 26.04 LTS amd64'
--modification-date='2026042012000000'
--grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:'{iso}'
-partition_offset 16
-append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b --interval:local_fs:40d-47d::'{iso}'
-appended_part_as_gpt
-c '/boot.catalog'
-b '/boot/grub/i386-pc/eltorito.img'
-no-emul-boot
-eltorito-alt-boot
-e '--interval:appended_partition_2_start_10s_size_8d:all::'
-no-emul-boot
"""


class FakeRunner(CommandRunner):
    def __init__(self, output: str) -> None:
        super().__init__()
        self._output = output

    def capture(self, argv, *, check=True):  # noqa: ARG002
        return self._output


def test_analyze_boot_copies_boot_areas_and_rewrites_options(tmp_path):
    source = tmp_path / "my source.iso"
    source.write_bytes(bytes(range(256)) * 256)  # 64 KiB, position dependent content
    boot_dir = tmp_path / "boot"
    options = iso.analyze_boot(FakeRunner(REPORT.format(iso=source)), source, boot_dir)

    assert "-V" not in options and not any(o.startswith("--modification-date") for o in options)
    mbr = options[options.index("--grub2-mbr") + 1]
    assert mbr == f"--interval:local_fs:0d-63d:zero_mbrpt,zero_gpt:{BOOT_DIR_TOKEN}/boot-0.img"
    assert (boot_dir / "boot-0.img").read_bytes() == source.read_bytes()[: 16 * 2048]
    efi = options[options.index("-append_partition") + 3]
    assert efi == f"{BOOT_DIR_TOKEN}/boot-1.img"
    assert (boot_dir / "boot-1.img").read_bytes() == source.read_bytes()[40 * 512 : 48 * 512]
    assert options[options.index("-e") + 1] == "--interval:appended_partition_2:all::"
    assert iso.has_boot_record(options)
    assert iso.boot_image_paths(options) >= {"boot.catalog", "boot/grub/i386-pc/eltorito.img"}


def test_md5sums_skip_boot_images(tmp_path):
    (tmp_path / "casper").mkdir()
    (tmp_path / "casper" / "vmlinuz").write_bytes(b"kernel")
    (tmp_path / "boot.catalog").write_bytes(b"x")
    (tmp_path / "md5sum.txt").write_text("stale")
    assert iso.write_md5sums(tmp_path, {"boot.catalog"}) == 1
    assert (tmp_path / "md5sum.txt").read_text().endswith("  ./casper/vmlinuz\n")


def test_volume_id_is_trimmed():
    assert iso.sanitize_volume_id("  DebION GNU/Linux 26.04 — édition spéciale ") == (
        "DebION GNU/Linux 26.04  dition s"
    )


# -- customizer -----------------------------------------------------------------------

GRUB_CFG = """set timeout=30
menuentry "Try or Install Ubuntu Server" {
\tset gfxpayload=keep
\tlinux\t/casper/vmlinuz  ---
\tinitrd\t/casper/initrd
}
menuentry 'Ubuntu (safe graphics)' {
\tlinux\t/casper/vmlinuz nomodeset quiet --- quiet
}
menuentry 'UEFI Firmware Settings' {
\tfwsetup
}
"""


def test_grub_timeout_set_or_added():
    assert "set timeout=5\n" in customizer.set_grub_timeout(GRUB_CFG, 5)
    assert customizer.set_grub_timeout("menuentry x {}\n", 0).startswith("set timeout=0\n")


def test_kernel_args_go_before_separator_and_are_not_duplicated():
    patched = customizer.add_kernel_args(GRUB_CFG, "quiet splash")
    assert "linux\t/casper/vmlinuz quiet splash ---\n" in patched
    assert "linux\t/casper/vmlinuz nomodeset quiet splash --- quiet\n" in patched
    assert "initrd\t/casper/initrd" in patched
    assert customizer.add_kernel_args(patched, "quiet splash") == patched  # idempotent


def test_menu_entries_are_renamed():
    patched = customizer.rename_menu_entries(GRUB_CFG, "DebION GNU/Linux")
    assert 'menuentry "Try or Install DebION GNU/Linux" {' in patched
    assert "menuentry 'DebION GNU/Linux (safe graphics)' {" in patched
    assert "menuentry 'UEFI Firmware Settings' {" in patched


@pytest.mark.parametrize(
    "changes",
    [
        {"extra_packages": ["vlc", "--allow-unauthenticated"]},
        {"remove_packages": ["foo; rm -rf /"]},
        {"distro_name": 'Evil"; reboot'},
        {"plymouth_theme": "../../etc"},
        {"grub_extra_kernel_args": "quiet $(reboot)"},
        {"grub_timeout": 9999},
        {"wallpaper_path": "/nonexistent.png"},
    ],
)
def test_validation_rejects_unsafe_input(tmp_path, changes):
    config = CustomizationConfig(**changes)
    with pytest.raises(customizer.CustomizationError):
        customizer.Customizer(Workspace(tmp_path), CommandRunner()).validate(config)


def test_validation_accepts_reasonable_input(tmp_path):
    autoinstall = tmp_path / "autoinstall.yaml"
    autoinstall.write_text("autoinstall:\n  version: 1\n")
    config = CustomizationConfig(
        distro_name="DebION GNU/Linux",
        grub_timeout=5,
        extra_packages=["vlc", "libreoffice-l10n-id", "g++"],
        desktop="kde",
        display_manager="greetd",
        autoinstall_path=str(autoinstall),
    )
    customizer.Customizer(Workspace(tmp_path), CommandRunner()).validate(config)
    assert CustomizationConfig.from_dict(config.to_dict()) == config  # JSON round trip used by the GUI


# -- workspace ------------------------------------------------------------------------


def test_project_state_round_trip(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    assert workspace.load_state() == ProjectState()
    state = ProjectState(
        source_iso="/x.iso",
        volume_id="Ubuntu",
        target_squashfs="casper/minimal.standard.squashfs",
        lower_squashfs=["casper/minimal.squashfs"],
        boot_options=["-b", "/boot/grub/i386-pc/eltorito.img"],
        extracted=True,
    )
    workspace.save_state(state)
    assert workspace.load_state() == state and state.is_layered
    assert workspace.edit_dir == Path(tmp_path / "ws" / "edit")
