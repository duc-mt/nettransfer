"""[Push] tab: send a file to ONE device over SCP / SFTP / FTP / FTPS."""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.events import EventSink, State, StateEvent
from ..core.requests import is_ftp
from .common import UiContext, wire_runner
from .push_options import PushOptionsPanel
from .widgets import FilePicker, TransferPanel
from .workers import JobRunner


class PushTab(QWidget):
    SOURCE, PREFIX = "PUSH", "push"

    def __init__(
        self,
        *,
        ctx: UiContext,
        services: Callable[[str], Callable[[EventSink], object]],
        tester_factory: Callable[[EventSink], object],
        hash_factory: Callable[[EventSink], object],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._ctx, self._services, self._tester_factory, self._hash_factory = (
            ctx,
            services,
            tester_factory,
            hash_factory,
        )
        self._service = None
        self._runner, self._aux = JobRunner(self), JobRunner(self)

        self._file = FilePicker(with_hash=True)
        self._host = QLineEdit()
        self._host.setPlaceholderText("Device IP or hostname, e.g. 192.168.1.1")
        self._test = QPushButton("Test connection")
        self._test.setToolTip("Check reachability, banner and host key - no login, nothing is sent")
        self._options = PushOptionsPanel()
        self._action = QPushButton("Push file")
        self._status = QLabel("Ready")
        self._status.setWordWrap(True)
        self._panel = TransferPanel()

        host_row = QHBoxLayout()
        host_row.addWidget(self._host, 1)
        host_row.addWidget(self._test)
        form = QFormLayout()
        form.addRow("File to push", self._file)
        form.addRow("Device", host_row)
        run_row = QHBoxLayout()
        run_row.addWidget(self._action)
        run_row.addWidget(self._status, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._options)
        layout.addLayout(run_row)
        layout.addWidget(self._panel, 1)

        wire_runner(self._runner, self._panel, ctx, self.SOURCE)
        wire_runner(self._aux, self._panel, ctx, self.SOURCE)
        self._runner.state.connect(self._on_state)
        self._runner.idle.connect(self._on_idle)
        self._aux.idle.connect(self._on_aux_idle)
        self._action.clicked.connect(self._on_action)
        self._test.clicked.connect(self._on_test)
        self._file.hash_requested.connect(self._on_hash)
        self._restore()

    # -- aux jobs (hash / connection test) -------------------------------------
    def _on_hash(self) -> None:
        path = self._file.path()
        if self._aux.running or not os.path.isfile(path):
            return
        self._busy_aux(True)
        service = self._hash_factory(self._aux.emit_event)
        self._aux.start(lambda: service.run(path))

    def _on_test(self) -> None:
        host = self._host.text().strip()
        if self._aux.running or not host:
            if not host:
                QMessageBox.warning(self, "Missing information", "Enter the device IP or hostname.")
            return
        self._busy_aux(True)
        tester = self._tester_factory(self._aux.emit_event)
        protocol, port = self._options.protocol(), self._options.port()
        self._aux.start(lambda: tester.run(host, port, protocol))

    def _busy_aux(self, busy: bool) -> None:
        self._test.setEnabled(not busy)
        self._file.set_hash_enabled(not busy)

    @Slot()
    def _on_aux_idle(self) -> None:
        self._busy_aux(False)

    # -- push ---------------------------------------------------------------------
    def _validate(self) -> str:
        defaults = self._options.defaults()
        if not os.path.isfile(self._file.path()):
            return "Choose an existing file to push."
        if not self._host.text().strip():
            return "Enter the device IP or hostname."
        if not is_ftp(defaults.protocol) and not defaults.username:
            return "Enter the username."
        if not is_ftp(defaults.protocol) and not (defaults.password or defaults.key_path):
            return "Enter a password or choose an SSH key."
        return self._options.problem()

    def _on_action(self) -> None:
        if self._runner.running:
            self._action.setEnabled(False)
            self._action.setText("Cancelling...")
            if self._service is not None:
                self._service.cancel()
            return
        problem = self._validate()
        if problem:
            QMessageBox.warning(self, "Missing information", problem)
            return
        defaults = self._options.defaults()
        request = defaults.build(self._host.text().strip(), self._file.path())
        service = self._services(defaults.protocol)(self._runner.emit_event)
        self._service = service
        self._panel.reset_progress()
        self._set_inputs_enabled(False)
        self._action.setText("Cancel")
        self._status.setText("Connecting...")
        self._runner.start(lambda: service.push(request))

    @Slot(object)
    def _on_state(self, ev: StateEvent) -> None:
        text = {
            State.STARTED: "Connected - transferring...",
            State.FINISHED: "Done",
            State.STOPPED: "Cancelled",
            State.FAILED: f"Failed: {ev.detail}",
        }.get(ev.state)
        if text:
            self._status.setText(text)
        if ev.state is State.FAILED:
            self._ctx.notify(f"{self.SOURCE}: push failed", ev.detail)

    @Slot()
    def _on_idle(self) -> None:
        self._service = None
        self._set_inputs_enabled(True)
        self._action.setEnabled(True)
        self._action.setText("Push file")

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (self._file, self._host, self._options):
            widget.setEnabled(enabled)

    # -- settings / lifecycle -------------------------------------------------------
    def _restore(self) -> None:
        s = self._ctx.settings
        self._file.set_path(str(s.get(f"{self.PREFIX}.file", "")))
        self._host.setText(str(s.get(f"{self.PREFIX}.host", "")))
        self._options.load(s, self.PREFIX)

    def save_settings(self) -> None:
        s = self._ctx.settings
        s.set(f"{self.PREFIX}.file", self._file.path())
        s.set(f"{self.PREFIX}.host", self._host.text().strip())
        self._options.save(s, self.PREFIX)

    def shutdown(self) -> None:
        if self._runner.running:
            if self._service is not None:
                self._service.cancel()
            self._runner.wait()
        self._aux.wait()
