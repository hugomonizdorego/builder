"""ISO level operations: inspect, extract and rebuild hybrid boot images.

Boot strategy
-------------
A remastered ISO has to boot exactly like the original: legacy BIOS, UEFI
and — because the EFI partition holds Canonical's *signed* shim and GRUB —
UEFI with Secure Boot enabled. Instead of guessing the right ``xorriso``
incantation per release, :func:`analyze_boot` asks xorriso to describe the
source image (``-report_el_torito as_mkisofs``), copies the boot sector and
EFI system partition it refers to into the workspace and stores the
resulting option list. :func:`build_iso` replays those options, so the new
image has the same MBR, GPT, El Torito catalog and signed EFI binaries as
the one it was made from. This works for GRUB-only images (22.04 and later)
as well as for the older isolinux + GRUB layout (20.04).

Only when the source image carries no boot information at all are fresh
boot images generated with ``grub-mkimage``/``grub-mkstandalone`` and
``mtools`` (:func:`regenerate_boot_images`). Those are unsigned and will not
pass Secure Boot.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from ubuntu_builder.core.errors import BuilderError, CommandError, WorkspaceError
from ubuntu_builder.core.runner import CommandRunner, parse_percent
from ubuntu_builder.models.workspace import BOOT_DIR_TOKEN

ProgressCallback = Callable[[float], None]

_INTERVAL = re.compile(r"^--interval:local_fs:(\d+)([sd])-(\d+)([sd]):([^:]*):(.+)$")
_APPENDED = re.compile(r"^--interval:appended_partition_(\d+)[^:]*:(.*)$")
_LSDL = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s.*'(/[^']+)'$")
_RESTORED = re.compile(r"\(\s*([\d.]+)([kmgt])\)")
_UNIT = {"s": 2048, "d": 512}

#: Volume identifiers are limited to 32 characters by ISO 9660.
MAX_VOLUME_ID = 32


def read_volume_id(runner: CommandRunner, iso: Path) -> str:
    """Return the volume label of ``iso`` (empty if it has none)."""
    output = runner.capture(["xorriso", "-indev", iso, "-pvd_info"])
    match = re.search(r"^Volume Id\s*:\s*(.*)$", output, re.MULTILINE)
    return match.group(1).strip() if match else ""


def list_squashfs(runner: CommandRunner, iso: Path) -> dict[str, int]:
    """Squashfs images in ``/casper`` of ``iso``: ``{relative path: size}``."""
    output = runner.capture(
        ["xorriso", "-indev", iso, "-find", "/casper", "-name", "*.squashfs", "-exec", "lsdl", "--"],
        check=False,
    )
    found: dict[str, int] = {}
    for line in output.splitlines():
        match = _LSDL.match(line.strip())
        if match:
            found[match.group(2).lstrip("/")] = int(match.group(1))
    return found


def read_iso_file(runner: CommandRunner, iso: Path, path: str) -> bytes | None:
    """Read one small file out of ``iso``; ``None`` if it does not exist."""
    with tempfile.TemporaryDirectory(prefix="ubuntu-builder-") as tmp:
        target = Path(tmp) / "file"
        code = runner.run(
            ["xorriso", "-osirrox", "on", "-indev", iso, "-extract", "/" + path.lstrip("/"), target],
            check=False,
            quiet=True,
        )
        return target.read_bytes() if code == 0 and target.is_file() else None


def extract_tree(runner: CommandRunner, iso: Path, dest: Path, progress: ProgressCallback | None = None) -> None:
    """Extract the whole ISO file tree into ``dest`` without mounting it."""
    total = max(iso.stat().st_size, 1)
    factor = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}

    def on_line(line: str) -> None:
        match = _RESTORED.search(line)
        if match and progress is not None:
            done = float(match.group(1)) * factor[match.group(2)]
            progress(min(100.0, done * 100.0 / total))

    dest.mkdir(parents=True, exist_ok=True)
    runner.run(["xorriso", "-osirrox", "on", "-indev", iso, "-extract", "/", dest], on_line=on_line)
    # ISO 9660 files come out read-only; the tree has to be editable.
    runner.run(["chmod", "-R", "u+w", dest], quiet=True)


def _copy_range(source: Path, target: Path, offset: int, length: int) -> None:
    with source.open("rb") as src, target.open("wb") as dst:
        src.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = src.read(min(remaining, 4 * 1024 * 1024))
            if not chunk:
                raise WorkspaceError(f"{source} is shorter than its boot record claims.")
            dst.write(chunk)
            remaining -= len(chunk)


def analyze_boot(runner: CommandRunner, iso: Path, boot_dir: Path) -> list[str]:
    """Derive reusable boot options from ``iso``.

    Byte ranges of the source image referenced by the report (MBR, EFI
    system partition) are copied to ``boot_dir`` so the original ISO is not
    needed again. Paths in the returned options use ``BOOT_DIR_TOKEN``.
    """
    report = runner.capture(["xorriso", "-indev", iso, "-report_el_torito", "as_mkisofs"])
    tokens = shlex.split(report)
    boot_dir.mkdir(parents=True, exist_ok=True)

    options: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "-V":  # the volume id is chosen at build time
            skip_next = True
            continue
        if token.startswith("--modification-date"):
            continue  # let the new image carry its own timestamp
        interval = _INTERVAL.match(token)
        if interval:
            start = int(interval.group(1)) * _UNIT[interval.group(2)]
            end = (int(interval.group(3)) + 1) * _UNIT[interval.group(4)]
            flags = interval.group(5)
            name = f"boot-{len(list(boot_dir.glob('boot-*.img')))}.img"
            _copy_range(iso, boot_dir / name, start, end - start)
            if flags:
                # Keep xorriso's post-processing (e.g. zeroing the partition
                # table inside the copied MBR) by staying in interval syntax.
                last = (end - start) // 512 - 1
                options.append(f"--interval:local_fs:0d-{last}d:{flags}:{BOOT_DIR_TOKEN}/{name}")
            else:
                options.append(f"{BOOT_DIR_TOKEN}/{name}")
            continue
        appended = _APPENDED.match(token)
        if appended:
            # The report embeds the old start/size; the generic form lets
            # xorriso locate the partition in the *new* image.
            options.append(f"--interval:appended_partition_{appended.group(1)}:{appended.group(2)}")
            continue
        options.append(token)
    return options


def has_boot_record(options: list[str]) -> bool:
    return "-b" in options or "-e" in options


def boot_image_paths(options: list[str]) -> set[str]:
    """ISO paths of El Torito boot images (patched by xorriso at build time)."""
    paths = {"boot.catalog", "isolinux/boot.cat"}
    for flag, value in zip(options, options[1:], strict=False):
        if flag in ("-b", "-e", "-c") and value.startswith("/"):
            paths.add(value.lstrip("/"))
    return paths


def regenerate_boot_images(runner: CommandRunner, iso_dir: Path, boot_dir: Path) -> list[str]:
    """Fallback: build unsigned GRUB boot images for BIOS and UEFI.

    Needs ``grub-pc-bin``, ``grub-efi-amd64-bin`` and ``mtools`` on the host.
    The result boots on BIOS and on UEFI with Secure Boot *disabled*.
    """
    grub_cfg = iso_dir / "boot" / "grub" / "grub.cfg"
    if not grub_cfg.is_file():
        raise WorkspaceError(
            "The source ISO has no boot record and no boot/grub/grub.cfg; "
            "a bootable image cannot be generated."
        )
    boot_dir.mkdir(parents=True, exist_ok=True)
    bios_dir = iso_dir / "boot" / "grub" / "i386-pc"
    bios_dir.mkdir(parents=True, exist_ok=True)
    runner.log("WARNING: generating unsigned boot images — Secure Boot will reject this ISO.")
    try:
        runner.run(
            ["grub-mkimage", "-O", "i386-pc-eltorito", "-p", "/boot/grub", "-o", bios_dir / "eltorito.img",
             "biosdisk", "iso9660", "normal", "search", "configfile", "linux", "part_gpt", "part_msdos"]
        )
        efi_binary = boot_dir / "bootx64.efi"
        runner.run(
            ["grub-mkstandalone", "-O", "x86_64-efi", "-o", efi_binary,
             "--modules=part_gpt part_msdos iso9660 search configfile linux normal",
             f"boot/grub/grub.cfg={_write_search_stub(boot_dir)}"]
        )
        efi_image = boot_dir / "efi.img"
        size_kib = efi_binary.stat().st_size // 1024 + 1024
        with efi_image.open("wb") as handle:
            handle.truncate(size_kib * 1024)
        runner.run(["mformat", "-i", efi_image, "-F", "::"])
        runner.run(["mmd", "-i", efi_image, "::/EFI", "::/EFI/boot"])
        runner.run(["mcopy", "-i", efi_image, efi_binary, "::/EFI/boot/bootx64.efi"])
    except CommandError as exc:
        raise BuilderError(
            "Could not generate boot images. Install grub-pc-bin, grub-efi-amd64-bin and mtools.\n"
            f"{exc}"
        ) from exc
    return [
        "--grub2-mbr", "/usr/lib/grub/i386-pc/boot_hybrid.img",
        "-partition_offset", "16",
        "--mbr-force-bootable",
        "-append_partition", "2", "28732ac11ff8d211ba4b00a0c93ec93b", f"{BOOT_DIR_TOKEN}/efi.img",
        "-appended_part_as_gpt",
        "-iso_mbr_part_type", "a2a0d0ebe5b9334487c068b6b72699c7",
        "-c", "/boot.catalog",
        "-b", "/boot/grub/i386-pc/eltorito.img",
        "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table", "--grub2-boot-info",
        "-eltorito-alt-boot",
        "-e", "--interval:appended_partition_2:all::",
        "-no-emul-boot",
    ]  # fmt: skip


def _write_search_stub(boot_dir: Path) -> Path:
    """Tiny embedded config that hands over to the grub.cfg on the ISO."""
    stub = boot_dir / "grub-stub.cfg"
    stub.write_text(
        "search --no-floppy --set=root --file /boot/grub/grub.cfg\n"
        "set prefix=($root)/boot/grub\n"
        "configfile /boot/grub/grub.cfg\n",
        encoding="utf-8",
    )
    return stub


def write_md5sums(iso_dir: Path, exclude: set[str], runner: CommandRunner | None = None) -> int:
    """Regenerate ``md5sum.txt`` (used by casper's media integrity check).

    Files in ``exclude`` (boot images patched at build time) are left out,
    as in the official images. Returns the number of files hashed.
    """
    skip = exclude | {"md5sum.txt"}
    lines: list[str] = []
    for path in sorted(p for p in iso_dir.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = path.relative_to(iso_dir).as_posix()
        if relative in skip:
            continue
        if runner is not None:
            runner.check_cancelled()
        digest = hashlib.md5(usedforsecurity=False)  # noqa: S324 - format mandated by casper
        with path.open("rb") as handle:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  ./{relative}")
    (iso_dir / "md5sum.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def sanitize_volume_id(volume_id: str) -> str:
    """Trim a volume label to what ISO 9660 can store."""
    cleaned = re.sub(r"[^\x20-\x7e]", "", volume_id).strip()
    return cleaned[:MAX_VOLUME_ID]


def build_iso(
    runner: CommandRunner,
    iso_dir: Path,
    output: Path,
    volume_id: str,
    boot_options: list[str],
    boot_dir: Path,
    progress: ProgressCallback | None = None,
) -> None:
    """Assemble the hybrid BIOS/UEFI ISO from ``iso_dir``."""
    options = [opt.replace(BOOT_DIR_TOKEN, str(boot_dir)) for opt in boot_options]

    def on_line(line: str) -> None:
        percent = parse_percent(line)
        if percent is not None and progress is not None and "done" in line:
            progress(percent)

    partial = output.with_name(output.name + ".part")
    try:
        runner.run(
            ["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long", "-l", "-iso-level", "3",
             "-V", sanitize_volume_id(volume_id), "-o", partial, *options, iso_dir],
            on_line=on_line,
        )  # fmt: skip
        shutil.move(partial, output)  # only a complete image gets the final name
    finally:
        partial.unlink(missing_ok=True)
