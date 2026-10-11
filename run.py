#!/usr/bin/env python3
"""Entry point:  python run.py            -> GUI   (sudo on Linux/macOS for TFTP port 69)
               python run.py serve|push|batch|test|hash ...  -> headless CLI (see --help)

Preflight, in order: Python version -> dependencies -> OS detection -> port-69
capability probe -> launch the GUI. Problems are printed with the exact fix.
"""

from __future__ import annotations

import importlib.util
import os
import sys

MIN_PYTHON = (3, 9)
CLI_COMMANDS = {"serve", "push", "batch", "test", "hash", "-h", "--help", "--version"}
CLI_REQUIRED = {"tftpy": "tftpy", "paramiko": "paramiko", "scp": "scp"}
REQUIRED = {"PySide6": "PySide6", **CLI_REQUIRED}
OPTIONAL = {"psutil": "psutil (lets you choose between several network interfaces)"}
TFTP_PORT = 69


def _pause_if_windows_console() -> None:
    if os.name == "nt" and sys.stdin and sys.stdin.isatty():
        input("Press Enter to close...")


def _line_buffer_output() -> None:
    """Show messages immediately even when piped/redirected (PyInstaller builds ignore PYTHONUNBUFFERED).
    A windowed Windows build has no console: its streams are None."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(line_buffering=True)
            except (OSError, ValueError):
                pass


def main() -> int:
    _line_buffer_output()
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    frozen = bool(getattr(sys, "frozen", False))  # PyInstaller build: everything is bundled
    selftest = "--selftest" in sys.argv
    if selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # verifies the bundle, needs no display
    cli = len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS

    if sys.version_info < MIN_PYTHON:
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (found {sys.version.split()[0]}).")
        _pause_if_windows_console()
        return 1

    required = CLI_REQUIRED if cli else REQUIRED
    missing = [] if frozen else [pkg for pkg, mod in required.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(
            f"Missing required packages: {', '.join(missing)}\n"
            f"Install them with:\n  {sys.executable} -m pip install -r {os.path.join(here, 'requirements.txt')}"
        )
        _pause_if_windows_console()
        return 1
    if cli:
        from app.cli import main as cli_main

        return cli_main(sys.argv[1:])
    for mod, why in OPTIONAL.items():
        if not frozen and importlib.util.find_spec(mod) is None:
            print(f"Note: optional package {why} is not installed.")

    from app.bootstrap import run_gui
    from app.platform.detect import detect_platform
    from app.platform.network import default_route_ip
    from app.platform.privileges import build_startup_report

    info = detect_platform()
    report = build_startup_report(info, TFTP_PORT, default_route_ip())
    print(f"Network File Transfer - {info.describe()}")
    if not report.probe.ok:
        print(
            f"[!] Cannot bind UDP {report.probe_ip}:{TFTP_PORT} "
            f"({report.probe.reason or 'error'}): {report.probe.detail}"
        )
    for line in report.messages:
        print(f"    {line}")
    return run_gui(report, selftest=selftest)


if __name__ == "__main__":
    sys.exit(main())
