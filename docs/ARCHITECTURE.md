# Architecture

```
run.py                      GUI (default) or CLI dispatch; --selftest; preflight: Python -> deps -> OS -> port probe
app/
  bootstrap.py              composition root: the only place concrete classes are wired together
  cli.py                    headless commands (no Qt)          crashlog.py  last-resort error reporting
  platform/                 detect (OS/admin) | privileges (bind probe, per-OS remedies) | network (IPv4 discovery)
                            paths (per-user data dir, correct under sudo)
  core/        (no Qt)      events | progress | formatting | serve_policy | device_hints | requests | settings | audit
                            tftp_service | http_service | scp_service (SCP+SFTP) | ftp_service | ssh_session
                            known_hosts | precheck | batch | hashing
  ui/                       main_window | server_tab (base) -> tftp_tab, http_tab | push_tab | batch_tab
                            push_options | widgets | common (UiContext, wiring) | workers (Qt bridge)
packaging/                  nettransfer.spec | build.py | make_icons.py | cli_entry.py | entitlements.plist
scripts/                    check_version.py | extract_changelog.py                  (release helpers)
.github/                    workflows (ci, build/release, codeql, security) | dependabot | issue + PR templates
tests/                      services use list.append as the event sink; GUI runs offscreen
```

Dependencies point inward: `ui -> core`, `bootstrap -> everything`; `core` never imports Qt or `ui`. Services and tabs get
their collaborators (service factories, interface lister, port probe, stores, context) through constructors.

## Event flow and threads

```
 worker QThread                                              GUI thread
 library callback -> ProgressTracker (sliding-window speed, <=10 Hz, final always sent)
   -> service.on_event(Log|Progress|State|Batch event)       # EventSink: plain callable
   -> JobRunner.emit_event -> Signal.emit ==queued==>  panel / table slots, audit log, notifications
```

Widgets are touched only on the GUI thread. Signals from workers are connected to real `QObject` slots (never to
lambdas, which PySide may run on the emitting thread). `JobRunner` owns one `QThread` and emits `idle` when it has fully
finished; tabs re-enable controls on `idle`, never earlier. Each tab has a second runner for short aux jobs (hash,
connection test). `LogEvent.tag` marks a completed transfer, which drives desktop notifications.

## Servers

*TFTP*: tftpy calls `dyn_file_func` only for names missing from its root, so the root is an empty temp dir and every read
is answered with the selected file via `_ProgressReader`; `upload_open` returns `None` (read-only). The first read size is
the negotiated block size. *HTTP(S)*: `ThreadingHTTPServer`, any path serves the file, 64 KB chunks, TLS handshake in the
handler thread, throw-away RSA-2048 certificate. Both use `ServeGuard`: allowlist, serve-once, idle watchdog.

## SSH

`ssh_session.open_transport`: connect, host-key check (trust on first use via `KnownHostsStore`, changed key refused unless
replaced explicitly), then key auth, password auth, keyboard-interactive fallback. Passphrases are encoded to bytes and
encrypted-key errors are translated into readable messages. SCP uses `scp`, SFTP uses `SFTPClient.put(confirm=True)`.
Cancel closes the transport, which also interrupts a blocked handshake.

## Batch

`BatchRunner(service_factory, request_factory)` is protocol-agnostic. Each device gets its own service whose events are
re-published as host-prefixed log lines and `BatchEvent`s; a failing/crashing device never stops the others; cancel
reaches running services and marks queued ones. The UI keeps cumulative results so *Retry failed* re-runs only failures.

## Decisions

* **paramiko `>=4,<5`**: 5.0 removed SHA-1 kex and `ssh-rsa`; 4.x still offers them last, so no "legacy" switch is needed
  (proved against a legacy-only OpenSSH server in `tests/test_openssh.py`).
* **Data folder** resolves to the invoking user under `sudo` and hands created files back to them.
* **Passwords/passphrases are never persisted or logged**; CSV passwords are allowed but warned about.
* **macOS ships an `.app`** (PyInstaller discourages one-file windowed on macOS); Windows/Linux ship one file.
* **Packaged Linux build does not suggest `setcap`** (breaks one-file loaders); it suggests `sudo -E <executable>`.

## Known limits

* tftpy serves all sessions from one select loop: concurrent devices interleave, not parallel. TFTP "sent" means every byte
  was handed to the socket and the session closed; the protocol has no stronger signal.
* No RFC 7440 windowed TFTP (would need a different engine); HTTP(S) is the fast alternative.
* HTTPS needs a device that accepts a self-signed certificate and TLS 1.2+. FTPS does not verify the certificate.
* Windows Firewall is not *detected*; the banner gives the `netsh` rule to add.
* FTPS is untested here (no TLS FTP server available in the test environment); plain FTP is tested against pyftpdlib.
* Verified on Linux (headless Qt, real sockets, real OpenSSH, a real PyInstaller build). Windows/macOS builds are produced by
  the CI matrix and have not been run by the author.

## Repository and delivery

| Piece | Role |
|---|---|
| `ci.yml` | ruff lint + format check, version/CHANGELOG consistency, tests on Linux (Python 3.9-3.13), Windows and macOS, a launch self-test per OS, coverage report; a single `CI passed` gate job for branch protection. Reusable (`workflow_call`). |
| `build.yml` | on a `v*` tag: verify tag = `__version__` = CHANGELOG, run the whole CI, build/smoke-test/sign the executables on 4 runners, then publish the GitHub Release (CHANGELOG notes, merged `SHA256SUMS.txt`, build-provenance attestations). On PRs touching packaging it only dry-runs the build. |
| `codeql.yml`, `security.yml`, `dependabot.yml` | static analysis (Python + Actions), dependency review on PRs, weekly `pip-audit`, grouped weekly dependency PRs (paramiko major bumps are ignored on purpose). |
| `tests/test_repo_hygiene.py` | keeps the above honest: required files exist, relative links and anchors resolve, workflows have least-privilege permissions and timeouts, the paramiko pin matches Dependabot, the ruff version is pinned identically in CI and pre-commit, the release scripts behave. |

Releasing is documented in [RELEASING.md](RELEASING.md); contributing in [../CONTRIBUTING.md](../CONTRIBUTING.md).
