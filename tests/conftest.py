"""Shared fixtures: an in-process SSH server that speaks the scp sink protocol."""

import socket
import threading

import paramiko
import pytest


class _Server(paramiko.ServerInterface):
    def __init__(self):
        self.command = None
        self.exec_requested = threading.Event()
        self.client_key = None  # set by a test to enable public-key auth
        self.kbd_only = False  # set by a test to offer keyboard-interactive only

    def check_auth_password(self, username, password):
        ok = (username, password) == ("admin", "secret")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = self.client_key is not None and key.asbytes() == self.client_key.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        query = paramiko.InteractiveQuery("", "")
        query.add_prompt("Password: ", False)
        return query

    def check_auth_interactive_response(self, responses):
        return paramiko.AUTH_SUCCESSFUL if list(responses) == ["secret"] else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "keyboard-interactive" if self.kbd_only else "password,publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode()
        self.exec_requested.set()
        return True


def _read_until(chan, terminator=b"\n"):
    buf = b""
    while not buf.endswith(terminator):
        chunk = chan.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture
def ssh_stub():
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    state = {"received": None, "name": None, "server": _Server()}

    def run():
        try:
            conn, _ = listener.accept()
            transport = paramiko.Transport(conn)
            transport.add_server_key(key)
            transport.start_server(server=state["server"])
            chan = transport.accept(10)
            if chan is None or not state["server"].exec_requested.wait(10):
                return
            chan.send(b"\x00")
            header = _read_until(chan).decode().split()  # C0644 <size> <name>
            size = int(header[1])
            state["name"] = header[2]
            chan.send(b"\x00")
            data = b""
            while len(data) < size:
                data += chan.recv(min(32768, size - len(data)))
            chan.recv(1)  # trailing \0 from the client
            chan.send(b"\x00")
            state["received"] = data
            chan.close()
            transport.close()
        except Exception:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield listener.getsockname()[1], state
    listener.close()
    thread.join(5)
