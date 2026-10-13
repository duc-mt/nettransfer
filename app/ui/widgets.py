"""Widgets shared by the tabs: file picker, banner, safety/hint panels, progress + log panel."""

from __future__ import annotations

import html
import os
import time
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.device_hints import Hint, render
from ..core.events import Level, LogEvent, ProgressEvent
from ..core.formatting import human_size, human_speed
from ..core.serve_policy import ServePolicy, parse_allowlist
from ..core.settings import SettingsStore

_LEVEL_COLORS = {Level.SUCCESS: "#3fb950", Level.WARNING: "#d29922", Level.ERROR: "#f85149"}
_BANNER_STYLES = {
    True: "background:#fff4ce; color:#5c4300; border:1px solid #e0c36a;",  # warning
    False: "background:#e7f1fb; color:#0b3d62; border:1px solid #9cc3e6;",  # info
}


def _mono():
    return QFontDatabase.systemFont(QFontDatabase.FixedFont)


class FilePicker(QWidget):
    """Line edit + Browse (+ optional Hash). Accepts a file dropped anywhere on it."""

    changed = Signal(str)
    hash_requested = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        with_hash: bool = False,
        placeholder: str = "Select any file (firmware, config, image ...)",
        dialog_title: str = "Select file to transfer",
        file_filter: str = "All files (*)",
    ) -> None:
        super().__init__(parent)
        self._title, self._filter = dialog_title, file_filter
        self.setAcceptDrops(True)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(placeholder)
        self._edit.setAcceptDrops(False)  # let drops bubble up to this widget instead of pasting a URL as text
        self._button = QPushButton("Browse...")
        self._hash = QPushButton("Hash")
        self._hash.setToolTip("Show MD5 and SHA-256 in the log, to compare with the device's own verify output")
        self._hash.setVisible(with_hash)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._button)
        layout.addWidget(self._hash)
        self._button.clicked.connect(self._browse)
        self._hash.clicked.connect(self.hash_requested)
        self._edit.textChanged.connect(self.changed)

    def path(self) -> str:
        return self._edit.text().strip()

    def set_path(self, text: str) -> None:
        self._edit.setText(text)

    def set_hash_enabled(self, enabled: bool) -> None:
        self._hash.setEnabled(enabled)

    def _browse(self) -> None:
        start = os.path.dirname(self.path()) or os.path.expanduser("~")
        chosen, _ = QFileDialog.getOpenFileName(self, self._title, start, self._filter)
        if chosen:
            self._edit.setText(chosen)

    @staticmethod
    def _dropped_file(mime) -> str:
        for url in mime.urls():
            if url.isLocalFile() and os.path.isfile(url.toLocalFile()):
                return os.path.normpath(url.toLocalFile())
        return ""

    def dragEnterEvent(self, event) -> None:
        if self.isEnabled() and self._dropped_file(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        dropped = self._dropped_file(event.mimeData())
        if dropped:
            self._edit.setText(dropped)
            event.acceptProposedAction()


class InfoBanner(QLabel):
    """Hidden when there is nothing to say; amber for problems, blue for notes."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)  # lets users copy the sudo command
        self.hide()

    def show_lines(self, lines: List[str], warning: bool) -> None:
        if not lines:
            self.hide()
            return
        self.setStyleSheet(f"QLabel {{ {_BANNER_STYLES[warning]} border-radius:4px; padding:6px 8px; }}")
        self.setText("\n".join(lines))
        self.show()


class SafetyPanel(QGroupBox):
    """Serve-once / idle auto-stop / client allowlist for the unauthenticated servers."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Safety (TFTP/HTTP have no authentication)", parent)
        self._once = QCheckBox("Stop after the first successful transfer")
        self._idle = QCheckBox("Stop when idle for")
        self._idle_min = QSpinBox()
        self._idle_min.setRange(1, 1440)
        self._idle_min.setValue(10)
        self._idle_min.setSuffix(" min")
        self._allow = QLineEdit()
        self._allow.setPlaceholderText("Only these clients, e.g. 10.0.0.0/24, 192.168.1.5   (blank = anyone)")
        idle_row = QHBoxLayout()
        idle_row.addWidget(self._idle)
        idle_row.addWidget(self._idle_min)
        idle_row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self._once)
        layout.addLayout(idle_row)
        layout.addWidget(self._allow)

    def policy(self) -> ServePolicy:
        """Raises ValueError (user-presentable) when the allowlist is malformed."""
        return ServePolicy(
            self._once.isChecked(),
            self._idle_min.value() * 60.0 if self._idle.isChecked() else None,
            parse_allowlist(self._allow.text()),
        )

    def load(self, store: SettingsStore, prefix: str) -> None:
        self._once.setChecked(bool(store.get(f"{prefix}.once", False)))
        self._idle.setChecked(bool(store.get(f"{prefix}.idle", False)))
        self._idle_min.setValue(int(store.get(f"{prefix}.idle_min", 10)))
        self._allow.setText(str(store.get(f"{prefix}.allow", "")))

    def save(self, store: SettingsStore, prefix: str) -> None:
        store.set(f"{prefix}.once", self._once.isChecked())
        store.set(f"{prefix}.idle", self._idle.isChecked())
        store.set(f"{prefix}.idle_min", self._idle_min.value())
        store.set(f"{prefix}.allow", self._allow.text().strip())


class HintPanel(QWidget):
    """Copy-ready device commands, filled in with the server IP/port and the selected file name."""

    def __init__(self, hints: Sequence[Hint], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._hints = list(hints)
        self._values: Dict[str, object] = {}
        self._vendor = QComboBox()
        for hint in self._hints:
            self._vendor.addItem(hint.vendor)
        self._pull, self._verify = QLineEdit(), QLineEdit()
        for edit in (self._pull, self._verify):
            edit.setReadOnly(True)
            edit.setFont(_mono())
        self._copy_pull, self._copy_verify = QPushButton("Copy"), QPushButton("Copy")
        self._verify_label = QLabel("Verify")
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #9a6700;")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Device command", self._vendor)
        pull_row, verify_row = QHBoxLayout(), QHBoxLayout()
        pull_row.addWidget(self._pull, 1)
        pull_row.addWidget(self._copy_pull)
        verify_row.addWidget(self._verify, 1)
        verify_row.addWidget(self._copy_verify)
        form.addRow("Run on device", pull_row)
        form.addRow(self._verify_label, verify_row)
        form.addRow("", self._note)
        QVBoxLayout(self).addLayout(form)
        self._vendor.currentIndexChanged.connect(lambda _i: self._render())
        self._copy_pull.clicked.connect(lambda: QApplication.clipboard().setText(self._pull.text()))
        self._copy_verify.clicked.connect(lambda: QApplication.clipboard().setText(self._verify.text()))
        self.setToolTip("Templates - adjust filesystem, VRF and syntax to your platform and release")
        self._render()

    def update_context(self, **values: object) -> None:
        self._values = values
        self._render()

    def _render(self) -> None:
        if not self._hints:
            return
        hint = self._hints[self._vendor.currentIndex()]
        values = {"scheme": "http", "ip": "<server-ip>", "port": "", "file": "<file>", **self._values}
        self._pull.setText(render(hint.pull, **values))
        has_verify = hint.verify is not None
        self._verify.setText(render(hint.verify, **values) if has_verify else "")
        for widget in (self._verify, self._copy_verify, self._verify_label):
            widget.setVisible(has_verify)
        self._note.setText(hint.note)
        self._note.setVisible(bool(hint.note))


class TransferPanel(QWidget):
    """Progress bar, speed/bytes line and colour-coded log (slots run on the GUI thread)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)  # permille: avoids QProgressBar's int32 limit on huge files
        self._stats = QLabel("Idle")
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(_mono())
        self._log.document().setMaximumBlockCount(5000)
        export = QPushButton("Export log...")
        export.setToolTip("Save the log shown here to a text file")
        export.clicked.connect(self._export)
        clear = QPushButton("Clear log")
        clear.clicked.connect(self._log.clear)

        top = QHBoxLayout()
        top.addWidget(self._stats, 1)
        top.addWidget(export)
        top.addWidget(clear)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._bar)
        layout.addLayout(top)
        layout.addWidget(self._log, 1)

    def reset_progress(self) -> None:
        self._bar.setValue(0)
        self._stats.setText("Idle")

    def log_text(self) -> str:
        return self._log.toPlainText()

    @Slot(object)
    def on_progress(self, ev: ProgressEvent) -> None:
        frac = 1.0 if ev.total <= 0 else min(ev.sent / ev.total, 1.0)
        self._bar.setValue(int(frac * 1000))
        who = f"{ev.peer}  \u2022  " if ev.peer else ""
        self._stats.setText(f"{who}{human_speed(ev.bps)}  \u2022  {human_size(ev.sent)} / {human_size(ev.total)}")

    @Slot(object)
    def on_log(self, ev: LogEvent) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        text = html.escape(ev.message)
        color = _LEVEL_COLORS.get(ev.level)
        body = f'<span style="color:{color}">{text}</span>' if color else text
        self._log.append(f'<span style="white-space:pre-wrap"><span style="color:gray">[{stamp}]</span> {body}</span>')

    def _export(self) -> None:
        default = f"nettransfer-log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export log", os.path.join(os.path.expanduser("~"), default), "Text files (*.txt);;All files (*)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.log_text() + "\n")
            except OSError as exc:
                self._log.append(f"Could not write {html.escape(path)}: {html.escape(str(exc))}")
