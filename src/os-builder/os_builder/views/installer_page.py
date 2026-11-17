"""Installer tab: a form for Subiquity's ``autoinstall.yaml``."""

from __future__ import annotations

from pathlib import Path

import yaml
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from os_builder.core import autoinstall
from os_builder.models.installer import INTERACTIVE_SECTIONS, InstallerSettings

TIMEZONES = (
    "Etc/UTC", "Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura", "Asia/Singapore", "Asia/Kuala_Lumpur",
    "Asia/Tokyo", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Lisbon",
    "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Sao_Paulo", "Australia/Sydney",
)  # fmt: skip
KEYBOARDS = ("us", "gb", "de", "fr", "es", "it", "pt", "br", "jp", "ru", "tr")


def _mono(edit: QPlainTextEdit) -> QPlainTextEdit:
    edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return edit


class InstallerPage(QWidget):
    """Edits :class:`InstallerSettings`; renders and checks the YAML."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._password_hash = ""
        self.enabled_check = QCheckBox("Write autoinstall.yaml to the ISO (Subiquity and the Ubuntu Desktop installer)")

        # -- behaviour -------------------------------------------------------------
        self.mode_group = QButtonGroup(self)
        self.guided_radio = QRadioButton("Guided — every screen is shown, pre-filled with the answers below")
        self.partial_radio = QRadioButton("Partly automatic — only these screens are shown:")
        self.automatic_radio = QRadioButton("Fully automatic — no questions; the first disk is ERASED")
        for index, radio in enumerate((self.guided_radio, self.partial_radio, self.automatic_radio)):
            self.mode_group.addButton(radio, index)
        self.section_checks: dict[str, QCheckBox] = {}
        sections = QGridLayout()
        for index, (key, label) in enumerate(INTERACTIVE_SECTIONS):
            check = QCheckBox(label)
            self.section_checks[key] = check
            sections.addWidget(check, index // 4, index % 4)
        self.refresh_check = QCheckBox("Update the installer from the Snap Store before installing")
        mode_box = QGroupBox("Behaviour")
        mode_layout = QVBoxLayout(mode_box)
        for widget in (self.guided_radio, self.partial_radio):
            mode_layout.addWidget(widget)
        mode_layout.addLayout(sections)
        mode_layout.addWidget(self.automatic_radio)
        mode_layout.addWidget(self.refresh_check)

        # -- region & user ------------------------------------------------------------
        self.locale_edit = QLineEdit()
        self.keyboard_combo = QComboBox()
        self.keyboard_combo.setEditable(True)
        self.keyboard_combo.addItems(KEYBOARDS)
        self.variant_edit = QLineEdit()
        self.variant_edit.setPlaceholderText("optional, e.g. intl")
        self.timezone_combo = QComboBox()
        self.timezone_combo.setEditable(True)
        self.timezone_combo.addItems(TIMEZONES)
        self.hostname_edit = QLineEdit()
        self.hostname_edit.setPlaceholderText("e.g. debion")
        self.realname_edit = QLineEdit()
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("stored only as a SHA-512 hash")
        self.password2_edit = QLineEdit()
        self.password2_edit.setEchoMode(QLineEdit.EchoMode.Password)
        user_box = QGroupBox("Region and user")
        user_form = QFormLayout(user_box)
        keyboard_row = QHBoxLayout()
        keyboard_row.addWidget(self.keyboard_combo)
        keyboard_row.addWidget(self.variant_edit)
        user_form.addRow("Language:", self.locale_edit)
        user_form.addRow("Keyboard / variant:", keyboard_row)
        user_form.addRow("Time zone:", self.timezone_combo)
        user_form.addRow("Computer name:", self.hostname_edit)
        user_form.addRow("Full name:", self.realname_edit)
        user_form.addRow("User name:", self.username_edit)
        user_form.addRow("Password:", self.password_edit)
        user_form.addRow("Repeat password:", self.password2_edit)

        # -- disk, network, software ---------------------------------------------
        self.storage_combo = QComboBox()
        for value, label in (("lvm", "LVM (recommended)"), ("direct", "Plain partitions"), ("zfs", "ZFS")):
            self.storage_combo.addItem(label, value)
        self.encrypt_check = QCheckBox("Encrypt the disk (the passphrase is asked during installation)")
        self.ssh_check = QCheckBox("Install the OpenSSH server")
        self.ssh_pw_check = QCheckBox("Allow password login over SSH")
        self.ssh_keys_edit = _mono(QPlainTextEdit())
        self.ssh_keys_edit.setPlaceholderText("Authorized SSH public keys, one per line")
        self.ssh_keys_edit.setFixedHeight(48)
        self.updates_combo = QComboBox()
        self.updates_combo.addItem("Security updates", "security")
        self.updates_combo.addItem("All updates", "all")
        self.drivers_check = QCheckBox("Install third-party drivers")
        self.codecs_check = QCheckBox("Install multimedia codecs")
        self.packages_edit = QLineEdit()
        self.packages_edit.setPlaceholderText("installed by the installer, e.g. htop git")
        self.snaps_edit = QLineEdit()
        self.snaps_edit.setPlaceholderText("e.g. firefox, code --classic, lxd/5.21/stable")
        self.commands_edit = _mono(QPlainTextEdit())
        self.commands_edit.setPlaceholderText(
            "late-commands, one per line; run after installation. Use 'curtin in-target --' to run inside "
            "the new system,\ne.g. curtin in-target -- apt-get -y purge popularity-contest"
        )
        self.commands_edit.setFixedHeight(70)
        self.shutdown_combo = QComboBox()
        self.shutdown_combo.addItem("Reboot", "reboot")
        self.shutdown_combo.addItem("Power off", "poweroff")
        system_box = QGroupBox("Disk, network and software")
        system_form = QFormLayout(system_box)
        system_form.addRow("Disk layout:", self.storage_combo)
        system_form.addRow("", self.encrypt_check)
        system_form.addRow("SSH:", self.ssh_check)
        system_form.addRow("", self.ssh_pw_check)
        system_form.addRow("", self.ssh_keys_edit)
        system_form.addRow("Updates:", self.updates_combo)
        system_form.addRow("", self.drivers_check)
        system_form.addRow("", self.codecs_check)
        system_form.addRow("Packages:", self.packages_edit)
        system_form.addRow("Snaps:", self.snaps_edit)
        system_form.addRow("Commands:", self.commands_edit)
        system_form.addRow("When done:", self.shutdown_combo)

        self.extra_edit = _mono(QPlainTextEdit())
        self.extra_edit.setPlaceholderText(
            "Any other autoinstall keys (merged on top), e.g.\nearly-commands:\n  - echo starting\n"
            "user-data:\n  disable_root: true"
        )
        self.extra_edit.setFixedHeight(90)
        extra_box = QGroupBox("Advanced YAML")
        QVBoxLayout(extra_box).addWidget(self.extra_edit)

        preview, check, load, save = (QPushButton(t) for t in ("Preview YAML", "Check with Subiquity schema",
                                                                "Load file…", "Save as…"))  # fmt: skip
        preview.clicked.connect(self._preview)
        check.clicked.connect(self._check)
        load.clicked.connect(self._load)
        save.clicked.connect(self._save)
        actions = QHBoxLayout()
        for button in (preview, check, load, save):
            actions.addWidget(button)
        actions.addStretch(1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        for widget in (mode_box, user_box, system_box, extra_box):
            content_layout.addWidget(widget)
        content_layout.addLayout(actions)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.enabled_check)
        layout.addWidget(scroll, 1)
        self.enabled_check.toggled.connect(content.setEnabled)
        self.mode_group.idToggled.connect(self._mode_changed)
        self.ssh_check.toggled.connect(self.ssh_pw_check.setEnabled)
        self.load(InstallerSettings())
        self.enabled_check.setChecked(False)
        content.setEnabled(False)

    # -- model <-> form --------------------------------------------------------------

    def _mode_changed(self, _id: int, _checked: bool) -> None:
        partial = self.partial_radio.isChecked()
        for check in self.section_checks.values():
            check.setEnabled(partial)

    def load(self, s: InstallerSettings) -> None:
        {"guided": self.guided_radio, "partial": self.partial_radio,
         "automatic": self.automatic_radio}.get(s.mode, self.guided_radio).setChecked(True)  # fmt: skip
        for key, check in self.section_checks.items():
            check.setChecked(key in s.interactive)
        self._mode_changed(0, True)
        self.refresh_check.setChecked(s.refresh_installer)
        self.locale_edit.setText(s.locale)
        self.keyboard_combo.setCurrentText(s.keyboard_layout)
        self.variant_edit.setText(s.keyboard_variant)
        self.timezone_combo.setCurrentText(s.timezone)
        self.hostname_edit.setText(s.hostname)
        self.realname_edit.setText(s.realname)
        self.username_edit.setText(s.username)
        self._password_hash = s.password_hash
        self.password_edit.clear()
        self.password2_edit.clear()
        if s.password_hash:
            self.password_edit.setPlaceholderText("unchanged (a password is set)")
        self.storage_combo.setCurrentIndex(max(0, self.storage_combo.findData(s.storage_layout)))
        self.encrypt_check.setChecked(s.storage_encrypted)
        self.ssh_check.setChecked(s.ssh_server)
        self.ssh_pw_check.setChecked(s.ssh_allow_password)
        self.ssh_pw_check.setEnabled(s.ssh_server)
        self.ssh_keys_edit.setPlainText("\n".join(s.ssh_keys))
        self.updates_combo.setCurrentIndex(max(0, self.updates_combo.findData(s.updates)))
        self.drivers_check.setChecked(s.drivers)
        self.codecs_check.setChecked(s.codecs)
        self.packages_edit.setText(" ".join(s.packages))
        self.snaps_edit.setText(", ".join(s.snaps))
        self.commands_edit.setPlainText("\n".join(s.late_commands))
        self.shutdown_combo.setCurrentIndex(max(0, self.shutdown_combo.findData(s.shutdown)))
        self.extra_edit.setPlainText(s.extra_yaml)

    def settings(self) -> InstallerSettings:
        """Collect the form; hashes a newly typed password."""
        password = self.password_edit.text()
        if password or self.password2_edit.text():
            if password != self.password2_edit.text():
                raise autoinstall.AutoinstallError("The two passwords differ.")
            self._password_hash = autoinstall.hash_password(password)
            self.password_edit.clear()
            self.password2_edit.clear()
            self.password_edit.setPlaceholderText("unchanged (a password is set)")
        mode = ("guided", "partial", "automatic")[max(0, self.mode_group.checkedId())]
        return InstallerSettings(
            mode=mode,
            interactive=[key for key, check in self.section_checks.items() if check.isChecked()],
            locale=self.locale_edit.text().strip(),
            keyboard_layout=self.keyboard_combo.currentText().strip(),
            keyboard_variant=self.variant_edit.text().strip(),
            timezone=self.timezone_combo.currentText().strip(),
            hostname=self.hostname_edit.text().strip(),
            realname=self.realname_edit.text().strip(),
            username=self.username_edit.text().strip(),
            password_hash=self._password_hash,
            storage_layout=self.storage_combo.currentData(),
            storage_encrypted=self.encrypt_check.isChecked(),
            ssh_server=self.ssh_check.isChecked(),
            ssh_allow_password=self.ssh_pw_check.isChecked(),
            ssh_keys=[line.strip() for line in self.ssh_keys_edit.toPlainText().splitlines() if line.strip()],
            updates=self.updates_combo.currentData(),
            drivers=self.drivers_check.isChecked(),
            codecs=self.codecs_check.isChecked(),
            packages=self.packages_edit.text().split(),
            snaps=[part.strip() for part in self.snaps_edit.text().split(",") if part.strip()],
            late_commands=[line for line in self.commands_edit.toPlainText().splitlines() if line.strip()],
            refresh_installer=self.refresh_check.isChecked(),
            shutdown=self.shutdown_combo.currentData(),
            extra_yaml=self.extra_edit.toPlainText(),
        )

    def is_enabled(self) -> bool:
        return self.enabled_check.isChecked()

    def yaml_text(self) -> str:
        return autoinstall.render(self.settings())

    # -- actions --------------------------------------------------------------------------

    def _preview(self) -> None:
        try:
            text = self.yaml_text()
        except autoinstall.AutoinstallError as exc:
            QMessageBox.warning(self, "Installer", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("autoinstall.yaml")
        dialog.resize(640, 560)
        view = _mono(QPlainTextEdit(text))
        view.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(view)
        layout.addWidget(buttons)
        dialog.exec()

    def _check(self) -> None:
        try:
            document = autoinstall.build_document(self.settings())
            problems = autoinstall.check_with_official_schema(document)
        except autoinstall.AutoinstallError as exc:
            QMessageBox.warning(self, "Installer", str(exc))
            return
        if problems:
            QMessageBox.warning(self, "Installer", "Subiquity's schema reports:\n\n" + "\n".join(problems))
        else:
            QMessageBox.information(self, "Installer", "Valid: the document matches Subiquity's autoinstall schema.")

    def _load(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Load autoinstall.yaml", str(Path.home()), "YAML (*.yaml *.yml)")
        if not chosen:
            return
        try:
            document = autoinstall.parse(Path(chosen).read_text(encoding="utf-8"))
        except (OSError, autoinstall.AutoinstallError) as exc:
            QMessageBox.warning(self, "Installer", str(exc))
            return
        self.enabled_check.setChecked(True)
        known = {"version", "interactive-sections", "locale", "keyboard", "timezone", "identity", "storage", "ssh",
                 "updates", "drivers", "codecs", "packages", "snaps", "late-commands", "shutdown",
                 "refresh-installer"}  # fmt: skip
        identity, keyboard, ssh = (document.get(k) or {} for k in ("identity", "keyboard", "ssh"))
        sections = document.get("interactive-sections") or []
        settings = InstallerSettings(
            mode="guided" if "*" in sections else ("partial" if sections else "automatic"),
            interactive=[s for s in sections if s != "*"],
            locale=str(document.get("locale", "en_US.UTF-8")),
            keyboard_layout=str(keyboard.get("layout", "us")), keyboard_variant=str(keyboard.get("variant", "")),
            timezone=str(document.get("timezone", "Etc/UTC")),
            hostname=str(identity.get("hostname", "")), realname=str(identity.get("realname", "")),
            username=str(identity.get("username", "")), password_hash=str(identity.get("password", "")),
            storage_layout=str(((document.get("storage") or {}).get("layout") or {}).get("name", "lvm")),
            ssh_server=bool(ssh.get("install-server", False)), ssh_allow_password=bool(ssh.get("allow-pw", True)),
            ssh_keys=list(ssh.get("authorized-keys", [])),
            updates=str(document.get("updates", "security")),
            drivers=bool((document.get("drivers") or {}).get("install", True)),
            codecs=bool((document.get("codecs") or {}).get("install", True)),
            packages=[str(p) for p in document.get("packages", [])],
            snaps=[s["name"] + (" --classic" if s.get("classic") else "") for s in document.get("snaps", [])],
            late_commands=[c if isinstance(c, str) else " ".join(c) for c in document.get("late-commands", [])],
            refresh_installer=bool((document.get("refresh-installer") or {}).get("update", False)),
            shutdown=str(document.get("shutdown", "reboot")),
            extra_yaml=yaml.safe_dump({k: v for k, v in document.items() if k not in known}, sort_keys=False)
            if set(document) - known else "",
        )  # fmt: skip
        self.load(settings)

    def _save(self) -> None:
        try:
            text = self.yaml_text()
        except autoinstall.AutoinstallError as exc:
            QMessageBox.warning(self, "Installer", str(exc))
            return
        chosen, _ = QFileDialog.getSaveFileName(self, "Save autoinstall.yaml", str(Path.home() / "autoinstall.yaml"),
                                                "YAML (*.yaml *.yml)")  # fmt: skip
        if chosen:
            Path(chosen).write_text(text, encoding="utf-8")

