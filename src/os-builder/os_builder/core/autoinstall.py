"""Generate and check Subiquity ``autoinstall.yaml`` files.

The generated document is written to the root of the ISO, where Subiquity
(server) and the Ubuntu Desktop installer look for it. With the
``autoinstall`` kernel argument on the boot menu the installation runs
without asking for confirmation; without it, the installer asks once.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

import requests
import yaml

from os_builder import APP_ID, __version__
from os_builder.core.errors import BuilderError
from os_builder.models.installer import INTERACTIVE_SECTIONS, MODES, STORAGE_LAYOUTS, InstallerSettings

SCHEMA_URL = "https://raw.githubusercontent.com/canonical/subiquity/main/autoinstall-schema.json"
_HOSTNAME = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SNAP = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_SECTIONS = {name for name, _label in INTERACTIVE_SECTIONS}


class AutoinstallError(BuilderError):
    """The installer settings or the autoinstall document are invalid."""


def hash_password(password: str) -> str:
    """SHA-512 crypt hash, as Subiquity expects in ``identity.password``."""
    if not password:
        raise AutoinstallError("The password must not be empty.")
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, password on stdin
            ["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AutoinstallError(f"Could not hash the password with openssl: {exc}") from exc
    return done.stdout.strip()


def build_document(settings: InstallerSettings) -> dict[str, Any]:
    """The ``autoinstall:`` mapping for ``settings``."""
    s = settings
    if s.mode not in MODES:
        raise AutoinstallError(f"Unknown installer mode: {s.mode}")
    doc: dict[str, Any] = {"version": 1}
    if s.mode == "guided":
        doc["interactive-sections"] = ["*"]
    elif s.mode == "partial":
        unknown = set(s.interactive) - _SECTIONS
        if unknown:
            raise AutoinstallError(f"Unknown installer screens: {', '.join(sorted(unknown))}")
        doc["interactive-sections"] = list(s.interactive)
    if s.refresh_installer:
        doc["refresh-installer"] = {"update": True}
    doc["locale"] = s.locale
    doc["keyboard"] = {"layout": s.keyboard_layout, **({"variant": s.keyboard_variant} if s.keyboard_variant else {})}
    doc["timezone"] = s.timezone

    asks_identity = s.mode == "guided" or (s.mode == "partial" and "identity" in s.interactive)
    if s.username or s.hostname or s.password_hash:
        if not (s.username and s.hostname and s.password_hash):
            raise AutoinstallError("User name, computer name and password must be given together.")
        if not _USERNAME.match(s.username):
            raise AutoinstallError(f"Invalid user name: {s.username!r}")
        if not _HOSTNAME.match(s.hostname):
            raise AutoinstallError(f"Invalid computer name: {s.hostname!r}")
        doc["identity"] = {
            "hostname": s.hostname, "username": s.username, "password": s.password_hash,
            **({"realname": s.realname} if s.realname else {}),
        }  # fmt: skip
    elif not asks_identity:
        raise AutoinstallError("Set a user name, computer name and password, or keep the user screen interactive.")

    if s.storage_layout not in STORAGE_LAYOUTS:
        raise AutoinstallError(f"Unknown storage layout: {s.storage_layout}")
    doc["storage"] = {"layout": {"name": s.storage_layout}}
    if s.storage_encrypted:
        if s.storage_layout != "lvm":
            raise AutoinstallError("Disk encryption is available with the LVM layout.")
        # The passphrase is never written to the ISO: the storage screen stays
        # interactive so the person installing chooses it.
        sections = doc.setdefault("interactive-sections", [])
        if "*" not in sections and "storage" not in sections:
            sections.append("storage")

    doc["ssh"] = {"install-server": s.ssh_server, "allow-pw": s.ssh_allow_password}
    if s.ssh_keys:
        doc["ssh"]["authorized-keys"] = [key.strip() for key in s.ssh_keys if key.strip()]
    doc["updates"] = s.updates
    doc["drivers"] = {"install": s.drivers}
    doc["codecs"] = {"install": s.codecs}
    for name in s.packages:
        if not _PACKAGE.match(name):
            raise AutoinstallError(f"Invalid package name: {name!r}")
    if s.packages:
        doc["packages"] = list(s.packages)
    snaps = [_snap_entry(spec) for spec in s.snaps if spec.strip()]
    for entry in snaps:
        if not _SNAP.match(entry["name"]):
            raise AutoinstallError(f"Invalid snap name: {entry['name']!r}")
    if snaps:
        doc["snaps"] = snaps
    commands = [line for line in s.late_commands if line.strip()]
    if commands:
        doc["late-commands"] = commands
    doc["shutdown"] = s.shutdown

    if s.extra_yaml.strip():
        try:
            extra = yaml.safe_load(s.extra_yaml)
        except yaml.YAMLError as exc:
            raise AutoinstallError(f"Extra YAML is not valid YAML: {exc}") from exc
        if not isinstance(extra, dict):
            raise AutoinstallError("Extra YAML must be a mapping (key: value).")
        extra = extra.get("autoinstall", extra)
        doc.update(extra)
    return doc


def _snap_entry(spec: str) -> dict[str, Any]:
    """``name``, ``name/channel`` or ``name --classic``."""
    classic = spec.endswith(" --classic")
    spec = spec.removesuffix(" --classic").strip()
    name, _, channel = spec.partition("/")
    entry: dict[str, Any] = {"name": name}
    if channel:
        entry["channel"] = channel
    if classic:
        entry["classic"] = True
    return entry


def render(settings: InstallerSettings) -> str:
    header = (
        f"# Generated by {APP_ID} {__version__}. Reference:\n"
        "# https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html\n"
    )
    return header + yaml.safe_dump({"autoinstall": build_document(settings)}, sort_keys=False, allow_unicode=True)


def parse(text: str) -> dict[str, Any]:
    """Load an autoinstall file (with or without the ``autoinstall:`` wrapper)."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AutoinstallError(f"Not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise AutoinstallError("An autoinstall file must be a YAML mapping.")
    document = document.get("autoinstall", document)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise AutoinstallError("The autoinstall document must contain 'version: 1'.")
    return document


def check_with_official_schema(document: dict[str, Any], timeout: float = 15.0) -> list[str]:
    """Validate against Subiquity's published JSON schema.

    Returns a list of problems (empty when valid). Needs network access and
    the ``jsonschema`` module; raises :class:`AutoinstallError` without them.
    """
    try:
        import jsonschema
    except ImportError as exc:
        raise AutoinstallError("Install python3-jsonschema to check against the official schema.") from exc
    try:
        response = requests.get(SCHEMA_URL, timeout=timeout, headers={"User-Agent": f"{APP_ID}/{__version__}"})
        response.raise_for_status()
        schema = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoinstallError(f"Could not download the official schema: {exc}") from exc
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '(top)'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    ]
