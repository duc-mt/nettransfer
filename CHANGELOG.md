# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- HTTP(S) server no longer does a reverse-DNS lookup while binding, which could delay start-up by many seconds
  (and made the HTTP tests time out on macOS CI runners).

## [2.0.0] - 2026-10-11

First public release.

### Added
- **TFTP server** tab: auto-detected local IP, serves the selected file for any requested name (read-only), live
  progress/speed/log, negotiated block size logged with a tip when it is stuck at 512 bytes.
- **HTTP(S) server** tab for `copy http://...` on Cisco / Junos, with a throw-away self-signed certificate for HTTPS.
- **Push** tab: SCP, SFTP, FTP and FTPS to one device; password or SSH key (encrypted keys, keyboard-interactive
  fallback); remote-path presets; *Test connection* (reachability, banner and host-key status, no login).
- **Batch Push** tab: one file to many devices from a CSV, in parallel, per-device status, *Retry failed*, results export.
- **Safety** controls for the unauthenticated servers: stop after the first successful transfer, idle auto-stop,
  client IP/CIDR allowlist.
- **SSH host keys** use trust-on-first-use; a changed key is refused until explicitly accepted.
- Copy-ready **device command templates** (Cisco IOS/IOS-XE and NX-OS, Huawei VRP, Fortinet, Junos) filled in with the
  server IP, port and file name, plus MD5/SHA-256 for comparison with the device's verify output.
- **Headless CLI**: `serve`, `push`, `batch`, `test`, `hash` (the same services without Qt).
- Cross-platform start-up checks: OS detection, a real bind probe of UDP 69 with the exact fix per OS and install type.
- Drag-and-drop, remembered settings (never passwords), audit log, log export, optional desktop notifications.
- Single-file executables for Windows, macOS and Linux built by CI (PyInstaller), with a GUI `--selftest` smoke test.

### Security
- paramiko is pinned `>=4,<5`: 5.0 dropped SHA-1 key exchange and `ssh-rsa`, which older network devices still require.
- Passwords and key passphrases are never logged or stored; the CLI never accepts a password as an argument.
- The HTTP server does not use `SO_REUSEADDR` on Windows, where it would let a second process take over a bound port.

[Unreleased]: https://github.com/duc-mt/nettransfer/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/duc-mt/nettransfer/releases/tag/v2.0.0
