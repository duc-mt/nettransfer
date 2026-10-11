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
5. **Push file**; **Cancel** aborts, including a stuck connection.

**Host keys** are trusted on first use and saved. If a device later presents a *different* key the push is refused with a clear
message; if you replaced or reset the device, tick *Accept changed host key* once. Nothing is sent before the key is checked.

Device prerequisites: Cisco IOS needs `ip scp server enable` and a privilege-15 user for SCP. Older SSH servers that only offer
SHA-1 key exchange and `ssh-rsa` work without any switch.

## Batch Push

1. Pick the **file**, then a **targets CSV** and press **Load targets**:

   ```csv
   host,username,remote_path,port
   10.10.1.1,,flash:,
   10.10.1.2,ops,flash:,2222
   ```

   Only `host` is required (columns: `host, username, password, remote_path, port`; see
   [`examples/targets.csv`](../examples/targets.csv)). Blank cells use the **defaults** below the file fields. Any invalid row
   aborts the load with a list of problems, because silently skipping a device in a rollout is worse than stopping. A CSV
   that contains passwords works but is flagged: prefer a shared password or an SSH key.
2. Set **Parallel devices** (default 4). They share your NIC's bandwidth.
3. **Start batch**. The table shows each device (`queued`, `running` with a percentage, `done`, `failed` with the reason,
   `cancelled`). **Cancel** stops running pushes and marks the rest cancelled.
4. **Retry failed** re-runs only the failed/cancelled devices. **Export results...** writes `host,status,detail` as CSV.

One failing or crashing device never stops the others.

## Command line

The same services without Qt, for jump hosts and scripts. From source use `python run.py <command>`; the packaged CLI is
`nettransfer-cli <command>`.

| Command | Purpose |
|---|---|
| `serve tftp\|http FILE [--ip IP] [--port N] [--https] [--once] [--idle SECONDS] [--allow CIDRS]` | serve a file |
| `push scp\|sftp\|ftp\|ftps FILE HOST -u USER [--password-env VAR] [--key PATH] [--key-passphrase-env VAR] [--remote PATH] [--port N] [--accept-changed-key]` | push to one device |
| `batch PROTO FILE --targets CSV [--workers N] [--results CSV]` (+ the push options) | push to many |
| `test PROTO HOST [--port N]` | reachability, banner, host-key status; no login |
| `hash FILE` | MD5 and SHA-256 |

Passwords are never accepted on the command line. Use `--password-env NAME` or answer the prompt. Exit codes: `0` success,
`1` failure (any device failed in a batch), `2` usage or CSV error, `130` interrupted with Ctrl-C.

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
| `logs/nettransfer-YYYYMMDD.log` | audit trail of every transfer (switch off in *Options*) |
| `crash.log` | unexpected errors, if any |
