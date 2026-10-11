"""Main window: tabs, Options menu, desktop notifications; stops background work on close."""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import QMainWindow, QSystemTrayIcon, QTabWidget

from .. import __version__
from ..platform.detect import PlatformInfo
from .common import UiContext


class MainWindow(QMainWindow):
    """``tabs`` is a sequence of (title, widget); every widget provides save_settings() and shutdown()."""

    def __init__(
        self, tabs: Sequence[tuple], platform_info: PlatformInfo, ctx: UiContext, icon: QIcon = QIcon(), data_dir=None
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"Network File Transfer {__version__}")
        self.setMinimumSize(780, 700)
        if not icon.isNull():
            self.setWindowIcon(icon)
        self._ctx = ctx
        self._tabs = [widget for _title, widget in tabs]
        self._data_dir = data_dir
        widget = QTabWidget()
        for title, tab in tabs:
            widget.addTab(tab, title)
        self.setCentralWidget(widget)
        self.statusBar().showMessage(platform_info.describe())

        self._tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(icon if not icon.isNull() else self.windowIcon(), self)

        menu = self.menuBar().addMenu("&Options")
        self._notify_action = QAction("Desktop notifications when a transfer ends", self, checkable=True)
        self._notify_action.setChecked(bool(ctx.settings.get("ui.notifications", True)) and self._tray is not None)
        self._notify_action.setEnabled(self._tray is not None)
        self._audit_action = QAction("Keep an audit log of every transfer", self, checkable=True)
        self._audit_action.setChecked(bool(ctx.settings.get("ui.audit", True)))
        self._audit_action.toggled.connect(lambda on: setattr(ctx.audit, "enabled", on))
        ctx.audit.enabled = self._audit_action.isChecked()
        open_action = QAction("Open data folder (settings, known hosts, logs)", self)
        open_action.triggered.connect(self._open_data_dir)
        open_action.setEnabled(data_dir is not None)
        for action in (self._notify_action, self._audit_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(open_action)

    def notify(self, title: str, message: str) -> None:
        """Desktop notification, only when the window is not in front (the log already shows it)."""
        if self._tray is not None and self._notify_action.isChecked() and not self.isActiveWindow():
            self._tray.show()
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _open_data_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._data_dir)))

    def closeEvent(self, event: QCloseEvent) -> None:
        for tab in self._tabs:
            tab.shutdown()  # releases UDP 69 / TCP ports before the process exits
            tab.save_settings()
        self._ctx.settings.set("ui.notifications", self._notify_action.isChecked())
        self._ctx.settings.set("ui.audit", self._audit_action.isChecked())
        self._ctx.settings.save()
        if self._tray is not None:
            self._tray.hide()
        event.accept()
