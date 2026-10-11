# Packaging: single executables for Windows, macOS and Linux

PyInstaller **cannot cross-compile**: a Windows `.exe` must be built on Windows, a macOS app on macOS, a
Linux binary on Linux. This repo therefore has one spec (`packaging/nettransfer.spec`), one driver
(`packaging/build.py`) and a CI matrix (`.github/workflows/build.yml`) that runs the driver on each OS.

## Build locally (on the OS you are targeting)

```bash
pip install -r requirements-dev.txt
python packaging/build.py            # builds, smoke-tests, collects into dist/release/
```

| OS | GUI | CLI (no Qt, ~17 MB) | Release file(s) in `dist/release/` |
|---|---|---|---|
| Windows | `NetTransfer.exe` - one file, no console window | `nettransfer-cli.exe` | `NetTransfer-<ver>-windows-x64.exe`, `nettransfer-cli-...exe` |
| macOS | `NetTransfer.app` | `nettransfer-cli` | `NetTransfer-<ver>-macos-<arch>.zip`, `nettransfer-cli-...tar.gz` |
| Linux | `nettransfer` - one file | `nettransfer-cli` | `NetTransfer-<ver>-linux-<arch>.tar.gz`, `nettransfer-cli-...tar.gz` |

`SHA256SUMS-<os>-<arch>.txt` is written next to them (the release workflow merges them into one `SHA256SUMS.txt`). The smoke test runs `nettransfer-cli --version`, `nettransfer-cli hash`
and `NetTransfer --selftest` (builds the whole GUI offscreen and touches every dynamically imported dependency),
so a broken bundle fails the build instead of a user's machine.

macOS ships an **.app bundle** rather than a literal single file: PyInstaller discourages one-file + windowed on
macOS (it would unpack to a temp dir at every launch). The zipped `.app` is the single thing to distribute.

## CI (all three at once)

Push a tag (`git tag -a v2.0.0 -m ... && git push origin v2.0.0`, see [RELEASING.md](RELEASING.md)) or run the *Build and release* workflow manually. Jobs: `windows-latest`,
`ubuntu-22.04` (oldest LTS runner, so the binary also runs on newer glibc), `macos-latest` (Apple Silicon) and
`macos-15-intel` (Intel). A tag also creates a GitHub Release with every file attached. Runner labels change over
time - check GitHub's runner-images list. For a single universal macOS binary set `TARGET_ARCH=universal2` (needs a
universal2 Python and universal wheels for every dependency; separate arm64/x86_64 builds are the safe default).

## Running the packaged app with TFTP (port 69)

UDP 69 is privileged on Linux/macOS. The app detects this and shows the exact command, naming its own path:

* **Linux:** `sudo -E ./nettransfer` (`-E` keeps `DISPLAY`/`XAUTHORITY` so the window can open). `setcap` is deliberately
  *not* suggested for the packaged build: file capabilities make the loader ignore `LD_LIBRARY_PATH`, which breaks a
  PyInstaller one-file binary.
* **macOS:** `sudo /Applications/NetTransfer.app/Contents/MacOS/NetTransfer` (a double-clicked app is not root).
* **Windows:** no elevation needed; allow the firewall prompt (or add the rule shown in the banner from an admin prompt).

Settings, known hosts, logs and `crash.log` live in a per-user folder, resolved for the *invoking* user even under sudo
(Windows `%APPDATA%\NetTransfer`, macOS `~/Library/Application Support/NetTransfer`, Linux `~/.config/nettransfer`).
Options > Open data folder jumps there.

## Signing, notarization, first-run warnings

Unsigned binaries work but trigger OS warnings. CI signs and notarizes automatically when the repository secrets listed in
[RELEASING.md](RELEASING.md#signing-and-notarization-optional-off-until-secrets-exist) exist (not exercised without certificates).
To do it by hand:

* **Windows SmartScreen:** sign `NetTransfer.exe` and `nettransfer-cli.exe` with `signtool sign /fd SHA256 /tr <timestamp-url> /td SHA256 /a <file>`
  (an EV or Azure Trusted Signing certificate removes the warning fastest). Unsigned: "More info" > "Run anyway".
* **macOS Gatekeeper:** set `CODESIGN_IDENTITY="Developer ID Application: ..."` before building (the spec passes it and
  `packaging/entitlements.plist` to PyInstaller), then notarize and staple:
  `xcrun notarytool submit NetTransfer-*.zip --apple-id ... --team-id ... --password ... --wait` then
  `xcrun stapler staple dist/NetTransfer.app` and re-zip. Unsigned: right-click > Open, or
  `xattr -dr com.apple.quarantine NetTransfer.app`. macOS 15+ also asks once for *Local Network* permission
  (the app declares why in its Info.plist).
* **Linux:** nothing to sign. Qt needs `libxcb-cursor0` at runtime on X11 sessions
  (`sudo apt install libxcb-cursor0`); the build machine's copy is bundled when present.

## Why paramiko is pinned `<5`

paramiko 5.0 removed SHA-1 key exchange and `ssh-rsa`, which older Cisco/Huawei/Juniper SSH servers still require.
4.x still offers them as the lowest-priority choice, so old gear connects without any "legacy mode" switch.
`tests/test_openssh.py` proves this against a real sshd restricted to `diffie-hellman-group14-sha1`, `ssh-rsa`,
`aes128-cbc` and `hmac-sha1`.

## Troubleshooting

* *Blank/instant exit on Windows:* the GUI has no console; read `%APPDATA%\NetTransfer\crash.log`, or run `nettransfer-cli`.
* *`ImportError` for a module at runtime:* add it to `hiddenimports` in the spec and rebuild; `--selftest` should catch it first.
* *Build is huge:* the spec already excludes unused Qt modules; Qt itself is ~60 MB of the GUI binary.
