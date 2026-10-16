"""Credentials/protocol options shared by the single-device Push tab and the Batch tab."""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.device_hints import REMOTE_PATH_PRESETS
from ..core.install import MAX_RELOAD_MINUTES, InstallOptions, describe
from ..core.requests import PROTOCOLS, PushDefaults, default_port, is_ftp
from ..core.settings import SettingsStore
from ..core.vendors import VENDORS, get_vendor
from ..core.verify import VerifyOptions, check_verify_template

_LABELS = {"scp": "SCP", "sftp": "SFTP", "ftp": "FTP", "ftps": "FTPS (explicit TLS)"}


def _collapsible(title: str) -> Tuple[QGroupBox, QFormLayout]:
    """A checkable group box that is just a title line until opened (no empty frame under it)."""
    group = QGroupBox(title)
    group.setCheckable(True)
    group.setChecked(False)
    body = QWidget()
    form = QFormLayout(body)
    form.setContentsMargins(0, 0, 0, 0)
    layout = QVBoxLayout(group)
    layout.addWidget(body)

    def toggle(on: bool) -> None:
        body.setVisible(on)
        layout.setContentsMargins(*((9, 9, 9, 9) if on else (0, 0, 0, 0)))

    toggle(False)
    group.toggled.connect(toggle)
    return group, form


def ask_confirmation(parent: QWidget, title: str, text: str) -> bool:
    """Modal yes/no (default No) before an intrusive action."""
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


class PushOptionsPanel(QWidget):
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None, user_label: str = "Username") -> None:
        super().__init__(parent)
        self._protocol = QComboBox()
        for proto in PROTOCOLS:
            self._protocol.addItem(_LABELS[proto], proto)
        self._user = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._key = QLineEdit()
        self._key.setPlaceholderText("optional private key (OpenSSH/PEM) instead of / before the password")
        self._key_browse = QPushButton("Browse...")
        self._passphrase = QLineEdit()
        self._passphrase.setEchoMode(QLineEdit.Password)
        self._passphrase.setPlaceholderText("key passphrase, if the key is encrypted")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(default_port("scp"))
        self._remote = QLineEdit()
        self._remote.setPlaceholderText("optional, e.g. flash:  or  /var/tmp/   (blank = device default)")
        self._preset = QComboBox()
        self._preset.addItem("Preset...", None)
        for name, value in REMOTE_PATH_PRESETS.items():
            self._preset.addItem(name, value)
        self._replace = QCheckBox("Accept changed host key (replace the stored one)")
        self._resume = QCheckBox("Resume a partial upload (SFTP/FTP) instead of starting over")
        self._resume.setToolTip(
            "Continues a shorter file already on the device.\n"
            "Combine with Verify: the digest proves the result is right."
        )
        self._vendor = QComboBox()
        self._vendor.addItem("None - plain copy", "")
        for vendor in VENDORS.values():
            self._vendor.addItem(vendor.label, vendor.id)
        self._vendor.setToolTip(
            "Tells NetTransfer which commands the device understands (verify, free space) and where a blank remote\n"
            "path lands. These are templates - check them against your devices before relying on them."
        )
        self._preflight = QCheckBox("Pre-flight: check the image name and free space before sending")
        self._preflight.setToolTip("Stops before sending if the device reports too little free space")
        self._verify = QCheckBox("Verify afterwards: compare the device's digest with this file's")
        self._verify.setToolTip("Runs the device's checksum command over SSH; a mismatch fails the push")
        self._verify_cmd = QLineEdit()
        self._verify_cmd.setPlaceholderText("optional - overrides the vendor's, e.g. sha256sum {path}")
        self._boot = QCheckBox("After a verified push, set the boot image to this file")
        self._boot.setToolTip(
            "Changes the device's boot variable (e.g. 'boot system flash:file'). Needs Verify afterwards."
        )
        self._reload_mode = QComboBox()
        self._reload_mode.addItem("Don't reload", "none")
        self._reload_mode.addItem("Schedule a reload in...", "scheduled")
        self._reload_mode.addItem("Reload immediately", "now")
        self._reload_minutes = QSpinBox()
        self._reload_minutes.setRange(1, MAX_RELOAD_MINUTES)
        self._reload_minutes.setValue(5)
        self._reload_minutes.setSuffix(" min")
        self._reload_minutes.setEnabled(False)
        self._dry_run = QCheckBox("Dry run: show the commands, change nothing")
        self._dry_run.setChecked(True)
        self._dry_run.setToolTip(
            "Untick to really apply the boot image / reload. You are asked to confirm first.\n"
            "These choices are not remembered between sessions: every session starts as a dry run."
        )

        self._key_row = QWidget()
        key_layout = QHBoxLayout(self._key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self._key, 1)
        key_layout.addWidget(self._key_browse)

        self._form = QFormLayout()
        self._form.addRow("Protocol", self._protocol)
        self._form.addRow(user_label, self._user)
        self._form.addRow("Password", self._password)
        self._form.addRow("SSH key", self._key_row)
        self._form.addRow("Key passphrase", self._passphrase)

        self._advanced, adv = _collapsible("Advanced options")
        adv.addRow("Port", self._port)
        remote_row = QHBoxLayout()
        remote_row.addWidget(self._remote, 1)
        remote_row.addWidget(self._preset)
        adv.addRow("Remote path", remote_row)
        adv.addRow("", self._replace)
        adv.addRow("", self._resume)

        self._checks, checks = _collapsible("Device checks (SSH)")
        checks.addRow("Vendor", self._vendor)
        checks.addRow("", self._preflight)
        checks.addRow("", self._verify)
        checks.addRow("Verify command", self._verify_cmd)
        reload_row = QHBoxLayout()
        reload_row.addWidget(self._reload_mode)
        reload_row.addWidget(self._reload_minutes)
        reload_row.addStretch(1)
        checks.addRow("After push", self._boot)
        checks.addRow("Reload", reload_row)
        checks.addRow("", self._dry_run)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._form)
        outer.addWidget(self._advanced)
        outer.addWidget(self._checks)

        self._protocol.currentIndexChanged.connect(self._on_protocol)
        self._preset.activated.connect(self._apply_preset)
        self._key_browse.clicked.connect(self._browse_key)
        for edit in (self._user, self._key, self._remote):
            edit.textChanged.connect(self.changed)
        self._port.valueChanged.connect(self.changed)
        self._verify_cmd.textChanged.connect(self.changed)
        self._vendor.currentIndexChanged.connect(self.changed)
        for box in (self._preflight, self._verify, self._resume, self._boot, self._dry_run):
            box.toggled.connect(self.changed)
        self._reload_mode.currentIndexChanged.connect(self._on_reload_mode)
        self._reload_minutes.valueChanged.connect(self.changed)
        self.changed.connect(self._refresh_checks_title)
        self._on_protocol()

    # -- values -----------------------------------------------------------
    def protocol(self) -> str:
        return self._protocol.currentData()

    def port(self) -> int:
        return self._port.value()

    def defaults(self, confirmed: bool = False) -> PushDefaults:
        """The chosen options. ``confirmed`` is set by the tab only after the operator agreed to the confirmation
        dialog; without it the core refuses to apply a boot image / reload change."""
        proto = self.protocol()
        ssh = not is_ftp(proto)  # the device checks run a command over SSH: meaningless for FTP/FTPS
        verify = VerifyOptions(command=self._verify_cmd.text().strip()) if ssh and self._verify.isChecked() else None
        return PushDefaults(
            proto,
            self._user.text().strip(),
            self._password.text(),
            self._key.text().strip(),
            self._passphrase.text(),
            self._port.value(),
            self._remote.text().strip(),
            self._replace.isChecked() and ssh,
            vendor=self._vendor.currentData() if ssh else "",
            preflight=ssh and self._preflight.isChecked(),
            verify=verify,
            install=self._install_options(confirmed) if ssh else None,
            resume=self._resume.isChecked() and proto != "scp",  # the SCP protocol cannot continue a partial upload
        )

    def vendor_id(self) -> str:
        """The chosen platform profile id ('' = none)."""
        return str(self._vendor.currentData() or "")

    def verify_command(self) -> str:
        """The custom verify command, if one was typed (used by Check device even when 'Verify afterwards' is off)."""
        return self._verify_cmd.text().strip()

    def _install_options(self, confirmed: bool) -> Optional[InstallOptions]:
        mode = self._reload_mode.currentData()
        if not (self._boot.isChecked() or mode != "none"):
            return None
        return InstallOptions(
            execute=not self._dry_run.isChecked(),
            reload=mode == "now",
            set_boot=self._boot.isChecked(),
            reload_in=self._reload_minutes.value() if mode == "scheduled" else None,
            confirmed=confirmed,
        )

    def problem(self, vendors_from_targets: bool = False) -> str:
        """Why the chosen device checks cannot run (empty = fine). ``vendors_from_targets``: every batch device
        brings its own vendor (from the inventory), so a missing default vendor is not an error."""
        if is_ftp(self.protocol()):
            return ""
        return self._verify_problem(vendors_from_targets) or self._install_problem(vendors_from_targets)

    def _verify_problem(self, vendors_from_targets: bool) -> str:
        if not self._verify.isChecked():
            return ""
        custom = self._verify_cmd.text().strip()
        if custom:
            reason = check_verify_template(custom) or ""
            return reason[:1].upper() + reason[1:]
        vendor_id = self._vendor.currentData()
        if not vendor_id:
            return (
                ""
                if vendors_from_targets
                else "Verify needs a vendor (for its checksum command) or a custom verify command."
            )
        vendor = get_vendor(vendor_id)
        if vendor.verify_cmd is None and not vendor.verify_steps and not vendors_from_targets:
            return f"{vendor.label} has no built-in verify command: enter a custom one."
        return ""

    def _install_problem(self, vendors_from_targets: bool) -> str:
        install = self._install_options(False)
        if install is None:
            return ""
        vendor_id = self._vendor.currentData()
        if install.execute and not self._verify.isChecked():
            return "Applying a boot image or reload needs 'Verify afterwards' (a corrupt file must never be booted)."
        if not vendor_id:
            return "" if vendors_from_targets else "Choose a vendor to set the boot image or reload."
        vendor = get_vendor(vendor_id)
        if install.set_boot and not vendor.install:
            return f"{vendor.label} has no boot-image commands."
        if install.reload_in is not None and not vendor.reload_in:
            return f"{vendor.label} has no scheduled reload: choose 'Reload immediately' or no reload."
        if install.reload and not vendor.reload:
            return f"{vendor.label} has no reload command."
        return ""

    def confirmation_text(self, subject: str) -> str:
        """What to ask the operator before applying changes ('' = nothing intrusive is about to happen)."""
        install = self._install_options(False)
        if install is None or not install.execute:
            return ""
        return (
            f"This will change {subject}:\n\n  - {describe(install)}\n\n"
            "The file is verified first and nothing is changed if verification fails. Continue?"
        )

    def set_enabled(self, enabled: bool) -> None:
        for w in (
            self._protocol,
            self._user,
            self._password,
            self._key_row,
            self._passphrase,
            self._advanced,
            self._checks,
        ):
            w.setEnabled(enabled)

    # -- settings: never passwords or passphrases ------------------------
    def load(self, store: SettingsStore, prefix: str) -> None:
        index = self._protocol.findData(store.get(f"{prefix}.protocol", "scp"))
        self._protocol.setCurrentIndex(max(index, 0))
        self._user.setText(str(store.get(f"{prefix}.user", "")))
        self._key.setText(str(store.get(f"{prefix}.key", "")))
        self._remote.setText(str(store.get(f"{prefix}.remote", "")))
        self._port.setValue(int(store.get(f"{prefix}.port", default_port(self.protocol()))))
        self._vendor.setCurrentIndex(max(self._vendor.findData(str(store.get(f"{prefix}.vendor", ""))), 0))
        self._preflight.setChecked(bool(store.get(f"{prefix}.preflight", False)))
        self._verify.setChecked(bool(store.get(f"{prefix}.verify", False)))
        self._verify_cmd.setText(str(store.get(f"{prefix}.verify_cmd", "")))
        self._resume.setChecked(bool(store.get(f"{prefix}.resume", False)))
        self._checks.setChecked(self._checks_in_use())  # open the group when it holds a saved choice

    def save(self, store: SettingsStore, prefix: str) -> None:
        store.set(f"{prefix}.protocol", self.protocol())
        store.set(f"{prefix}.user", self._user.text().strip())
        store.set(f"{prefix}.key", self._key.text().strip())
        store.set(f"{prefix}.remote", self._remote.text().strip())
        store.set(f"{prefix}.port", self._port.value())
        store.set(f"{prefix}.vendor", self._vendor.currentData())
        store.set(f"{prefix}.preflight", self._preflight.isChecked())
        store.set(f"{prefix}.verify", self._verify.isChecked())
        store.set(f"{prefix}.verify_cmd", self._verify_cmd.text().strip())
        store.set(f"{prefix}.resume", self._resume.isChecked())

    # -- internals ---------------------------------------------------------
    def _checks_in_use(self) -> bool:
        return bool(self._vendor.currentData() or self._preflight.isChecked() or self._verify.isChecked())

    def _refresh_checks_title(self) -> None:
        """A collapsed group must not hide that something (especially a boot/reload hook) is switched on."""
        active = self._checks_in_use() or self._install_options(False) is not None
        self._checks.setTitle("Device checks (SSH) - in use" if active else "Device checks (SSH)")

    def _on_reload_mode(self) -> None:
        self._reload_minutes.setEnabled(self._reload_mode.currentData() == "scheduled")
        self.changed.emit()

    def _on_protocol(self) -> None:
        proto = self.protocol()
        # follow the protocol's default port unless the user typed a custom one
        if self._port.value() in {default_port(p) for p in PROTOCOLS}:
            self._port.setValue(default_port(proto))
        ssh = not is_ftp(proto)
        for widget in (self._key_row, self._passphrase, self._replace, self._preset, self._checks):
            widget.setVisible(ssh)
        self._resume.setVisible(proto != "scp")
        label = self._form.labelForField(self._key_row)
        if label is not None:
            label.setVisible(ssh)
        label = self._form.labelForField(self._passphrase)
        if label is not None:
            label.setVisible(ssh)
        self.changed.emit()

    def _apply_preset(self, index: int) -> None:
        value = self._preset.itemData(index)
        if value is not None:
            self._remote.setText(value)
        self._preset.setCurrentIndex(0)

    def _browse_key(self) -> None:
        start = os.path.dirname(self._key.text()) or os.path.join(os.path.expanduser("~"), ".ssh")
        chosen, _ = QFileDialog.getOpenFileName(self, "Select SSH private key", start, "All files (*)")
        if chosen:
            self._key.setText(chosen)
