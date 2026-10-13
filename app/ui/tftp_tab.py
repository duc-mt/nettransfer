"""[TFTP Server] tab: serve the selected file to any device that asks for it on UDP 69."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QWidget

from ..core.device_hints import hints_for
from ..core.serve_policy import ServePolicy
from ..platform.detect import PlatformInfo
from ..platform.privileges import BindProbe, remedies
from .common import UiContext
from .server_tab import ServerTab


class TftpTab(ServerTab):
    SOURCE, PREFIX = "TFTP", "tftp"

    def __init__(
        self,
        *,
        ctx: UiContext,
        service_factory,
        hash_factory,
        list_interfaces,
        probe: Callable[[str, int], BindProbe],
        platform_info: PlatformInfo,
        port: int,
        initial_probe: Optional[BindProbe] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            ctx=ctx,
            service_factory=service_factory,
            hash_factory=hash_factory,
            list_interfaces=list_interfaces,
            hints=hints_for("tftp"),
            parent=parent,
        )
        self._probe, self._info, self._port = probe, platform_info, port
        self._initial_probe = initial_probe
        self._finish_init()

    def _hint_values(self) -> dict:
        return {"port": self._port, "scheme": "tftp"}

    def _serve(self, service, path: str, ip: str, policy: ServePolicy) -> None:
        service.serve(path, ip, self._port, policy)

    def _after_interfaces_changed(self) -> None:
        if self._runner.running:  # our own socket would make the probe report "in use"
            return
        probe, self._initial_probe = self._initial_probe, None
        probe = probe or self._probe(self._iface.currentData(), self._port)
        self._banner.show_lines(remedies(self._info, probe, self._port), warning=not probe.ok)
