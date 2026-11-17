"""Settings of the Subiquity installer, written out as ``autoinstall.yaml``.

Subiquity (the Ubuntu Server installer) and the Ubuntu Desktop installer
read ``/autoinstall.yaml`` from the install medium. It pre-answers the
installer's questions, chooses which screens are still shown
(``interactive-sections``), and adds packages, snaps and commands. This is
the supported way to adapt the installer to a custom distribution without
patching Subiquity itself.

Reference: https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

#: Installer screens that can stay interactive while the rest is answered.
INTERACTIVE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("locale", "Language"),
    ("keyboard", "Keyboard"),
    ("network", "Network"),
    ("proxy", "Proxy"),
    ("apt", "Mirror"),
    ("storage", "Disk / partitioning"),
    ("identity", "User and computer name"),
    ("timezone", "Time zone"),
    ("ssh", "SSH server"),
    ("snaps", "Featured snaps"),
    ("drivers", "Third-party drivers"),
    ("codecs", "Multimedia codecs"),
)

MODES = ("guided", "partial", "automatic")
STORAGE_LAYOUTS = ("lvm", "direct", "zfs")


@dataclass(slots=True)
class InstallerSettings:
    """What the ISO maker presets in the installer."""

    #: guided: every screen shown, pre-filled with these answers;
    #: partial: only ``interactive`` screens shown;
    #: automatic: nothing asked — the disk is erased without confirmation.
    mode: str = "guided"
    interactive: list[str] = field(default_factory=lambda: ["storage", "identity"])
    locale: str = "en_US.UTF-8"
    keyboard_layout: str = "us"
    keyboard_variant: str = ""
    timezone: str = "Etc/UTC"
    hostname: str = ""
    realname: str = ""
    username: str = ""
    password_hash: str = ""  # SHA-512 crypt; the clear text is never stored
    storage_layout: str = "lvm"
    storage_encrypted: bool = False  # LUKS; the passphrase is asked at install time
    ssh_server: bool = False
    ssh_allow_password: bool = True
    ssh_keys: list[str] = field(default_factory=list)
    updates: str = "security"  # or "all"
    drivers: bool = True
    codecs: bool = True
    packages: list[str] = field(default_factory=list)
    snaps: list[str] = field(default_factory=list)
    late_commands: list[str] = field(default_factory=list)
    refresh_installer: bool = False
    shutdown: str = "reboot"
    #: Free YAML merged on top, for anything the form does not cover.
    extra_yaml: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallerSettings:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
