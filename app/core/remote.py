"""Run commands on a device over an already authenticated SSH transport (exec channel or interactive shell)."""

from __future__ import annotations

import re
import socket
import time
from typing import Callable, List, Optional, Sequence, Tuple

import paramiko

# A prompt ends in one of these characters, or is a question waiting for an answer.
_PROMPT_CHAR = re.compile(r"[#>$%]\s*$")
_QUESTION = re.compile(
    r"(?:[?:]|\[confirm\]|\[yes,\s*no\]"
    r"|\[(?:y|n|yes|no)(?:/(?:y|n|yes|no))?\]"
    r"|\((?:y|n|yes|no)(?:/(?:y|n|yes|no))?\)"
    r"|\(y or n\))"
    r"\s*:?\s*(?:\[[^\]\n]{0,12}\]|\([^)\n]{0,12}\))?\s*:?\s*$",
    re.IGNORECASE,
)
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
    "invalid command",
    "invalid syntax",
    "command fail",
    "command not found",
    "no such file or directory",
)
ABORT = "!"  # an answer of "!" aborts the script instead of answering (used when a prompt must not be auto-answered)


class RemoteError(Exception):
    pass


def looks_like_question(output: str) -> bool:
    """True if the device's last line is a question waiting for an answer (``[confirm]``, ``(y/n)``, ``?`` ...)."""
    lines = output.rstrip().splitlines()
    return bool(lines) and bool(_QUESTION.search(lines[-1].rstrip()))


def is_prompt(output: str) -> bool:
    return bool(_PROMPT_CHAR.search(output)) or looks_like_question(output)


def parse_answer(line: str) -> Tuple[Optional[str], str]:
    """``@y`` -> (None, "y"): answer ``y`` to any question. ``@~save~n`` -> ("save", "n"): only a question matching
    the regex. ``@`` alone answers with Enter. Answers are *gated*: they are only ever sent when the device really
    asked something, so a stray "yes" can never be executed as a command by a POSIX shell."""
    body = line[1:]
    if body.startswith("~") and body.count("~") >= 2:
        _, pattern, answer = body.split("~", 2)
        return pattern, answer
    return None, body


def describe_line(line: str) -> str:
    if not line.startswith("@"):
        return line or "<Enter>"
    pattern, answer = parse_answer(line)
    what = "abort" if answer == ABORT else (answer or "<Enter>")
    return f"(if asked{' about /' + pattern + '/' if pattern else ''}: {what})"


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
    (``#``, ``>``, ``$``, ``%`` or a question at the end of the output), so every wait has a timeout."""

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
                if out and is_prompt(out.rstrip("\r\n ") + " "):
                    return out
                continue
            if not data:
                self.closed = True
                return out
            out += data.decode("utf-8", "replace")
            if is_prompt(out):
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

    def run_script(
        self,
        lines: Sequence[str],
        emit: Callable[[str], None] = lambda _line: None,
        disconnect_after: Optional[str] = None,
    ) -> str:
        """Type ``lines`` one by one and return everything the device printed. Lines starting with ``@`` are gated
        answers (see ``parse_answer``). A device that stops answering right after ``disconnect_after`` (a reload) is
        the expected outcome, not an error. Stops at the first line the device rejects."""
        transcript = [self.read_until_prompt()]  # banner / first prompt
        last, tolerating = transcript[0], False
        for line in lines:
            if line.startswith("@"):
                pattern, answer = parse_answer(line)
                asked = looks_like_question(last) and (pattern is None or re.search(pattern, last, re.IGNORECASE))
                if not asked:
                    continue  # nothing to answer: sending text now could be run as a command
                if answer == ABORT:
                    raise RemoteError(f"the device asked: {last.strip().splitlines()[-1].strip()} - answer it yourself")
                to_send = answer
            else:
                to_send = line
                tolerating = tolerating or (disconnect_after is not None and line == disconnect_after)
            emit(f"  > {to_send or '<Enter>'}")
            try:
                last = self.send_line(to_send)
            except RemoteError:
                if tolerating:
                    emit("Device stopped answering - reload in progress")
                    return "".join(transcript)
                raise
            transcript.append(last)
            problem = first_error(last)
            if problem:
                raise RemoteError(f"'{to_send}' was rejected by the device: {problem}")
            if self.closed:
                if tolerating:
                    emit("Session closed by the device - reload in progress")
                    return "".join(transcript)
                raise RemoteError("the device closed the session before the script finished")
        return "".join(transcript)

    def close(self) -> None:
        self._chan.close()


def first_error(output: str) -> Optional[str]:
    for line in output.splitlines():
        if any(marker in line.lower() for marker in ERROR_MARKERS):
            return line.strip()
    return None
