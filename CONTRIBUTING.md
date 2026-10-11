# Contributing

Thanks for helping! Bug reports, device-compatibility reports, docs fixes and code are all welcome. By taking part you
agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems: see [SECURITY.md](SECURITY.md), not a public issue.

## The most useful contribution: a device report

Tell us what works. Use the **Device compatibility report** issue form: vendor, model, software version, protocol, the
exact command or remote path that worked. Those feed the built-in command templates and remote-path presets
(`app/core/device_hints.py`).

## Development setup

```bash
git clone https://github.com/duc-mt/nettransfer.git && cd REPO   # the folder git created
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pre-commit install                                        # ruff + hygiene checks on every commit
```

Python 3.9+ is supported (CI runs 3.9 - 3.13 on Linux, 3.12 on Windows and macOS). On Linux the Qt tests need a few system
libraries (`libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1`); they run without a display.

| Task | Command |
|---|---|
| Run the GUI | `./run.sh` / `run.bat` (or `python run.py`, `sudo` on Linux/macOS for TFTP port 69) |
| Tests | `QT_QPA_PLATFORM=offscreen pytest -q` |
| Coverage | `QT_QPA_PLATFORM=offscreen pytest -q --cov --cov-report=term-missing` |
| Lint + format check | `ruff check . && ruff format --check .` (`ruff check --fix . && ruff format .` applies fixes) |
| Build binaries for this OS | `python packaging/build.py` (see [docs/PACKAGING.md](docs/PACKAGING.md)) |
| Release consistency | `python scripts/check_version.py` |

## How the code is organised

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first. The rules that matter:

* **`app/core` never imports Qt.** Services report through an `EventSink` callable, so they are tested with `list.append`
  and reused by the CLI. Qt appears only in `app/ui` (and `bootstrap.py`).
* **Dependencies are injected** (service factories, stores, probes) through constructors; no hard-coded globals.
* **Never touch widgets from a worker thread.** Connect worker signals to real `QObject` slots, not lambdas.
* **Secrets never reach logs, settings, the audit file or repr().** Use `field(repr=False)` for passwords/passphrases.
* Prefer small, targeted changes that follow the pattern already used nearby.

## Tests

Every behaviour change needs a test. There is no need for real devices: the suite uses real sockets, an in-process SSH/SCP
server (`tests/conftest.py`), a real FTP server (pyftpdlib), a real OpenSSH `sshd` on Linux, and offscreen Qt for the tabs.

* Core logic: a unit test next to the closest existing one (`tests/test_*.py`).
* A tab or widget: `tests/test_ui_integration.py`. Wait with the `wait_until` helper (it pumps events and sleeps; do not
  use `QTest.qWait`, which holds the GIL and starves the worker threads). Register widgets with the `track` fixture.
* Anything that could differ per OS: think about Windows and macOS, and guard POSIX-only assertions.

## Common changes

* **A new device command template**: add a `Hint` to `TFTP_HINTS` / `HTTP_HINTS` in `app/core/device_hints.py` and a case in
  `tests/test_serve_features.py`. Only add syntax you have verified on the platform and say which release.
* **A new push protocol**: a service with `push(request)` / `cancel()` in `app/core/`, a request dataclass, extend
  `PushDefaults.build` and `make_service_factory` in `app/core/requests.py`, add it to `PROTOCOLS`, then the tests. The Push
  and Batch tabs and the CLI pick it up through those two functions.
* **A new tab**: subclass `ServerTab` for "device pulls the file" services; otherwise follow `PushTab`. Wire it in `bootstrap.py`
  and implement `save_settings()` and `shutdown()`.

## Pull requests

1. Branch from `main`; keep a PR to one concern.
2. Lint and test checks (`ruff check .`, `pytest`) must pass; CI also runs Windows, macOS, a launch self-test and CodeQL.
3. Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
4. Update the docs you affected. Do not commit secrets, real device IPs or customer data (also in screenshots and logs).
5. Label the PR (`feature`, `bug`, `docs`, `breaking`, `device-compat`, `skip-changelog`); the labels shape generated release notes.

Commit messages: a short imperative summary (`Fix SFTP target when the remote path is a directory`); the body explains *why*.
[Conventional Commits](https://www.conventionalcommits.org) prefixes are welcome but not required.

## Releases

Maintainers: see [docs/RELEASING.md](docs/RELEASING.md).
