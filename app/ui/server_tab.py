"""Base for the two 'device pulls the file' tabs (TFTP, HTTP/HTTPS)."""

from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.device_hints import Hint
from ..core.events import EventSink, State, StateEvent
from ..core.serve_policy import ServePolicy
from ..platform.network import Interface
from .common import UiContext, wire_runner
from .widgets import FilePicker, HintPanel, InfoBanner, SafetyPanel, TransferPanel
from .workers import JobRunner


class ServerTab(QWidget):
    """Common flow: pick file + IP, set safety limits, start/stop, watch progress.

    Subclasses provide ``_serve`` (the protocol call) and may add form rows, lockable widgets and a
    banner. Call ``_finish_init`` last in the subclass ``__init__``."""

    SOURCE = "SERVER"  # tag in the audit log and notifications
    PREFIX = "server"  # settings key prefix

    def __init__(
        self,
        *,
        ctx: UiContext,
        service_factory: Callable[[EventSink], object],
        hash_factory: Callable[[EventSink], object],
        list_interfaces: Callable[[], List[Interface]],
        hints: Sequence[Hint],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._ctx, self._factory, self._hash_factory = ctx, service_factory, hash_factory
        self._list_interfaces = list_interfaces
        self._service = None
        self._runner, self._aux = JobRunner(self), JobRunner(self)

        self._banner = InfoBanner()
        self._file = FilePicker(with_hash=True)
        self._iface = QComboBox()
        self._rescan = QPushButton("Rescan")
        self._rescan.setToolTip("Re-detect local network interfaces")
        self._safety = SafetyPanel()
        self._hints = HintPanel(hints)
        self._action = QPushButton("Start server")
        self._status = QLabel("Stopped")
        self._status.setWordWrap(True)
        self._panel = TransferPanel()

        iface_row = QHBoxLayout()
        iface_row.addWidget(self._iface, 1)
        iface_row.addWidget(self._rescan)
        self._form = QFormLayout()
        self._form.addRow("File to serve", self._file)
        self._form.addRow("Server IP", iface_row)
        self._add_form_rows(self._form)
        run_row = QHBoxLayout()
        run_row.addWidget(self._action)
        run_row.addWidget(self._status, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self._banner)
        layout.addLayout(self._form)
        layout.addWidget(self._safety)
        layout.addWidget(self._hints)
        layout.addLayout(run_row)
        layout.addWidget(self._panel, 1)

        wire_runner(self._runner, self._panel, ctx, self.SOURCE)
        wire_runner(self._aux, self._panel, ctx, self.SOURCE)
        self._runner.state.connect(self._on_state)
        self._runner.idle.connect(self._on_idle)
        self._aux.idle.connect(self._on_aux_idle)
        self._rescan.clicked.connect(self._reload_interfaces)
        self._iface.currentIndexChanged.connect(self._on_iface_changed)
        self._file.changed.connect(self._update_hints)
        self._file.hash_requested.connect(self._on_hash)
        self._action.clicked.connect(self._on_action)

    # -- hooks for subclasses ----------------------------------------------
    def _add_form_rows(self, form: QFormLayout) -> None:
        pass

    def _lockable(self) -> List[QWidget]:
        return []

    def _hint_values(self) -> dict:
        return {}

    def _serve(self, service: object, path: str, ip: str, policy: ServePolicy) -> None:
        raise NotImplementedError

    def _after_interfaces_changed(self) -> None:
        pass

    def _finish_init(self) -> None:
        self._restore()
        self._reload_interfaces()

    # -- interfaces / hints -------------------------------------------------
    def _reload_interfaces(self) -> None:
        previous = self._iface.currentData() or self._ctx.settings.get(f"{self.PREFIX}.ip")
        self._iface.blockSignals(True)
        self._iface.clear()
        interfaces = self._list_interfaces()
        for iface in interfaces:
            self._iface.addItem(iface.label, iface.ip)
        if not interfaces:
            self._iface.addItem("0.0.0.0  (all interfaces - no active NIC detected)", "0.0.0.0")
        self._iface.setCurrentIndex(max(self._iface.findData(previous), 0))
        self._iface.blockSignals(False)
        self._after_interfaces_changed()
        self._update_hints()

    def _on_iface_changed(self, _index: int) -> None:
        self._after_interfaces_changed()
        self._update_hints()

    def _update_hints(self, *_args) -> None:
        name = os.path.basename(self._file.path()) or "<file>"
        self._hints.update_context(ip=self._iface.currentData() or "<server-ip>", file=name, **self._hint_values())

    # -- hashing ----------------------------------------------------------------
    def _on_hash(self) -> None:
        path = self._file.path()
        if self._aux.running or not os.path.isfile(path):
            return
        self._file.set_hash_enabled(False)
        service = self._hash_factory(self._aux.emit_event)
        self._aux.start(lambda: service.run(path))

    @Slot()
    def _on_aux_idle(self) -> None:
        self._file.set_hash_enabled(True)

    # -- start / stop --------------------------------------------------------------
    def _on_action(self) -> None:
        if self._runner.running:
            self._action.setEnabled(False)
            self._action.setText("Stopping...")
            self._status.setText("Stopping (can take a few seconds)...")
            if self._service is not None:
                self._service.stop()
            return
        path, ip = self._file.path(), self._iface.currentData()
        if not os.path.isfile(path):
            QMessageBox.warning(self, "Select a file", "Choose an existing file to serve first.")
            return
        try:
            policy = self._safety.policy()
        except ValueError as exc:
            QMessageBox.warning(self, "Safety settings", str(exc))
            return
        service = self._factory(self._runner.emit_event)
        self._service = service
        self._panel.reset_progress()
        self._set_controls_enabled(False)
        self._action.setEnabled(False)
        self._action.setText("Starting...")
        self._status.setText("Starting...")
        self._runner.start(lambda: self._serve(service, path, ip, policy))

    @Slot(object)
    def _on_state(self, ev: StateEvent) -> None:
        if ev.state is State.STARTED:
            self._status.setText(f"Listening on {ev.detail}")
            self._action.setText("Stop server")
            self._action.setEnabled(True)
        elif ev.state is State.FAILED:
            self._status.setText(f"Failed: {ev.detail}")
            self._ctx.notify(f"{self.SOURCE}: server failed", ev.detail)
        elif ev.state is State.STOPPED:
            self._status.setText("Stopped")

    @Slot()
    def _on_idle(self) -> None:
        self._service = None
        self._set_controls_enabled(True)
        self._action.setEnabled(True)
        self._action.setText("Start server")
        self._after_interfaces_changed()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in [self._file, self._iface, self._rescan, self._safety, *self._lockable()]:
            widget.setEnabled(enabled)

    # -- settings / lifecycle ------------------------------------------------------
    def _restore(self) -> None:
        self._file.set_path(str(self._ctx.settings.get(f"{self.PREFIX}.file", "")))
        self._safety.load(self._ctx.settings, self.PREFIX)

    def save_settings(self) -> None:
        s = self._ctx.settings
        s.set(f"{self.PREFIX}.file", self._file.path())
        s.set(f"{self.PREFIX}.ip", self._iface.currentData())
        self._safety.save(s, self.PREFIX)

    def shutdown(self) -> None:
        if self._runner.running:
            if self._service is not None:
                self._service.stop()
            self._runner.wait()
        self._aux.wait()
