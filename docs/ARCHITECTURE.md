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
                            vendors | remote | verify | preflight | install | retry     (post-push checks and install)
                            devicecheck                                                  (verify without pushing)
                            inventory | credentials | history                            (batch sources, keyring, report)
  webui.py                  token-protected control panel for the servers (stdlib only, no Qt)
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

## Post-push pipeline

`ScpService.push` is: connect -> *(preflight)* -> transfer (SCP, or SFTP with optional resume) -> *(verify)* -> *(install)* ->
FINISHED. Each optional stage is switched on by a field of `ScpRequest`, reuses the already authenticated transport, and
fails the push with a specific message. `vendors.py` holds the per-platform command templates; `remote.py` runs them
(`run_exec` for one-shot commands, `Shell` for the interactive install session with prompt detection and error markers).
Install execution is refused without verification, and without `InstallOptions.confirmed`, which only a front-end sets after
the operator agreed (CLI prompt / GUI dialog), so no caller can change a device by accident. The boot-image and reload hooks
are independent; a scheduled reload is a per-vendor template (`reload_in`) and is refused where none exists. A `StateEvent(FAILED)` carries `transient`, set by `retry.is_transient`,
which `BatchRunner` uses for retries with exponential backoff (the failed attempt is not reported as a result until the
last attempt, so the UI never shows a premature failure). Jump hosts are an `SshOptions.jump` bastion whose `direct-tcpip`
channel carries the device connection; closing the device transport closes the bastion too.

## Vendor profiles

`vendors.py` is data, not code: a frozen `Vendor` per platform (verify/space/install/reload templates, parser names, the
transfer model `push|pull|web`, the shell model `cli|posix`, a hint for the wrong login shell). The only logic is in the
parsers (`preflight.parse_free_bytes`, `verify.parse_digest/evaluate_output`) and the script runner
(`remote.Shell.run_script`). Template lines beginning `@` are *gated answers*, sent only when `looks_like_question` says the
device is waiting; `!` aborts. Adding a platform = adding one `Vendor(...)` entry and its parser samples to the tests.
Profiles never hard-block a push to a pull-only platform (the operator may know better); they warn.

## Decisions

* **paramiko `>=4,<5`**: 5.0 removed SHA-1 kex and `ssh-rsa`; 4.x still offers them last, so no "legacy" switch is needed
  (proved against a legacy-only OpenSSH server in `tests/test_openssh.py`).
* **Audit log** lines carry a hash-chain tag (HMAC with a per-install key when it can be created). Tamper-*evident* only;
  see the user guide for what that does not cover. Older untagged lines are tolerated and reported as "legacy".
* **No hidden name lookups on hot paths.** `HTTPServer.server_bind` calls `socket.getfqdn`, a reverse-DNS lookup that stalled
  macOS CI runners for >10 s; all HTTP-based servers (including the web panel) use `_Server`, which binds without it. A test
  fixture (`hostile_dns`, or `NETTRANSFER_HOSTILE_DNS=1` for the whole suite, run as its own CI job) makes every name lookup
  stall so a regression fails deterministically.
* **Actions are pinned to commit SHAs** (a hygiene test enforces it); Dependabot updates the SHA and the `# vX.Y.Z` comment.
* **Data folder** resolves to the invoking user under `sudo` and hands created files back to them.
* **Passwords/passphrases are never persisted or logged**; CSV passwords are allowed but warned about.
* **macOS ships an `.app`** (PyInstaller discourages one-file windowed on macOS); Windows/Linux ship one file.
* **Packaged Linux build does not suggest `setcap`** (breaks one-file loaders); it suggests `sudo -E <executable>`.

## Known limits

* Vendor command templates (verify, space check, install) are untested against real devices; they were run against a scripted
  fake SSH device. The newest nine profiles (Aruba AOS-CX, PAN-OS, VyOS, pfSense/OPNsense, Ruijie, Sophos, Gaia, and Huawei/FortiOS
  additions) are built from vendor documentation and community references: expect to adjust prompts and output formats. Huawei and Fortinet have no standard verify command: pass `--verify-cmd`.
* Resume trusts that the shorter remote file is a prefix of the local one; only `--verify` proves it.
* IPv6 is HTTP(S) only (tftpy is IPv4); the IPv6 test is skipped on hosts without an IPv6 loopback.
* The web panel controls servers only (no uploads, no pushing), is single-token, and has no per-user accounts.
* The Push and Batch tabs expose pre-flight, verify, the boot/reload hooks, resume, inventories/filters and
  retries (through the shared `PushOptionsPanel`); jump hosts and keyring credentials are CLI-only. The Push tab's *Check device* runs `DeviceCheckService`, the same engine as
  `nettransfer verify`. The hook choices are
  session-only (never persisted) and every session starts as a dry run.
* The Junos free-space command is `show system storage /var` as specified; if a release rejects the argument the plain
  command is tried, and if that cannot be parsed the check is skipped (never blocks).

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
