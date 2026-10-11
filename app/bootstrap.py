"""Composition root: the only module that wires concrete classes together."""

from __future__ import annotations

import sys

from . import crashlog
from .core.audit import AuditLog
from .core.hashing import HashService
from .core.http_service import HttpService
from .core.known_hosts import KnownHostsStore
from .core.precheck import ConnectionTester
from .core.requests import make_service_factory
from .core.settings import SettingsStore
from .core.tftp_service import TftpService
from .platform.network import list_interfaces
from .platform.paths import data_dir, fix_owner
from .platform.privileges import StartupReport, probe_udp_bind
from .resources import resource_path


def run_gui(report: StartupReport, selftest: bool = False) -> int:
    # Qt is imported here, after run.py has verified it is installed.
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.batch_tab import BatchTab
    from .ui.common import UiContext
    from .ui.http_tab import HttpTab
    from .ui.main_window import MainWindow
    from .ui.push_tab import PushTab
    from .ui.tftp_tab import TftpTab

    app = QApplication(sys.argv)
    app.setApplicationName("Network File Transfer")
    icon_file = resource_path("assets/icon.png")
    icon = QIcon(str(icon_file)) if icon_file.exists() else QIcon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    folder = data_dir()
    crashlog.install(folder, lambda title, text: QMessageBox.critical(None, title, text))
    if selftest:
        _selftest_imports()
    ctx = UiContext(SettingsStore(folder / "settings.json", fix_owner), AuditLog(folder / "logs", fix_owner))
    known_hosts = KnownHostsStore(folder / "known_hosts", fix_owner)
    services = lambda protocol: make_service_factory(protocol, known_hosts)  # noqa: E731
    common = dict(ctx=ctx, hash_factory=HashService)

    tabs = [
        (
            "TFTP Server",
            TftpTab(
                service_factory=TftpService,
                list_interfaces=list_interfaces,
                probe=probe_udp_bind,
                platform_info=report.platform,
                port=report.tftp_port,
                initial_probe=report.probe,
                **common,
            ),
        ),
        ("HTTP(S) Server", HttpTab(service_factory=HttpService, list_interfaces=list_interfaces, **common)),
        ("Push", PushTab(services=services, tester_factory=lambda sink: ConnectionTester(sink, known_hosts), **common)),
        ("Batch Push", BatchTab(services=services, **common)),
    ]
    window = MainWindow(tabs, report.platform, ctx, icon, folder)
    ctx.notify = window.notify
    window.show()
    if selftest:
        if not icon_file.exists():
            raise SystemExit("selftest failed: bundled assets/icon.png is missing")
        QTimer.singleShot(400, app.quit)  # prove the event loop runs, then leave
    code = app.exec()
    if selftest:
        print("selftest ok")
    return code


def _selftest_imports() -> None:
    """Touch everything that PyInstaller can only discover dynamically, so a broken bundle fails here
    (in CI) rather than on a user's machine."""
    import paramiko
    import psutil
    import scp  # noqa: F401
    import tftpy  # noqa: F401

    from .core.http_service import make_self_signed

    make_self_signed("127.0.0.1")  # cryptography + its Rust backend
    paramiko.RSAKey.generate(1024)  # paramiko crypto path
    psutil.net_if_addrs()  # native extension
