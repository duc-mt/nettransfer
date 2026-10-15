# User guide

* [First run and privileges](#first-run-and-privileges)
* [TFTP and HTTP(S) servers: the device pulls the file](#tftp-and-https-servers-the-device-pulls-the-file)
* [Push: you send the file](#push-you-send-the-file)
* [Batch Push](#batch-push)
* [Command line](#command-line)
* [Device recipes](#device-recipes)
* [Troubleshooting](#troubleshooting)
* [Where your data lives](#where-your-data-lives)

## First run and privileges

Start the app (`NetTransfer.exe`, `NetTransfer.app`, `./nettransfer`, or `python run.py`). A banner appears on the TFTP tab
when UDP port 69 cannot be bound. This is the only part that needs elevated rights, and only on Linux/macOS:

| OS | Command |
|---|---|
| Linux (packaged) | `sudo -E ./nettransfer` (`-E` keeps `DISPLAY` so the window can open) |
| Linux (source) | `sudo -E python3 run.py` |
| macOS (packaged) | `sudo /Applications/NetTransfer.app/Contents/MacOS/NetTransfer` |
| Windows | no elevation; allow the Windows Firewall prompt, or add the rule shown in the banner from an admin prompt |

The probe really tries to bind the port, so a Linux system that already grants low ports to your user (capabilities or
`net.ipv4.ip_unprivileged_port_start`) shows no warning. HTTP(S) servers and all push protocols use unprivileged ports.

## TFTP and HTTP(S) servers: the device pulls the file

1. **File to serve** - pick any file (or drop it on the field). **Hash** prints MD5 and SHA-256 in the log, to compare with
   the device's own verify command.
2. **Server IP** - the default-route address is preselected. A laptop cabled straight to a switch has no default route, so
   pick the right NIC from the list and press **Rescan** after plugging a cable. The server binds that exact address.
3. **Safety** (TFTP and HTTP have no authentication):
   * *Stop after the first successful transfer* - ideal for a one-off upgrade.
   * *Stop when idle for N min* - protects against forgetting it is running.
   * *Allowlist* - comma-separated IPs or CIDRs, e.g. `10.20.0.0/24, 192.168.1.5`. Blocked requests are logged.
4. **Device command** - pick your vendor; the box shows the command with your IP, port and file name. **Copy** it, adjust the
   filesystem/VRF for your platform and release, and paste it on the device. *Verify* is the matching checksum command.
5. **Start server**, run the command, watch the progress bar and log. Closing the window stops the server.

The server is **read-only** and serves the selected file **for any requested name**, so you do not have to match file names.
For TFTP the log shows the negotiated block size; if it is 512 bytes the transfer will be slow, and the log says how to raise it
(e.g. Cisco IOS `ip tftp blocksize 8192`). For big images prefer **HTTP(S)**, which is much faster. HTTPS uses a throw-away
self-signed certificate: the device must be told to skip verification (and must support TLS 1.2+).

## Push: you send the file

1. Pick the **file** and enter the **device** (IP or hostname). **Test connection** checks reachability, banner and host-key
   status without logging in and sends nothing.
2. Choose the **protocol**: **SCP**, **SFTP**, **FTP** or **FTPS** (explicit TLS, certificate not verified).
3. Enter the **username** and **password** and/or an **SSH key** (encrypted keys need the passphrase; devices that only offer
   keyboard-interactive login work too). FTP with a blank username logs in anonymously.
4. **Advanced options**: port, **remote path** (blank = the device's default directory) with presets, and *Accept changed
   host key*. Presets are starting points: `flash:` (Cisco IOS), `bootflash:` (NX-OS), `/var/tmp/` (Junos), `/tmp/` (Linux).
5. For SCP and SFTP, open **Device checks (SSH)** to choose a **vendor** and turn on **Pre-flight** (stops before sending when the
   device reports too little free space) and **Verify afterwards** (compares the device's own MD5/SHA digest with the file's; a
   mismatch fails the push). A custom **verify command** such as `sha256sum {path}` overrides the vendor's; Huawei and Fortinet
   have no built-in one. **Resume a partial upload** (Advanced options, SFTP/FTP/FTPS) continues a shorter file already on the
   device. Under *After push* you can also **set the boot image** to the file and **reload** (scheduled in N minutes, or
   immediately); these start as a **dry run** (the plan is only shown). Untick *Dry run* to apply: you are asked to confirm
   first, and it needs *Verify afterwards*. See [Checks, installs and inventories](#checks-installs-and-inventories) for
   what each does and its limits.
6. **Push file**; **Cancel** aborts, including a stuck connection.

**Host keys** are trusted on first use and saved. If a device later presents a *different* key the push is refused with a clear
message; if you replaced or reset the device, tick *Accept changed host key* once. Nothing is sent before the key is checked.

Device prerequisites: Cisco IOS needs `ip scp server enable` and a privilege-15 user for SCP. Older SSH servers that only offer
SHA-1 key exchange and `ssh-rsa` work without any switch.

## Batch Push

1. Pick the **file**, then a **targets file** and press **Load targets**. Besides the CSV below it accepts an Ansible or Netmiko
   YAML/JSON inventory and a Zabbix hosts export (see [Inventories and filters](#inventories-and-filters)); the optional
   **Filter** box keeps only matching devices, e.g. `site:DCTT vendor:fortinet,juniper !group:lab` (hover a device to see its
   tags). A filter that matches nothing leaves the current list untouched. CSV format:

   ```csv
   host,username,remote_path,port
   10.10.1.1,,flash:,
   10.10.1.2,ops,flash:,2222
   ```

   Only `host` is required (columns: `host, username, password, remote_path, port, vendor, tags`; see
   [`examples/targets.csv`](../examples/targets.csv)). Blank cells use the **defaults** below the file fields. Any invalid row
   aborts the load with a list of problems, because silently skipping a device in a rollout is worse than stopping. A CSV
   that contains passwords works but is flagged: prefer a shared password or an SSH key.
2. Set **Parallel devices** (default 4; they share your NIC's bandwidth) and **Retries** (default 0: extra attempts for
   timeouts, resets and failed verifications, with doubling delays; wrong passwords and changed host keys are never retried).
   **Device checks (SSH)** work as on the Push tab; when every device in the inventory carries its own vendor, *Verify* needs
   no default vendor.
3. **Start batch**. The table shows each device (`queued`, `running` with a percentage, `retrying` with the wait, `done`,
   `failed` with the reason, `cancelled`). **Cancel** stops running pushes and marks the rest cancelled.
4. **Retry failed** re-runs only the failed/cancelled devices. **Export results...** writes `host,status,detail` as CSV.

One failing or crashing device never stops the others.

## Command line

The same services without Qt, for jump hosts and scripts. From source use `python run.py <command>`; the packaged CLI is
`nettransfer-cli <command>`.

| Command | Purpose |
|---|---|
| `commands tftp\|http\|https FILE [--ip IP \| --to DEVICE_IP] [--port N] [--vendor TEXT]` | print copy-ready device commands (Cisco, Juniper, Arista, MikroTik, Aruba, Palo Alto, ...) |
| `serve tftp\|http FILE [--ip IP\|all \| --to DEVICE_IP] [--port N] [--https [--cert PEM --key PEM]] [--once] [--idle SECONDS] [--allow CIDRS] [--commands [VENDOR]]` | serve a file (`--commands` prints the matching device commands first) |
| `push scp\|sftp\|ftp\|ftps FILE HOST -u USER [--password-env VAR] [--key PATH] [--key-passphrase-env VAR] [--remote PATH] [--port N] [--accept-changed-key]` | push to one device |
| `batch PROTO FILE (--targets CSV \| --inventory FILE [--inventory-format F]) [--filter EXPR] [--workers N] [--retries N] [--backoff S] [--results CSV]` (+ the push options) | push to many |
| `test PROTO HOST [--port N]` | reachability, banner, host-key status; no login |
| `hash FILE` | MD5 and SHA-256 |
| `interfaces [--ipv6] [--to DEVICE_IP]` | local addresses; which one routes to a device |
| `credentials set\|delete HOST USER` | device password in the OS keyring (`HOST` may be `*`) |
| `audit [FILES] [--expect-head TAG]` | verify the audit log's hash chain |
| `history [--device T] [--file T] [--html OUT]` | which device got which file, when |
| `web --files DIR [--bind IP] [--port N] [--https]` | token-protected browser control panel |

Passwords are never accepted on the command line. Use `--password-env NAME` or answer the prompt. Exit codes: `0` success,
`1` failure (any device failed in a batch), `2` usage or CSV error, `130` interrupted with Ctrl-C.

## Checks, installs and inventories

Pre-flight, verification, the boot-image and reload hooks, retries, resume and inventories are available in the **Push and
Batch tabs** as well as on the command line; jump hosts and keyring credentials are **CLI only**. Everything is
**opt-in**: with nothing ticked a push behaves exactly as before. The table below uses the command-line flags.
`--vendor` selects a profile (`cisco-ios`, `cisco-nxos`, `arista-eos`, `junos`, `huawei`, `fortinet`, `linux`) that knows the device's
verify command, where a blank remote path lands, and what a software image looks like. The commands in a profile are
*templates*: syntax differs between platforms and releases, so check them against your devices (and override with
`--verify-cmd`) before relying on them in a change window. They were exercised here against a scripted fake device, not
against real hardware.

| Option | What it does |
|---|---|
| `--preflight` | before sending: warns if the file name does not look like an image for the vendor, and **stops** if the device reports less free space than the file needs (when the reply cannot be read it carries on and says so) |
| `--verify` / `--verify-cmd 'sha256sum {path}'` | after sending: runs the device's digest command over SSH and compares with the local digest; a mismatch fails the push (and is retried in a batch). Needs `--vendor` or `--verify-cmd`; scp/sftp only |
| `--install` (alias `--set-boot`) | after a verified push, **set the boot image** to the file (e.g. Cisco `boot system flash:fw.bin`). Without `--execute` this is only a **dry run** that prints the commands |
| `--reload` / `--reload-in MINUTES` | **reload** the device now, or schedule it (`reload in 5`; Cisco IOS, Arista, Junos). Independent of `--install`; NX-OS, Huawei and Fortinet have no scheduled form here and are refused before anything is uploaded |
| `--execute` | really apply the `--install` / `--reload` plan. Refused unless `--verify` is on, so a corrupt upload can never become the boot image |
| `--yes` | with `--execute`: skip the confirmation prompt. Without it the CLI asks you to type `yes` and refuses to run non-interactively; the desktop app shows a confirmation dialog. Dry runs never ask |
| `--retries N --backoff S` | batch: retry timeouts, resets and failed verifications with doubling delays; never retries bad credentials or a changed host key |
| `--resume` | sftp/ftp/ftps: continue a shorter partial remote file instead of starting over (combine with `--verify`: the digest proves the stitched file is right) |
| `--jump [user@]host[:port]` | reach the device through an SSH bastion; `--jump-key`, `--jump-password-env`. scp/sftp only (FTP needs a second data connection) |
| `--use-keyring` | take the password from the OS keyring (`nettransfer credentials set HOST USER`); never written to NetTransfer's own files |

File names are placed into device commands, so a name containing anything other than letters, digits and `. _ / : @ % + = , ~ -`
is refused for `--verify`/`--install`/`--preflight` rather than escaped.

### Verification result

After the transfer the device's own checksum is compared with the one computed locally (MD5 or SHA-256, whichever the
device's command prints). The outcome is one of:

* **`VERIFIED (hash match)`**: shown in the Push status, in each Batch row's *Detail* column, in the log and the audit
  trail (and the history report);
* **`FAILED (checksum mismatch)`**: the device returned a digest and it differs, so the file on it is corrupt; the push fails
  and, in a batch with retries, is sent again;
* **`FAILED (verification error)`**: the device's reply could not be read, no command is known for it, or a custom command
  is malformed.

### Supported platforms

`--vendor` (and the *Vendor* dropdown) picks a profile. Everything marked *template* is documented-syntax from vendor
manuals and community references and was **exercised against a scripted fake device, never real hardware**: try a dry run
and one lab device before relying on it.

| Profile (aliases) | Takes a push? | Integrity check | Free-space check | Boot / reload hooks |
|---|---|---|---|---|
| `cisco-ios` (`cisco`) | yes | `verify /md5 flash:FILE` | `dir flash:` | boot var, reload, `reload in N` |
| `cisco-nxos` | yes | `show file bootflash:FILE md5sum` | `dir bootflash:` | boot var, reload |
| `arista-eos` (`arista`) | yes | `verify /sha256 flash:FILE` | `dir flash:` | boot var, reload, `reload in H:MM` |
| `junos` (`juniper`) | yes | `file checksum md5 FILE` | `show system storage /var` | software add, reboot, `reboot in N` |
| `huawei` (`vrp`) | yes (SCP/SFTP server must be on) | `check system-software FILE` (answers its Y/N; SHA256) | `dir flash:` | `startup system-software`, `reboot` |
| `fortinet` (`fortigate`) | **no: device pulls** | none exists: `verify --expect-version` | free RAM only, advisory | `execute reboot` |
| `aruba-aos-cx` (`aruba`) | **no: device pulls** | `show images` (SHA-256 of the **primary** slot) | none (slot based) | `boot set-default primary`, `boot system primary` |
| `paloalto-panos` (`paloalto`) | **no: device pulls** | none exists: `verify --expect-version` | `show system disk-space` (`/opt/panrepo`) | `request system software install` (no auto reload) |
| `ruijie-rgos` (`ruijie`) | **no: device pulls** | none documented: `verify --expect-version` | best effort | `reload` |
| `vyos` | yes | `sha256sum FILE` | `df -Pk` | `add system image`, `reboot`, `reboot in N` |
| `pfsense`, `opnsense` | yes (packages, files) | `sha256 FILE` | `df -Pk` | `pkg add`, `shutdown -r now / +N` |
| `checkpoint-gaia` (`checkpoint`, `gaia`) | yes, **bash login shell required** | `sha256sum FILE` | `df -Pk` | CPUSE import, `reboot`, `shutdown -r +N` |
| `sophos-sfos` (`sophos`) | **no: web admin only** | `sha256sum` from the Advanced Shell only | `df -Pk` (same) | none |
| `linux` | yes | `sha256sum FILE` | `df -Pk` | none |

Things the table cannot show:

* **Pull platforms.** FortiOS, AOS-CX, PAN-OS and RGOS fetch their own image (`execute restore image tftp ...`,
  `copy tftp://... primary`, `scp import software from ...`, `upgrade download tftp://...`). Serve the file
  (`serve tftp` / `serve http`, `commands` prints the exact line), run that command on the device, then confirm with
  **`nettransfer verify`** below. A push to one of them still works if you insist, but warns first.
* **No hash exists** on FortiOS, PAN-OS and RGOS (they validate the image themselves while installing). Rather than invent
  one, NetTransfer checks the **running version** (`--expect-version`) and reports it as `VERIFIED (version match)`, which
  proves what is running, not that a file is intact.
* **Huawei** `check system-software` prints a SHA256 on current VRP releases, but what it hashes may differ by release.
  If a correct file reports a mismatch, validate on your device and use `--verify-cmd` instead of trusting it blindly.
* **Aruba** `show images` lists both slots; the digest after the *Primary Image* heading is used, never the other slot's.
* **PAN-OS** installs run as a background job, so no automatic restart is attempted: watch `show jobs all`, then
  `request restart system`. The version comes from the file name (`PanOS_220-10.1.11-h1` -> `10.1.11-h1`).
* **Fortinet's free-space figure** is free RAM from `get system performance status` (FortiOS shows no flash-free number); a
  low value only warns, it never blocks.
* **Ruijie** `dir` output is not documented anywhere reliable: only figures with an explicit unit are trusted, otherwise the
  check is skipped.

#### Shells: vendor CLI or POSIX

Some platforms end in a real shell, others in a restricted CLI; commands sent over SSH behave differently.

* **VyOS** runs `sha256sum` / `df` directly, but its `add system image`, `reboot` and `show version` are op-mode commands, so
  they are typed into an interactive session; the installer's questions are answered with their **defaults**.
* **pfSense / OPNsense** SSH normally lands in a *console menu*, not a shell. Log in as root (or give the user `/bin/sh`)
  so commands run directly; NetTransfer detects the menu and tells you.
* **Check Point Gaia** logs in to **Clish** by default, which rejects `sha256sum` and breaks SCP. Run
  `set user USER shell /bin/bash` then `save config` (Spark: `bashUser on`). Clish actions are wrapped as `clish -c "..."`.
* **Sophos SFOS** SSH opens a console menu (Device Management -> Advanced Shell); firmware goes through the web admin.

#### Confirmations are never blind

Reload and install steps often end in a question (`Proceed with reload? [confirm]`). NetTransfer answers **only if the
device actually asked**, and only with the profile's answer, because on a POSIX shell (VyOS, FreeBSD, Gaia) a stray `yes`
would run the `yes` program. A save-the-configuration question during a reload is **never** answered for you: the step
stops and tells you to save or discard the configuration yourself.

### `nettransfer verify`: check a device without pushing

```bash
# after a FortiGate pulled and installed an image
python run.py verify 10.0.0.1 --vendor fortinet -u admin --password-env FG_PW --expect-version 7.2.4
# an Aruba switch that copied an image into its primary slot
python run.py verify 10.0.0.2 fw.swi --vendor aruba -u admin --password-env CX_PW
# both a digest and a version, through a bastion
python run.py verify 10.0.0.3 fw.tgz --vendor checkpoint --expect-version R81.20 --jump ops@bastion
```

Exit status is 0 only if every requested check passed. A file digest needs a platform with a hash command (or
`--verify-cmd`); `--expect-version` works wherever a version command is known.

### Device command presets

The **TFTP** and **HTTP(S)** tabs have a *Command for the device* dropdown, and `nettransfer commands` / `serve --commands`
print the same lines for scripts. Besides Cisco, Juniper, Huawei and Fortinet it now has:

| Vendor | Command |
|---|---|
| Arista EOS | `copy tftp://IP/FILE flash:` / `copy http(s)://IP:PORT/FILE flash:` (+ `verify /sha256 flash:FILE`) |
| MikroTik RouterOS | `/tool fetch url="http(s)://IP:PORT/FILE" mode=http(s)` (HTTPS with the self-signed certificate needs `check-certificate=no`) |
| Aruba AOS-CX | `copy tftp://IP/FILE primary` |
| Aruba AOS-CX (SFTP pull) | `copy sftp://USER@IP/FILE primary` |
| Palo Alto PAN-OS (SCP pull) | `scp import software from USER@IP:FILE` |

The two *pull* entries make the **device** fetch over SSH from a server that must run on your machine: NetTransfer serves
TFTP and HTTP(S) only, so they are shown with a note, and you replace `USER` and adjust the path yourself. All templates
are starting points: syntax varies by software release.

### Inventories and filters

`batch --inventory FILE` reads a batch CSV (extra columns `vendor` and `tags`), an **Ansible** YAML inventory (groups become
`group:<name>` tags; `ansible_host`/`ansible_user`/`ansible_port`/`ansible_network_os`), a **Netmiko** YAML/JSON device list, or a
**Zabbix** *Export hosts* JSON file. INI-style Ansible inventories are not supported. YAML needs PyYAML
(`pip install -r requirements-extras.txt`; the Docker image has it).

`--filter` takes space-separated terms that must **all** match; inside a term `,` means *or*, `!` negates, `*` is a wildcard:
`site:DCTT vendor:fortinet,juniper !group:lab host:10.1.*`. Check a selection first with `--list-targets`.

## Audit trail, history and the web panel

Every audit line ends with a chain tag (SHA-256, or HMAC-SHA-256 with the key in `audit.key`) over the previous tag and the line.
`nettransfer audit` re-computes the chain and reports the first edited, removed or inserted line. This is **tamper-evident, not
tamper-proof**: someone who can read `audit.key` can rebuild a consistent chain, and the end of a file can be cut off without
breaking what remains. Paste the printed `head` into the change ticket and verify later with `audit --expect-head TAG`; that
closes both gaps. The CLI writes audit lines only with `--audit`; the desktop app always does.

`nettransfer history` turns the logs into "which device got which file, when, and did it verify"; `--html OUT` writes a single
self-contained page (no scripts, no external requests).

`nettransfer web --files DIR` starts a browser panel to start/stop TFTP and HTTP(S) servers on a shared lab host. It requires a
token (generated and shown once, or `--token-env VAR`), serves only files that sit directly in `DIR`, has no uploads, binds to
loopback unless you say otherwise, and warns if you bind further without `--https`. It controls the *serving* side only;
pushing stays in the CLI and desktop app.

## Containers

`docker build -t nettransfer-cli .` builds a Qt-free image that runs as a non-root user. Use `--network host` (or publish the
port) so devices can reach a server, and for TFTP add `--sysctl net.ipv4.ip_unprivileged_port_start=0` or pick `--port 6969`.
Mount a volume at `/home/nettransfer/.config/nettransfer` to keep `known_hosts` and audit logs between runs.

## IPv6 and interfaces

`serve http` accepts an IPv6 address (`--ip ::1`, or `--ip ::` for dual stack); TFTP is IPv4 only (the TFTP engine has no IPv6).
`--to DEVICE_IP` binds the interface the operating system would use to reach that device, which is the easy way to pick the
right NIC on a multi-homed laptop; `nettransfer interfaces --to DEVICE_IP` shows the choice without serving anything.
`serve http --https --cert PEM [--key PEM]` serves your own certificate; the SHA-256 fingerprint is always logged so a device
can pin it.

## Device recipes

Templates to adapt, not guarantees: filesystem names, VRFs and syntax vary by platform and release. The app fills them in
for you in the *Device command* box.

| Device | Pull from TFTP | Pull from HTTP(S) | Verify |
|---|---|---|---|
| Cisco IOS / IOS-XE | `copy tftp://IP/FILE flash:FILE` | `copy http://IP:PORT/FILE flash:FILE` | `verify /md5 flash:FILE` |
| Cisco NX-OS | `copy tftp://IP/FILE bootflash:FILE vrf management` | `copy http://IP:PORT/FILE bootflash:FILE vrf management` | `show file bootflash:FILE md5sum` |
| Juniper Junos | (use SCP/HTTP) | `file copy http://IP:PORT/FILE /var/tmp/FILE` | `file checksum md5 /var/tmp/FILE` |
| Huawei VRP | `tftp IP get FILE` (user view) | - | - |
| Fortinet FortiGate | `execute restore image tftp FILE IP` | - | - |

Push (SCP/SFTP) works with any device that runs an SSH server with SCP/SFTP enabled. Seen working remote paths:
`flash:` (Cisco IOS), `bootflash:` (NX-OS), `/var/tmp/` (Junos). Please report what works on your platform with the
*Device compatibility report* issue form so the presets and templates improve.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| *Permission denied binding ...:69* | Not privileged: see [First run](#first-run-and-privileges). |
| *Port ... is already in use* | Another TFTP/HTTP server runs (tftpd-hpa, dnsmasq, Tftpd64, SolarWinds ...). Stop it or use another port (HTTP). |
| Device times out fetching the file | Wrong NIC/IP selected; firewall (Windows: allow the prompt; Linux: `ufw`/`firewalld` UDP 69); device and PC on different VLANs; allowlist excludes the device. |
| TFTP is very slow | 512-byte blocks. Raise the block size on the device (see the log tip) or use **HTTP(S)**. |
| *Authentication failed* | Credentials; for Cisco IOS also `ip scp server enable` and a privilege-15 user. |
| *SSH negotiation failed* | Read the message; the device offers no algorithm in common. Report it with the device model. |
| *The host key ... has CHANGED* | The device was replaced/reset, or a man-in-the-middle. Verify, then tick *Accept changed host key*. |
| *The private key is encrypted* / *wrong passphrase* | Enter the key's passphrase. |
| *SCP error / refused the request* | Remote path or write permission; SCP/SFTP/FTP not enabled on the device. |
| HTTPS fails on the device | The device rejects self-signed certificates or needs older TLS. Use plain HTTP. |
| Linux: window does not open under `sudo` | `sudo -E ...` (keeps `DISPLAY`/`XAUTHORITY`), or on Wayland run via XWayland. |
| Linux: Qt `xcb` plugin error | `sudo apt install libxcb-cursor0` (and `libxkbcommon-x11-0`). |
| macOS: app "can't be opened" | Unsigned build: right-click > Open, or `xattr -dr com.apple.quarantine NetTransfer.app`. |
| Windows: SmartScreen warning | Unsigned build: *More info > Run anyway*. |
| Windows: nothing happens at launch | The GUI has no console: read `crash.log` in the data folder, or run `nettransfer-cli`. |

## Where your data lives

Per-user folder (resolved for the *invoking* user even under `sudo`): Windows `%APPDATA%\NetTransfer`, macOS
`~/Library/Application Support/NetTransfer`, Linux `~/.config/nettransfer`. **Options > Open data folder** jumps there.

| File | Contains |
|---|---|
| `settings.json` | last file, interface, protocol, user name, remote path, options. **Never passwords or passphrases.** |
| `known_hosts` | trusted SSH host keys (OpenSSH format, mode `0600` on POSIX) |
| `logs/nettransfer-YYYYMMDD.log` | hash-chained audit trail of every transfer (switch off in *Options*) |
| `audit.key` | random HMAC key for the audit chain (mode `0600`); losing it only means `audit` can no longer verify old files |
| `crash.log` | unexpected errors, if any |
