"""Credentials/protocol options shared by the single-device Push tab and the Batch tab."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.device_hints import REMOTE_PATH_PRESETS
from ..core.requests import PROTOCOLS, PushDefaults, default_port, is_ftp
from ..core.settings import SettingsStore

_LABELS = {"scp": "SCP", "sftp": "SFTP", "ftp": "FTP", "ftps": "FTPS (explicit TLS)"}


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

        self._advanced = QGroupBox("Advanced options")
        self._advanced.setCheckable(True)
        self._advanced.setChecked(False)
        body = QWidget()
        adv = QFormLayout(body)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.addRow("Port", self._port)
        remote_row = QHBoxLayout()
        remote_row.addWidget(self._remote, 1)
        remote_row.addWidget(self._preset)
        adv.addRow("Remote path", remote_row)
        adv.addRow("", self._replace)
        body.setVisible(False)
        adv_layout = QVBoxLayout(self._advanced)
        adv_layout.addWidget(body)

        def toggle(on: bool) -> None:  # collapsed: no empty frame under the title
            body.setVisible(on)
            adv_layout.setContentsMargins(*((9, 9, 9, 9) if on else (0, 0, 0, 0)))

        toggle(False)
        self._advanced.toggled.connect(toggle)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._form)
        outer.addWidget(self._advanced)

        self._protocol.currentIndexChanged.connect(self._on_protocol)
        self._preset.activated.connect(self._apply_preset)
        self._key_browse.clicked.connect(self._browse_key)
        for edit in (self._user, self._key, self._remote):
            edit.textChanged.connect(self.changed)
        self._port.valueChanged.connect(self.changed)
        self._on_protocol()

    # -- values -----------------------------------------------------------
    def protocol(self) -> str:
        return self._protocol.currentData()

    def port(self) -> int:
        return self._port.value()

    def defaults(self) -> PushDefaults:
        return PushDefaults(
            self.protocol(),
            self._user.text().strip(),
            self._password.text(),
            self._key.text().strip(),
            self._passphrase.text(),
            self._port.value(),
            self._remote.text().strip(),
            self._replace.isChecked() and not is_ftp(self.protocol()),
        )

    def set_enabled(self, enabled: bool) -> None:
        for w in (self._protocol, self._user, self._password, self._key_row, self._passphrase, self._advanced):
            w.setEnabled(enabled)

    # -- settings: never passwords or passphrases ------------------------
    def load(self, store: SettingsStore, prefix: str) -> None:
        index = self._protocol.findData(store.get(f"{prefix}.protocol", "scp"))
        self._protocol.setCurrentIndex(max(index, 0))
        self._user.setText(str(store.get(f"{prefix}.user", "")))
        self._key.setText(str(store.get(f"{prefix}.key", "")))
        self._remote.setText(str(store.get(f"{prefix}.remote", "")))
        self._port.setValue(int(store.get(f"{prefix}.port", default_port(self.protocol()))))

    def save(self, store: SettingsStore, prefix: str) -> None:
        store.set(f"{prefix}.protocol", self.protocol())
        store.set(f"{prefix}.user", self._user.text().strip())
        store.set(f"{prefix}.key", self._key.text().strip())
        store.set(f"{prefix}.remote", self._remote.text().strip())
        store.set(f"{prefix}.port", self._port.value())

    # -- internals ---------------------------------------------------------
    def _on_protocol(self) -> None:
        proto = self.protocol()
        # follow the protocol's default port unless the user typed a custom one
        if self._port.value() in {default_port(p) for p in PROTOCOLS}:
            self._port.setValue(default_port(proto))
        ssh = not is_ftp(proto)
        for widget in (self._key_row, self._passphrase, self._replace, self._preset):
            widget.setVisible(ssh)
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
