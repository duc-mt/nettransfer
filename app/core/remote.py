"""Run commands on a device over an already authenticated SSH transport (exec channel or interactive shell)."""

from __future__ import annotations

import re
import socket
import time
from typing import Callable, List, Optional, Tuple

import paramiko

_PROMPT = re.compile(r"(?:\[confirm\]|\(y/n\)\??(?:\s*\[\w\])?:?|\[yes/no\]:?|[#>$%])\s*$", re.IGNORECASE)
ERROR_MARKERS = (
    "% invalid",
    "% incomplete",
    "% ambiguous",
    "% unknown",
    "syntax error",
    "unknown command",
    "unrecognized command",
    "error:",
    "invalid input",
)


class RemoteError(Exception):
    pass


def run_exec(transport: paramiko.Transport, command: str, timeout: float = 60.0) -> Tuple[str, int]:
    """Run one command on an exec channel; returns (combined output, exit status). ``timeout`` bounds each wait for
    output, so a device that computes a checksum silently for minutes needs a generous value."""
    chan = transport.open_session(timeout=timeout)
    try:
        chan.settimeout(timeout)
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        chunks: List[bytes] = []
        while True:
            data = chan.recv(65536)
            if not data:
                break
            chunks.append(data)
        status = chan.recv_exit_status()
        return b"".join(chunks).decode("utf-8", "replace"), status
    except socket.timeout:
        raise RemoteError(f"no answer from the device within {timeout:g}s for: {command}") from None
    finally:
        chan.close()


class Shell:
    """Minimal interactive session: send a line, wait for the next prompt. Prompt detection is heuristic
    (``#``, ``>``, ``$``, ``%`` or a confirmation question at the end of the output), so every wait has a timeout."""

    def __init__(
        self, transport: paramiko.Transport, timeout: float = 30.0, clock: Callable[[], float] = time.monotonic
    ):
        self._chan = transport.open_session(timeout=timeout)
        self._chan.get_pty(width=200, height=50)
        self._chan.invoke_shell()
        self._chan.settimeout(0.2)
        self._timeout, self._clock = timeout, clock
        self.closed = False

    def read_until_prompt(self, timeout: Optional[float] = None) -> str:
        deadline = self._clock() + (timeout or self._timeout)
        out = ""
        while self._clock() < deadline:
            try:
                data = self._chan.recv(65536)
            except socket.timeout:
                if _PROMPT.search(out.rstrip("\r\n ") + " ") and out:
                    return out
                continue
            if not data:
                self.closed = True
                return out
            out += data.decode("utf-8", "replace")
            if _PROMPT.search(out):
                time.sleep(0.05)  # let a trailing prompt finish arriving
                try:
                    out += self._chan.recv(65536).decode("utf-8", "replace")
                except socket.timeout:
                    pass
                return out
        raise RemoteError("timed out waiting for the device prompt")

    def send_line(self, line: str) -> str:
        self._chan.sendall((line + "\n").encode())
        return self.read_until_prompt()

    def close(self) -> None:
        self._chan.close()


def first_error(output: str) -> Optional[str]:
    for line in output.splitlines():
        if any(marker in line.lower() for marker in ERROR_MARKERS):
            return line.strip()
    return None
