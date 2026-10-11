# Network File Transfer

[![CI](https://github.com/duc-mt/nettransfer/actions/workflows/ci.yml/badge.svg)](https://github.com/duc-mt/nettransfer/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/duc-mt/nettransfer?sort=semver)](https://github.com/duc-mt/nettransfer/releases/latest)
[![License: MIT](https://img.shields.io/github/license/duc-mt/nettransfer)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![Platforms](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)

**Zero-config desktop tool (and headless CLI) for getting files onto network devices** - firmware, configs and images
for switches, routers and firewalls - without setting up a TFTP daemon, an FTP server or hand-typing `scp` commands.

| Tab / command | What it does |
|---|---|
| **TFTP Server** | auto-detects your IP and serves the selected file to any device that asks (UDP 69) |
| **HTTP(S) Server** | the same over HTTP or HTTPS (self-signed), for `copy http://...` on Cisco / Junos |
| **Push** | sends a file to one device over **SCP, SFTP, FTP or FTPS** with a password or SSH key |
| **Batch Push** | one file to many devices from a CSV: parallel, per-device status, *Retry failed*, results export |

![TFTP server tab](docs/images/tftp.png)

<details>
<summary>More screenshots (sample data)</summary>

![Push tab](docs/images/push.png)
![Batch tab](docs/images/batch.png)

</details>

Every tab has a live progress bar, speed and a colour-coded log, a **Hash** button (MD5/SHA-256 to compare with the device's
own verify output), drag-and-drop, log export, remembered settings (never passwords), optional desktop notifications and an
audit log. The servers add copy-ready **device commands** for Cisco, Huawei, Fortinet and Junos, and **Safety** controls
(allowlist, stop after the first transfer, idle auto-stop) because TFTP/HTTP have no authentication.

## Install

**Download a ready-made single executable** from the [latest release](https://github.com/duc-mt/nettransfer/releases/latest)
(no Python needed):

| OS | File | Run |
|---|---|---|
| Windows | `NetTransfer-<version>-windows-x64.exe` | double-click |
| macOS | `NetTransfer-<version>-macos-arm64.zip` (Apple Silicon) or `-x86_64.zip` (Intel) | unzip, open `NetTransfer.app` |
| Linux | `NetTransfer-<version>-linux-x86_64.tar.gz` | `tar xzf ...`, then `./nettransfer` |

Each release also has `nettransfer-cli-*` (the headless CLI, no Qt) and `SHA256SUMS.txt` (see
[SECURITY.md](SECURITY.md#verifying-a-download) to verify). Binaries are not code-signed yet, so Windows SmartScreen and macOS
Gatekeeper warn on first launch: [docs/PACKAGING.md](docs/PACKAGING.md#signing-notarization-first-run-warnings) has the one-time steps.

**Or run from source** (Python 3.9+):

```bash
pip install -r requirements.txt
python run.py                 # Windows
sudo python3 run.py           # Linux / macOS: needed for TFTP port 69
```

### TFTP and privileges

UDP 69 is privileged on Linux and macOS. The app *probes* whether it can bind the port (not just "am I root") and, if not,
shows the exact command for your install: `sudo -E ./nettransfer`, `sudo /Applications/NetTransfer.app/Contents/MacOS/NetTransfer`,
or `sudo python3 run.py`. On Windows no elevation is needed, only a firewall allow. HTTP(S), SCP, SFTP and FTP use unprivileged ports.

## Quick start

1. **Device pulls the file (TFTP/HTTP):** choose the file, check the *Server IP*, press **Start server**, then run the shown
   *Device command* on the device (e.g. `copy tftp://10.0.0.5/fw.bin flash:fw.bin`). The file is served for any requested name.
2. **You push the file (SCP/SFTP/FTP):** open **Push**, pick the file, enter the device and credentials, **Push file**.
3. **Many devices:** open **Batch Push**, load a CSV ([`examples/targets.csv`](examples/targets.csv)), **Start batch**.

Full walkthrough, device recipes and troubleshooting: **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)**.

## Headless CLI

```bash
python run.py serve tftp fw.bin --once --allow 10.0.0.0/24      # or: nettransfer-cli serve ...
python run.py serve http image.tgz --https --idle 300
python run.py push sftp fw.bin 10.0.0.1 -u admin --key ~/.ssh/id_ed25519 --remote /var/tmp/
NT_PW=... python run.py batch scp fw.bin --targets examples/targets.csv -u admin --password-env NT_PW --results out.csv
python run.py test scp 10.0.0.1          # reachability + banner + host key, no login
python run.py hash fw.bin
```

Passwords are never accepted as arguments (they would show in `ps`): use `--password-env VAR` or the prompt.
Exit codes: `0` ok, `1` failure, `2` usage/CSV error, `130` interrupted.

## Security in one paragraph

TFTP, HTTP and FTP are unauthenticated and/or unencrypted by design, so run the servers on a management network, only while
needed, with the Safety options on. SSH host keys are trusted on first use and a *changed* key is refused until you accept it.
Passwords and passphrases are never stored or logged. Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Project

* [docs/USER_GUIDE.md](docs/USER_GUIDE.md) - usage, device recipes, troubleshooting
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - how it is built and why
* [docs/PACKAGING.md](docs/PACKAGING.md) - building the executables, signing, platform notes
* [docs/RELEASING.md](docs/RELEASING.md) - maintainers: cutting a release
* [CONTRIBUTING.md](CONTRIBUTING.md) - dev setup, tests, how to add things
* [CHANGELOG.md](CHANGELOG.md) - what changed

Built with [PySide6](https://doc.qt.io/qtforpython-6/), [tftpy](https://github.com/msoulier/tftpy),
[paramiko](https://www.paramiko.org/), [scp](https://github.com/jbardin/scp.py) and [PyInstaller](https://pyinstaller.org/).
Released under the [MIT License](LICENSE).
