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

from ..core.devicecheck import DeviceCheck, DeviceCheckService
from ..core.events import EventSink, Level, LogEvent, State, StateEvent
from ..core.requests import default_port, is_ftp
from ..core.vendors import UnknownVendorError, get_vendor
from ..core.verify import STATUS_VERIFIED, VerifyOptions, check_verify_template
from .common import UiContext, wire_runner
from .push_options import PushOptionsPanel, ask_confirmation
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
        checker_factory: Callable[[EventSink], DeviceCheckService],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._ctx, self._services, self._tester_factory, self._hash_factory = (
            ctx,
            services,
            tester_factory,
            hash_factory,
        )
        self._checker_factory = checker_factory
        self._service = None
        self._check_service: Optional[DeviceCheckService] = None
        self._runner, self._aux, self._checker = JobRunner(self), JobRunner(self), JobRunner(self)

        self._file = FilePicker(with_hash=True)
        self._host = QLineEdit()
        self._host.setPlaceholderText("Device IP or hostname, e.g. 192.168.1.1")
        self._test = QPushButton("Test connection")
        self._test.setToolTip("Check reachability, banner and host key - no login, nothing is sent")
        self._options = PushOptionsPanel()
        self._action = QPushButton("Push file")
        self._check = QPushButton("Check device")
        self._check.setToolTip(
            "Connect over SSH and check the device without sending anything: compare the file's digest with the copy "
            "on the device, and/or confirm the software version it reports (for platforms that pull their own image)."
        )
        self._expect = QLineEdit()
        self._expect.setPlaceholderText("optional - the software version the device must report, e.g. 7.2.4")
        self._status = QLabel("Ready")
        self._status.setWordWrap(True)
        self._panel = TransferPanel()

        host_row = QHBoxLayout()
        host_row.addWidget(self._host, 1)
        host_row.addWidget(self._test)
        form = QFormLayout()
        form.addRow("File to push", self._file)
        form.addRow("Device", host_row)
        form.addRow("Expect version", self._expect)
        run_row = QHBoxLayout()
        run_row.addWidget(self._action)
        run_row.addWidget(self._check)
        run_row.addWidget(self._status, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._options)
        layout.addLayout(run_row)
        layout.addWidget(self._panel, 1)

        wire_runner(self._runner, self._panel, ctx, self.SOURCE)
        wire_runner(self._aux, self._panel, ctx, self.SOURCE)
        wire_runner(self._checker, self._panel, ctx, self.SOURCE)
        self._checker.state.connect(self._on_check_state)
        self._checker.idle.connect(self._on_check_idle)
        self._runner.state.connect(self._on_state)
        self._runner.idle.connect(self._on_idle)
        self._aux.idle.connect(self._on_aux_idle)
        self._action.clicked.connect(self._on_action)
        self._check.clicked.connect(self._on_check)
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
        host = self._host.text().strip()
        text = self._options.confirmation_text(host)
        if text and not ask_confirmation(self, "Confirm changes to the device", text):
            return
        defaults = self._options.defaults(confirmed=bool(text))
        request = defaults.build(host, self._file.path())
        service = self._services(defaults.protocol)(self._runner.emit_event)
        self._service = service
        self._panel.reset_progress()
        self._set_inputs_enabled(False)
        self._check.setEnabled(False)
        self._action.setText("Cancel")
        self._status.setText("Connecting...")
        self._runner.start(lambda: service.push(request))

    @Slot(object)
    def _on_state(self, ev: StateEvent) -> None:
        text = {
            State.STARTED: "Connected - transferring...",
            State.FINISHED: f"Done - {STATUS_VERIFIED}" if ev.detail == STATUS_VERIFIED else "Done",
            State.STOPPED: "Cancelled",
            State.FAILED: ev.detail if ev.detail.startswith("FAILED (") else f"Failed: {ev.detail}",
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
        self._check.setEnabled(True)
        self._action.setText("Push file")

    # -- check device (no transfer) ---------------------------------------------------
    def _check_problem(self) -> str:
        """Why 'Check device' cannot run (empty = fine)."""
        d = self._options.defaults()
        if is_ftp(d.protocol):
            return "Check device runs commands over SSH: choose SCP or SFTP as the protocol."
        if not self._host.text().strip():
            return "Enter the device IP or hostname."
        if not d.username:
            return "Enter the username."
        if not (d.password or d.key_path):
            return "Enter a password or choose an SSH key."
        try:
            vendor = get_vendor(self._options.vendor_id())
        except UnknownVendorError:
            return "Choose a vendor under 'Device checks (SSH)' so NetTransfer knows which commands to run."
        custom = self._options.verify_command()
        bad = check_verify_template(custom) if custom else None
        if bad:
            return bad[:1].upper() + bad[1:]
        expect, has_file = self._expect.text().strip(), os.path.isfile(self._file.path())
        if expect and not vendor.version_cmd:
            return f"No version command is known for {vendor.label}."
        if not has_file and not expect:
            return "Choose a file to compare and/or enter the version the device must report."
        if has_file and not self._can_hash(vendor) and not expect:
            return (
                f"{vendor.label} has no built-in file-hash command: enter a custom verify command, or the version "
                "the device must report."
            )
        return ""

    def _can_hash(self, vendor) -> bool:
        return bool(self._options.verify_command() or vendor.verify_cmd or vendor.verify_steps)

    def _on_check(self) -> None:
        if self._checker.running:
            self._check.setEnabled(False)
            self._check.setText("Cancelling...")
            if self._check_service is not None:
                self._check_service.cancel()
            return
        problem = self._check_problem()
        if problem:
            QMessageBox.warning(self, "Missing information", problem)
            return
        d, vendor = self._options.defaults(), get_vendor(self._options.vendor_id())
        use_file = os.path.isfile(self._file.path()) and self._can_hash(vendor)
        if os.path.isfile(self._file.path()) and not use_file:
            self._panel.on_log(
                LogEvent(
                    Level.INFO,
                    f"The file is not compared: {vendor.label} has no file-hash command; checking the version only",
                )
            )
        request = DeviceCheck(
            self._host.text().strip(),
            d.username,
            vendor.id,
            d.password,
            d.port or default_port(d.protocol),
            d.key_path,
            d.key_passphrase,
            d.replace_host_key,
            file=self._file.path() if use_file else "",
            remote_path=d.remote_path,
            verify=VerifyOptions(command=self._options.verify_command()),
            expect_version=self._expect.text().strip(),
        )
        service = self._checker_factory(self._checker.emit_event)
        self._check_service = service
        self._panel.reset_progress()
        self._set_inputs_enabled(False)
        self._action.setEnabled(False)
        self._check.setText("Cancel check")
        self._status.setText("Checking the device...")
        self._checker.start(lambda: service.run(request))

    @Slot(object)
    def _on_check_state(self, ev: StateEvent) -> None:
        if ev.state is State.FINISHED:
            self._status.setText("Check passed")
        elif ev.state is State.STOPPED:
            self._status.setText("Check cancelled")
        elif ev.state is State.FAILED:
            specific = ev.detail and ev.detail != "verification failed"
            self._status.setText(f"Check failed: {ev.detail}" if specific else "Check failed - see the log")
            self._ctx.notify(f"{self.SOURCE}: device check failed", ev.detail)

    @Slot()
    def _on_check_idle(self) -> None:
        self._check_service = None
        self._set_inputs_enabled(True)
        self._action.setEnabled(True)
        self._check.setEnabled(True)
        self._check.setText("Check device")

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
        if self._checker.running:
            if self._check_service is not None:
                self._check_service.cancel()
            self._checker.wait()
        self._aux.wait()
