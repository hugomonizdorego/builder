"""Pre-flight check: verify the host tools the builder depends on."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HostTool:
    """A host dependency, located through a binary name or a file path."""

    name: str
    probe: str  # binary looked up in PATH, or an absolute path
    package: str  # apt package providing it
    purpose: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class ToolStatus:
    tool: HostTool
    location: str | None

    @property
    def found(self) -> bool:
        return self.location is not None


HOST_TOOLS: tuple[HostTool, ...] = (
    HostTool("unsquashfs", "unsquashfs", "squashfs-tools", "Unpack the root file system"),
    HostTool("mksquashfs", "mksquashfs", "squashfs-tools", "Repack the root file system"),
    HostTool("xorriso", "xorriso", "xorriso", "Read and build hybrid BIOS/UEFI ISO images"),
    HostTool("chroot", "chroot", "coreutils", "Enter the image"),
    HostTool("mount", "mount", "mount", "Bind mounts for the chroot"),
    HostTool("pkexec", "pkexec", "pkexec", "Ask for administrator rights (not needed when run as root)"),
    HostTool("mtools", "mformat", "mtools", "Rebuild the EFI boot partition (fallback only)", required=False),
    HostTool(
        "grub BIOS modules",
        "/usr/lib/grub/i386-pc/boot_hybrid.img",
        "grub-pc-bin",
        "Rebuild the BIOS boot image (fallback only)",
        required=False,
    ),
    HostTool(
        "grub UEFI modules",
        "/usr/lib/grub/x86_64-efi/normal.mod",
        "grub-efi-amd64-bin",
        "Rebuild the UEFI boot image (fallback only)",
        required=False,
    ),
    HostTool("Xephyr", "Xephyr", "xserver-xephyr", "Run the image's desktop in a window", required=False),
    HostTool("qemu", "qemu-system-x86_64", "qemu-system-x86", "Test-boot the finished ISO", required=False),
    HostTool("qemu-img", "qemu-img", "qemu-utils", "Create a scratch disk for the test VM", required=False),
    HostTool(
        "OVMF firmware",
        "/usr/share/OVMF/OVMF_CODE_4M.fd",
        "ovmf",
        "Test-boot the ISO in UEFI mode",
        required=False,
    ),
)


def _locate(tool: HostTool) -> str | None:
    if tool.probe.startswith("/"):
        return tool.probe if Path(tool.probe).exists() else None
    # Administrative tools live in sbin, which is not on every user's PATH.
    path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
    return shutil.which(tool.probe, path=path)


def check_host_tools(*, running_as_root: bool | None = None) -> list[ToolStatus]:
    """Probe every known host tool."""
    if running_as_root is None:
        running_as_root = os.geteuid() == 0
    statuses = []
    for tool in HOST_TOOLS:
        if tool.name == "pkexec" and running_as_root:
            continue  # no privilege escalation needed
        statuses.append(ToolStatus(tool, _locate(tool)))
    return statuses


def missing_required(statuses: list[ToolStatus]) -> list[ToolStatus]:
    return [s for s in statuses if s.tool.required and not s.found]


def install_command(statuses: list[ToolStatus], *, include_optional: bool = False) -> str:
    """The ``apt`` command line installing whatever is missing."""
    packages = sorted(
        {s.tool.package for s in statuses if not s.found and (s.tool.required or include_optional)}
    )
    return f"sudo apt install {' '.join(packages)}" if packages else ""
