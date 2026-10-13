"""[Batch Push] tab: one file to many devices from a CSV, in parallel, with per-device status."""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import Slot
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.batch import BatchFileError, BatchRunner, BatchTarget, write_results_csv
from ..core.events import BatchEvent, EventSink, Level, LogEvent, State, StateEvent
from ..core.inventory import NetBoxQuery, NetBoxResult, fetch_netbox, filter_targets, load_inventory
from ..core.requests import is_ftp
from .common import UiContext, wire_runner
from .netbox_dialog import NetBoxDialog
from .push_options import PushOptionsPanel, ask_confirmation
from .widgets import FilePicker, TransferPanel
from .workers import JobRunner

_COLORS = {
    "done": "#3fb950",
    "failed": "#f85149",
    "cancelled": "#d29922",
    "running": "#58a6ff",
    "retrying": "#d29922",
}
_COLUMNS = ("Device", "Status", "Progress", "Detail")


class BatchTab(QWidget):
    SOURCE, PREFIX = "BATCH", "batch"

    def __init__(
        self,
        *,
        ctx: UiContext,
        services: Callable[[str], Callable[[EventSink], object]],
        hash_factory: Callable[[EventSink], object],
        netbox_fetch: Callable[[NetBoxQuery], NetBoxResult] = fetch_netbox,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._ctx, self._services, self._hash_factory = ctx, services, hash_factory
        self._netbox_fetch = netbox_fetch
        self._runner, self._aux = JobRunner(self), JobRunner(self)
        self._batch: Optional[BatchRunner] = None
        self._targets: List[BatchTarget] = []
        self._results: Dict[int, Tuple[str, str]] = {}

        self._file = FilePicker(with_hash=True)
        self._csv = FilePicker(
            placeholder="CSV (host[,username,password,remote_path,port,vendor,tags]), Ansible/Netmiko YAML or JSON, "
            "or a Zabbix hosts export",
            dialog_title="Select targets or inventory file",
            file_filter="Inventories (*.csv *.yml *.yaml *.json);;All files (*)",
        )
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("optional filter, e.g.  site:DCTT vendor:fortinet,juniper !group:lab")
        self._filter.setToolTip(
            "All terms must match; inside a term a comma means OR, ! negates, * is a wildcard.\n"
            "Terms match a device's tags (site:..., group:..., vendor:...) or its address (host:10.1.*)."
        )
        self._load = QPushButton("Load targets")
        self._netbox = QPushButton("Import from NetBox...")
        self._netbox.setToolTip(
            "Query a NetBox or Nautobot server for devices (primary IP, platform, site, role, tags)"
        )
        self._workers = QSpinBox()
        self._workers.setRange(1, 32)
        self._workers.setValue(4)
        self._workers.setToolTip("Devices pushed at the same time - they share this machine's network bandwidth")
        self._retries = QSpinBox()
        self._retries.setRange(0, 10)
        self._retries.setToolTip(
            "Extra attempts for timeouts, resets and failed verifications (the delay doubles each time).\n"
            "Wrong passwords and changed host keys are never retried."
        )
        self._options = PushOptionsPanel(user_label="Default username")
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setMinimumHeight(140)
        self._action = QPushButton("Start batch")
        self._retry = QPushButton("Retry failed")
        self._export = QPushButton("Export results...")
        self._summary = QLabel("No targets loaded")
        self._panel = TransferPanel()

        csv_row = QHBoxLayout()
        csv_row.addWidget(self._csv, 1)
        csv_row.addWidget(self._load)
        csv_row.addWidget(self._netbox)
        workers_row = QHBoxLayout()
        workers_row.addWidget(self._workers)
        workers_row.addSpacing(12)
        workers_row.addWidget(QLabel("Retries"))
        workers_row.addWidget(self._retries)
        workers_row.addStretch(1)
        form = QFormLayout()
        form.addRow("File to push", self._file)
        form.addRow("Targets file", csv_row)
        form.addRow("Filter", self._filter)
        form.addRow("Parallel devices", workers_row)
        run_row = QHBoxLayout()
        for widget in (self._action, self._retry, self._export):
            run_row.addWidget(widget)
        run_row.addWidget(self._summary, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._options)
        layout.addWidget(self._table, 1)
        layout.addLayout(run_row)
        layout.addWidget(self._panel, 1)

        wire_runner(self._runner, self._panel, ctx, self.SOURCE)
        wire_runner(self._aux, self._panel, ctx, self.SOURCE)
        self._runner.batch.connect(self._on_batch)
        self._runner.state.connect(self._on_state)
        self._runner.idle.connect(self._on_idle)
        self._aux.idle.connect(lambda: self._file.set_hash_enabled(True))
        self._load.clicked.connect(self._load_targets)
        self._netbox.clicked.connect(self._import_netbox)
        self._action.clicked.connect(self._on_action)
        self._retry.clicked.connect(self._on_retry)
        self._export.clicked.connect(self._export_results)
        self._file.hash_requested.connect(self._on_hash)
        self._restore()
        self._sync_buttons()

    # -- targets / table ----------------------------------------------------------
    def _load_targets(self) -> None:
        path = self._csv.path()
        try:
            targets = load_inventory(path)
        except BatchFileError as exc:
            self._report_load_error(os.path.basename(path), exc.errors)
            return
        except OSError as exc:
            self._panel.on_log(LogEvent(Level.ERROR, f"Cannot read {path}: {exc}"))
            QMessageBox.warning(self, "Targets file", f"Cannot read {path}: {exc}")
            return
        self._show_targets(targets, os.path.basename(path))

    def _import_netbox(self) -> None:
        dialog = NetBoxDialog(self._ctx.settings, self, self._netbox_fetch)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_set is None:
            return
        result = dialog.result_set
        for warning in result.warnings:
            self._panel.on_log(LogEvent(Level.WARNING, f"NetBox: {warning}"))
        if result.skipped:
            names = ", ".join(f"{n} ({why})" for n, why in result.skipped[:5]) + (
                " ..." if len(result.skipped) > 5 else ""
            )
            self._panel.on_log(LogEvent(Level.WARNING, f"NetBox: skipped {len(result.skipped)} device(s): {names}"))
        host = urlsplit(dialog.source).netloc or dialog.source
        self._show_targets(result.targets, f"NetBox ({host})")

    def _report_load_error(self, label: str, errors: List[str]) -> None:
        for err in errors:
            self._panel.on_log(LogEvent(Level.ERROR, f"{label}: {err}"))
        QMessageBox.warning(self, "Targets file", "\n".join(errors[:12]) + ("\n..." if len(errors) > 12 else ""))

    def _show_targets(self, targets: List[BatchTarget], label: str) -> None:
        """Apply the Filter box and fill the table. A filter that matches nothing leaves the current list alone."""
        expression, total = self._filter.text().strip(), len(targets)
        if expression:
            targets = filter_targets(targets, expression)
            if not targets:
                self._report_load_error(label, [f"the filter '{expression}' matches none of the {total} devices"])
                return
        self._targets, self._results = targets, {}
        self._table.setRowCount(len(targets))
        for t in targets:
            self._set_row(t.index, t.host, "ready", "", "")
            tip = "  ".join(t.tags) + (f"\nvendor: {t.vendor}" if t.vendor else "")
            first = self._table.item(t.index, 0)
            if first is not None:
                first.setToolTip(tip.strip())
        selection = f"{len(targets)} of {total} devices (filter: {expression})" if expression else f"{total} devices"
        self._panel.on_log(LogEvent(Level.INFO, f"Loaded {selection} from {label}"))
        if any(t.password for t in targets):
            self._panel.on_log(
                LogEvent(
                    Level.WARNING,
                    "The targets file contains plaintext passwords - prefer "
                    "leaving them blank and using the default credentials or an SSH key",
                )
            )
        self._update_summary()
        self._sync_buttons()

    def _set_row(self, row: int, host: str, status: str, progress: str, detail: str) -> None:
        for col, text in enumerate((host, status, progress, detail)):
            item = self._table.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row, col, item)
            item.setText(text)
            if col == 1:
                color = _COLORS.get(status)
                item.setForeground(QBrush(QColor(color)) if color else QBrush())

    @Slot(object)
    def _on_batch(self, ev: BatchEvent) -> None:
        if not 0 <= ev.index < self._table.rowCount():
            return
        progress = f"{ev.sent * 100 // ev.total}%" if ev.total else ("100%" if ev.status == "done" else "")
        self._set_row(ev.index, ev.host, ev.status, progress, ev.detail)
        if ev.status in ("done", "failed", "cancelled"):
            self._results[ev.index] = (ev.status, ev.detail)
        self._update_summary()

    def _update_summary(self) -> None:
        count = lambda s: sum(1 for st, _ in self._results.values() if st == s)  # noqa: E731
        self._summary.setText(
            f"{len(self._targets)} devices - {count('done')} ok, {count('failed')} failed, "
            f"{count('cancelled')} cancelled"
        )

    # -- run ---------------------------------------------------------------------------
    def _validate(self) -> str:
        d = self._options.defaults()
        if not os.path.isfile(self._file.path()):
            return "Choose an existing file to push."
        if not self._targets:
            return "Load a targets file first."
        if not is_ftp(d.protocol) and not (d.password or d.key_path or any(t.password for t in self._targets)):
            return "Enter a default password or choose an SSH key."
        return self._options.problem(vendors_from_targets=all(t.vendor for t in self._targets))

    def _on_action(self) -> None:
        if self._runner.running:
            self._action.setEnabled(False)
            self._action.setText("Cancelling...")
            if self._batch is not None:
                self._batch.cancel()
            return
        problem = self._validate()
        if problem:
            QMessageBox.warning(self, "Missing information", problem)
            return
        self._launch(self._targets)

    def _on_retry(self) -> None:
        retry = [t for t in self._targets if self._results.get(t.index, ("", ""))[0] in ("failed", "cancelled")]
        if retry and not self._runner.running and not self._validate():
            self._launch(retry)

    def _launch(self, targets: List[BatchTarget]) -> None:
        text = self._options.confirmation_text(f"{len(targets)} device(s)")
        if text and not ask_confirmation(self, "Confirm changes to the devices", text):
            return
        defaults, path = self._options.defaults(confirmed=bool(text)), self._file.path()
        for t in targets:
            self._results.pop(t.index, None)
            self._set_row(t.index, t.host, "queued", "", "")
        self._update_summary()
        runner = BatchRunner(
            self._runner.emit_event,
            self._services(defaults.protocol),
            lambda t: defaults.build(t.host, path, t),
            self._workers.value(),
            retries=self._retries.value(),
        )
        self._batch = runner
        self._panel.reset_progress()
        self._set_inputs_enabled(False)
        self._action.setText("Cancel")
        self._runner.start(lambda: runner.run(targets))

    @Slot(object)
    def _on_state(self, ev: StateEvent) -> None:
        if ev.state is State.FINISHED:
            self._ctx.notify(f"{self.SOURCE}: finished", ev.detail)

    @Slot()
    def _on_idle(self) -> None:
        self._batch = None
        self._set_inputs_enabled(True)
        self._action.setText("Start batch")
        self._sync_buttons()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (
            self._file,
            self._csv,
            self._filter,
            self._load,
            self._netbox,
            self._workers,
            self._retries,
            self._options,
        ):
            widget.setEnabled(enabled)
        if not enabled:
            for widget in (self._retry, self._export):
                widget.setEnabled(False)

    def _sync_buttons(self) -> None:
        running = self._runner.running
        self._action.setEnabled(running or bool(self._targets))
        has_failed = any(st in ("failed", "cancelled") for st, _ in self._results.values())
        self._retry.setEnabled(not running and has_failed)
        self._export.setEnabled(not running and bool(self._results))

    # -- hash / export -------------------------------------------------------------------
    def _on_hash(self) -> None:
        path = self._file.path()
        if self._aux.running or not os.path.isfile(path):
            return
        self._file.set_hash_enabled(False)
        service = self._hash_factory(self._aux.emit_event)
        self._aux.start(lambda: service.run(path))

    def _export_results(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results", os.path.expanduser("~/batch-results.csv"), "CSV files (*.csv);;All files (*)"
        )
        if path:
            try:
                write_results_csv(path, self._targets, self._results)
            except OSError as exc:
                QMessageBox.warning(self, "Export results", f"Cannot write {path}: {exc}")

    # -- settings / lifecycle ----------------------------------------------------------------
    def _restore(self) -> None:
        s = self._ctx.settings
        self._file.set_path(str(s.get(f"{self.PREFIX}.file", "")))
        self._csv.set_path(str(s.get(f"{self.PREFIX}.csv", "")))
        self._filter.setText(str(s.get(f"{self.PREFIX}.filter", "")))
        self._workers.setValue(int(s.get(f"{self.PREFIX}.workers", 4)))
        self._retries.setValue(int(s.get(f"{self.PREFIX}.retries", 0)))
        self._options.load(s, self.PREFIX)

    def save_settings(self) -> None:
        s = self._ctx.settings
        s.set(f"{self.PREFIX}.file", self._file.path())
        s.set(f"{self.PREFIX}.csv", self._csv.path())
        s.set(f"{self.PREFIX}.filter", self._filter.text().strip())
        s.set(f"{self.PREFIX}.workers", self._workers.value())
        s.set(f"{self.PREFIX}.retries", self._retries.value())
        self._options.save(s, self.PREFIX)

    def shutdown(self) -> None:
        if self._runner.running:
            if self._batch is not None:
                self._batch.cancel()
            self._runner.wait()
        self._aux.wait()
