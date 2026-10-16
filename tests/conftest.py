"""Shared fixtures: an in-process SSH server that speaks the scp sink protocol."""

import socket
import threading
import time

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


# --------------------------------------------------------------------------------------------------------------
# ``ssh_lab``: a multi-connection in-process SSH server for the post-push features. Unlike ``ssh_stub`` (one
# scripted scp session) it accepts many connections and offers: scp sink, SFTP rooted at a temp dir, scripted exec
# commands, a scripted interactive shell, and ``direct-tcpip`` forwarding (so it can also act as a jump host).
# --------------------------------------------------------------------------------------------------------------
import os  # noqa: E402

from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface  # noqa: E402


class _LabHandle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OK


def _make_sftp_class(root):
    class LabSFTP(SFTPServerInterface):
        def _real(self, path):
            return os.path.join(root, path.lstrip("/"))

        def list_folder(self, path):
            try:
                out = []
                for name in os.listdir(self._real(path)):
                    attr = SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as exc:
                return SFTPServer.convert_errno(exc.errno)

        def stat(self, path):
            try:
                return SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as exc:
                return SFTPServer.convert_errno(exc.errno)

        lstat = stat

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o666)
            except OSError as exc:
                return SFTPServer.convert_errno(exc.errno)
            if flags & os.O_WRONLY:
                mode = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                mode = "a+b" if flags & os.O_APPEND else "r+b"
            else:
                mode = "rb"
            handle = _LabHandle(flags)
            handle.filename = real
            handle.readfile = handle.writefile = os.fdopen(fd, mode)
            return handle

        def remove(self, path):
            try:
                os.remove(self._real(path))
                return paramiko.SFTP_OK
            except OSError as exc:
                return SFTPServer.convert_errno(exc.errno)

    return LabSFTP


class _LabServer(paramiko.ServerInterface):
    def __init__(self, lab):
        self.lab = lab

    def check_auth_password(self, username, password):
        ok = (username, password) == ("admin", "secret")
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if not self.lab.allow_forward:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.lab.pending[chanid] = destination
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self.lab.handle_exec, args=(channel, command.decode()), daemon=True).start()
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self.lab.handle_shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_pty_request(self, *_args):
        return True


def _pump(src, dst):
    try:
        while True:
            data = src.recv(32768)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for end in (src, dst):
            try:
                end.close()
            except Exception:
                pass


class SshLab:
    """Scriptable fake device. Tests set ``commands`` (exec) / ``shell_replies`` and read ``files`` / ``executed``."""

    def __init__(self, root):
        self.root = str(root)
        self.key = paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.files = {}  # scp uploads: name -> bytes
        self.executed = []  # exec commands seen
        self.shell_log = []  # lines typed into the interactive shell
        self.commands = lambda command: ("", 0)  # exec handler: command -> (stdout, exit status)
        self.shell_replies = {}  # line -> text | (text, False) for a question with no prompt after it | [replies in order]
        self.prompt = "router# "
        self.allow_forward = False
        self.pending = {}  # direct-tcpip channel id -> (host, port)
        self.forwarded = []  # destinations this server was asked to reach (jump-host tests)
        self._stop = False
        self._transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        self.listener.close()
        for transport in self._transports:
            transport.close()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.key)
            transport.set_subsystem_handler("sftp", SFTPServer, _make_sftp_class(self.root))
            self._transports.append(transport)
            try:
                transport.start_server(server=_LabServer(self))
            except Exception:
                continue
            threading.Thread(target=self._serve_forwards, args=(transport,), daemon=True).start()

    def _serve_forwards(self, transport):
        # direct-tcpip channels arrive through transport.accept(); pump them to the real destination
        while transport.is_active():
            chan = transport.accept(1)
            dest = self.pending.pop(chan.get_id(), None) if chan is not None else None
            if dest is None:
                continue
            self.forwarded.append(dest)
            try:
                upstream = socket.create_connection(dest, timeout=10)
            except OSError:
                chan.close()
                continue
            for src, dst in ((chan, upstream), (upstream, chan)):
                threading.Thread(target=_pump, args=(src, dst), daemon=True).start()

    def handle_exec(self, chan, command):
        time.sleep(0.05)  # let paramiko send CHANNEL_SUCCESS for the exec request before we write/close
        self.executed.append(command)
        try:
            if command.startswith("scp "):
                self._scp_sink(chan, command)
                return
            out, status = self.commands(command)
            chan.sendall(out.encode())
            chan.send_exit_status(status)
        except OSError:
            pass  # the client hung up while we were still "working" (a cancelled check): nothing to deliver
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def _scp_sink(self, chan, command):
        chan.send(b"\x00")
        header = _read_until(chan).decode().split()
        size, name = int(header[1]), header[2]
        chan.send(b"\x00")
        data = b""
        while len(data) < size:
            data += chan.recv(min(32768, size - len(data)))
        chan.recv(1)
        chan.send(b"\x00")
        self.files[name] = data
        chan.send_exit_status(0)

    def handle_shell(self, chan):
        time.sleep(0.05)
        try:
            chan.send(self.prompt.encode())
            buf = b""
            while True:
                data = chan.recv(1024)
                if not data:
                    return
                buf += data
                while b"\n" in buf or b"\r" in buf:
                    sep = min(i for i in (buf.find(b"\n"), buf.find(b"\r")) if i != -1)
                    line, buf = buf[:sep].decode(), buf[sep + 1 :].lstrip(b"\n")
                    self.shell_log.append(line)
                    reply = self.shell_replies.get(line, "")
                    if isinstance(reply, list):  # successive answers to the same line (a multi-question installer)
                        reply = reply.pop(0) if reply else ""
                    text, with_prompt = reply if isinstance(reply, tuple) else (reply, True)
                    # a question ("Proceed? [confirm]") is the last thing on the line: no prompt follows it
                    tail = ("\r\n" if text else "") + self.prompt if with_prompt else ""
                    chan.send((text + tail).encode())
        except Exception:
            return


@pytest.fixture
def ssh_lab(tmp_path):
    root = tmp_path / "sftp-root"
    root.mkdir()
    lab = SshLab(root)
    yield lab
    lab.close()


# --------------------------------------------------------------------------------------------------------------
# Hostile resolver: every *name* lookup (reverse DNS, hostname -> address) stalls. IP literals and "localhost" still
# resolve instantly, exactly like a runner whose DNS is slow. Anything in the product that does a hidden lookup on
# its hot path (like HTTPServer.server_bind -> getfqdn did) then blows its timeout instead of passing by luck.
# Enabled per test with ``@pytest.mark.hostile_dns`` or for the whole suite with NETTRANSFER_HOSTILE_DNS=1 (CI job).
# --------------------------------------------------------------------------------------------------------------
HOSTILE_DNS_STALL = 60.0


def _install_hostile_dns(monkeypatch):
    import ipaddress

    real_getaddrinfo = socket.getaddrinfo

    def is_literal(host):
        if host in (None, "", "localhost"):
            return True
        try:
            ipaddress.ip_address(str(host).split("%")[0])
            return True
        except ValueError:
            return False

    def stall(*_a, **_k):
        time.sleep(HOSTILE_DNS_STALL)
        raise socket.gaierror("hostile resolver")

    def getaddrinfo(host, *args, **kwargs):
        return real_getaddrinfo(host, *args, **kwargs) if is_literal(host) else stall()

    for name in ("getfqdn", "gethostbyaddr", "gethostbyname_ex", "getnameinfo"):
        monkeypatch.setattr(socket, name, stall)
    monkeypatch.setattr(socket, "gethostbyname", lambda host: host if is_literal(host) else stall())
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _hostile_dns(request, monkeypatch):
    if request.node.get_closest_marker("hostile_dns") or os.environ.get("NETTRANSFER_HOSTILE_DNS") == "1":
        _install_hostile_dns(monkeypatch)
