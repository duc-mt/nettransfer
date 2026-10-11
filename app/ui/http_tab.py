"""[HTTP(S) Server] tab: devices pull the file with `copy http://...` (Cisco, Junos ...)."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QSpinBox, QWidget

from ..core.device_hints import HTTP_HINTS
from ..core.http_service import DEFAULT_PORT
from ..core.serve_policy import ServePolicy
from .common import UiContext
from .server_tab import ServerTab


class HttpTab(ServerTab):
    SOURCE, PREFIX = "HTTP", "http"

    def __init__(
        self, *, ctx: UiContext, service_factory, hash_factory, list_interfaces, parent: Optional[QWidget] = None
    ) -> None:
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(DEFAULT_PORT)
        self._https = QCheckBox("HTTPS (self-signed certificate - the device must skip verification)")
        super().__init__(
            ctx=ctx,
            service_factory=service_factory,
            hash_factory=hash_factory,
            list_interfaces=list_interfaces,
            hints=HTTP_HINTS,
            parent=parent,
        )
        self._port_spin.valueChanged.connect(self._update_hints)
        self._https.toggled.connect(self._update_hints)
        self._finish_init()

    def _add_form_rows(self, form: QFormLayout) -> None:
        row = QHBoxLayout()
        row.addWidget(self._port_spin)
        row.addWidget(self._https, 1)
        form.addRow("Port", row)

    def _lockable(self) -> List[QWidget]:
        return [self._port_spin, self._https]

    def _hint_values(self) -> dict:
        return {"port": self._port_spin.value(), "scheme": "https" if self._https.isChecked() else "http"}

    def _serve(self, service, path: str, ip: str, policy: ServePolicy) -> None:
        service.serve(path, ip, self._port_spin.value(), policy, self._https.isChecked())

    def _restore(self) -> None:
        super()._restore()
        self._port_spin.setValue(int(self._ctx.settings.get("http.port", DEFAULT_PORT)))
        self._https.setChecked(bool(self._ctx.settings.get("http.https", False)))

    def save_settings(self) -> None:
        super().save_settings()
        self._ctx.settings.set("http.port", self._port_spin.value())
        self._ctx.settings.set("http.https", self._https.isChecked())
