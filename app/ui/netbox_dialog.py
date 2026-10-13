"""Modal "Import from NetBox / Nautobot": URL + API token + filters -> BatchTargets for the Batch tab.

The API call runs on a worker thread (``JobRunner``), so the window never freezes. The token is held in this dialog
only: it is never saved to settings and never written to the log. While a request is in flight the dialog cannot be
dismissed (the request is bounded by its timeout), so no thread is ever left running against a closed window.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Callable, Optional

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.batch import BatchFileError
from ..core.events import State, StateEvent
from ..core.inventory import NetBoxQuery, NetBoxResult, fetch_netbox, split_values
from ..core.settings import SettingsStore
from .widgets import FilePicker
from .workers import JobRunner

Fetcher = Callable[[NetBoxQuery], NetBoxResult]
_FILTERS = (
    ("site", "Site", "NetBox site slug, or Nautobot location name"),
    ("tag", "Tag", "tag slug (Nautobot: tag name)"),
    ("role", "Role", "device role slug"),
    ("platform", "Platform", "platform slug, e.g. cisco-ios"),
    ("tenant", "Tenant", "tenant slug"),
)


class NetBoxDialog(QDialog):
    PREFIX = "netbox"

    def __init__(
        self, settings: SettingsStore, parent: Optional[QWidget] = None, fetch: Fetcher = fetch_netbox
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import devices from NetBox / Nautobot")
        self.setModal(True)
        self.result_set: Optional[NetBoxResult] = None
        self.source = ""
        self._settings, self._fetch = settings, fetch
        self._runner = JobRunner(self)
        self._runner.state.connect(self._on_state)

        self._kind = QComboBox()
        self._kind.addItem("NetBox (and Nautobot 1.x)", "netbox")
        self._kind.addItem("Nautobot 2.x", "nautobot")
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://netbox.example.com")
        self._token = QLineEdit(os.environ.get("NETBOX_TOKEN", ""))
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._token.setPlaceholderText("API token (used for this import only, never saved)")
        self._filters = {key: QLineEdit() for key, _label, _hint in _FILTERS}
        for key, _label, hint in _FILTERS:
            self._filters[key].setPlaceholderText(f"{hint} - several values: separate with commas")
        self._status = QLineEdit("active")
        self._status.setPlaceholderText("device status; leave blank for any")
        self._verify_tls = QCheckBox("Verify the server's TLS certificate")
        self._verify_tls.setChecked(True)
        self._ca = FilePicker(placeholder="optional CA bundle (PEM) for a private CA", dialog_title="Select CA bundle")
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._import = self._buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self._import.setDefault(True)

        form = QFormLayout()
        form.addRow("Server type", self._kind)
        form.addRow("URL", self._url)
        form.addRow("API token", self._token)
        for key, label, _hint in _FILTERS:
            form.addRow(label, self._filters[key])
        form.addRow("Status", self._status)
        form.addRow("", self._verify_tls)
        form.addRow("CA bundle", self._ca)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._message)
        layout.addWidget(self._buttons)
        self._buttons.accepted.connect(self._start)
        self._buttons.rejected.connect(self.reject)
        self._load()

    # -- settings: everything but the token ---------------------------------
    def _load(self) -> None:
        s, p = self._settings, self.PREFIX
        self._kind.setCurrentIndex(max(self._kind.findData(str(s.get(f"{p}.kind", "netbox"))), 0))
        self._url.setText(str(s.get(f"{p}.url", "")))
        for key in self._filters:
            self._filters[key].setText(str(s.get(f"{p}.{key}", "")))
        self._status.setText(str(s.get(f"{p}.status", "active")))
        self._verify_tls.setChecked(bool(s.get(f"{p}.verify_tls", True)))
        self._ca.set_path(str(s.get(f"{p}.ca", "")))

    def _save(self) -> None:
        s, p = self._settings, self.PREFIX
        s.set(f"{p}.kind", self._kind.currentData())
        s.set(f"{p}.url", self._url.text().strip())
        for key, edit in self._filters.items():
            s.set(f"{p}.{key}", edit.text().strip())
        s.set(f"{p}.status", self._status.text().strip())
        s.set(f"{p}.verify_tls", self._verify_tls.isChecked())
        s.set(f"{p}.ca", self._ca.path())

    # -- the import ----------------------------------------------------------
    def query(self) -> NetBoxQuery:
        values = {key: split_values(edit.text()) for key, edit in self._filters.items()}
        return NetBoxQuery(
            self._url.text().strip(),
            self._token.text().strip(),
            str(self._kind.currentData()),
            sites=values["site"],
            tags=values["tag"],
            roles=values["role"],
            platforms=values["platform"],
            tenants=values["tenant"],
            status=self._status.text().strip(),
            verify_tls=self._verify_tls.isChecked(),
            ca_bundle=self._ca.path(),
        )

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._kind,
            self._url,
            self._token,
            self._status,
            self._verify_tls,
            self._ca,
            *self._filters.values(),
        ):
            widget.setEnabled(not busy)
        self._import.setEnabled(not busy)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(not busy)

    def _start(self) -> None:
        if self._runner.running:
            return
        query = self.query()
        if not query.url:
            self._show("Enter the server URL.", error=True)
            return
        if not query.token:
            self._show("Enter an API token.", error=True)
            return
        self._save()
        self._set_busy(True)
        self._show("Contacting the server...")
        # NOTE: the query (with the token) is only captured by this closure; it is never emitted as an event
        self._runner.start(lambda: self._work(query))

    def _work(self, query: NetBoxQuery) -> None:
        """Worker thread: talk to the API, hand the outcome back through the runner's signals."""
        try:
            self.result_set = self._fetch(replace(query))
        except BatchFileError as exc:
            self._runner.emit_event(StateEvent(State.FAILED, "; ".join(exc.errors)))
            return
        self.source = query.url
        self._runner.emit_event(StateEvent(State.FINISHED, ""))

    @Slot(object)
    def _on_state(self, ev: StateEvent) -> None:
        if ev.state is State.FINISHED:
            self.accept()
        elif ev.state is State.FAILED:
            self.result_set = None
            self._set_busy(False)
            self._show(ev.detail, error=True)

    def reject(self) -> None:
        if self._runner.running:  # bounded by the request timeout; never leave a thread running against a closed window
            return
        super().reject()

    def done(self, code: int) -> None:
        if self._runner.running:
            self._runner.wait(30000)
        super().done(code)

    def _show(self, text: str, error: bool = False) -> None:
        self._message.setStyleSheet("color: #cf222e;" if error else "")
        self._message.setText(text)
