# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Post-push verification** (`--verify`, `--verify-cmd`): the device's own MD5/SHA digest is compared with the local one.
- **Pre-flight checks** (`--preflight`): image-name sanity per vendor and a free-space check before sending.
- **Opt-in push-and-install** (`--install`, dry run by default; `--execute`, `--reload`), refused without verification.
  Vendor profiles: Cisco IOS/IOS-XE, NX-OS, Junos, Huawei VRP (templates; see the user guide).
- **Inventories**: Ansible YAML, Netmiko YAML/JSON and Zabbix JSON exports, CSV `vendor`/`tags` columns, and `--filter` tag
  expressions (`site:DCTT vendor:fortinet,juniper !group:lab`).
- **Retries with exponential backoff** for transient batch failures (`--retries`, `--backoff`) and SFTP/FTP **resume**.
- **Jump host** support for scp/sftp (`--jump`), and **OS keyring** credentials (`credentials`, `--use-keyring`).
- **Hash-chained audit log** with `nettransfer audit` (tamper detection) and `nettransfer history` (+ HTML report).
- **HTTPS**: serve your own certificate (`--cert`/`--key`); the SHA-256 fingerprint is logged. **IPv6** for HTTP(S).
- `--to DEVICE_IP` picks the interface that routes to a device; `interfaces` lists local addresses.
- **Web control panel** (`nettransfer web`): token-protected, serves only files from one directory.
- **Desktop app**: the Push and Batch tabs gained a *Device checks (SSH)* group (vendor, pre-flight, verify, custom verify
  command) and a *Resume* option; the Batch tab loads any inventory format, has a tag *Filter* box and a *Retries* setting
  and shows a `retrying` status. Choices are remembered; passwords never are.
- **Docker image** for the headless CLI (non-root) and a CI job that builds and smoke-tests it.
- CI: a job that runs the whole suite under a **stalling DNS resolver**, `pytest-timeout` for every test, and all GitHub
  Actions **pinned to commit SHAs** (enforced by a test; Dependabot now also watches the Dockerfile).

- **Arista EOS** vendor profile (`arista-eos`: `verify /sha256`, `dir flash:`, boot/reload) and **Junos free-space pre-flight**
  (`show system storage /var`, with a fallback), including a parser for Junos' human-readable sizes.
- **Command presets** for Arista EOS, MikroTik RouterOS, Aruba AOS-CX and Palo Alto PAN-OS (the SFTP/SCP *pull* forms carry a
  note that they need your own SSH server), in the TFTP/HTTP tabs' dropdown, `nettransfer commands` and `serve --commands`.
- **Verification result** is now `VERIFIED (hash match)` / `FAILED (checksum mismatch)` (shown in the Push status, Batch rows,
  log and audit trail); the comparison is a pure, tested function.
- **Boot-image and reload hooks** are independent and available in the Push and Batch tabs: set the boot variable, reload
  now, or schedule it (`reload in N` on Cisco IOS, Arista, Junos). Dry run by default; applying needs verification and an
  **explicit confirmation** (CLI prompt or `--yes`, GUI dialog); the core refuses to apply anything unconfirmed.
- **NetBox / Nautobot inventory** (`app/core/inventory.py`): filter by site, tag, role, platform, tenant; primary IP, hostname
  and platform -> vendor; `batch --netbox-url ...` and an **Import from NetBox...** dialog in the Batch tab. TLS verified by
  default, token header-only, redirects refused, pagination re-based onto the typed origin.

### Fixed
- HTTP(S) server no longer does a reverse-DNS lookup while binding, which could delay start-up by many seconds
  (and made the HTTP tests time out on macOS CI runners).

### Changed
- Audit log lines now end with a chain tag (`  ~<32 hex>`); older logs keep working and are reported as legacy lines.
- `--install --execute` now asks for confirmation (type `yes`; `--yes` for scripts). `--reload` no longer requires `--install`.
- Log wording: `Verified X on H: ...` became `VERIFIED (hash match): X on H - ...` (the history report reads both).
- `InstallOptions` gained `set_boot`, `reload_in` and `confirmed`; `plan()` takes the options object.
- `StateEvent` gained a `transient` flag (default `False`); `BatchTarget` gained `vendor` and `tags`.

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
