#!/usr/bin/env python3
"""Build the single-file release artifacts for the CURRENT operating system.

    python packaging/build.py                  # build + smoke-test + collect into dist/release/
    python packaging/build.py --no-test        # skip the smoke tests
    python packaging/build.py --stage build    # PyInstaller + smoke tests only   } CI runs the two stages
    python packaging/build.py --stage collect  # archive + checksums only         } separately so it can sign in between

Requires:  pip install -r requirements-dev.txt
Output (dist/release/):  <name>-<version>-<os>-<arch>[.zip|.tar.gz|.exe]  + SHA256SUMS-<os>-<arch>.txt
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402

IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
OS_NAME = "windows" if IS_WIN else "macos" if IS_MAC else "linux"
ARCH = os.environ.get("TARGET_ARCH") or {
    "AMD64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}.get(platform.machine(), platform.machine())
DIST, WORK, RELEASE = ROOT / "dist", ROOT / "build", ROOT / "dist" / "release"


def run(cmd, **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def ensure_icons() -> None:
    if not all((ROOT / "assets" / n).exists() for n in ("icon.png", "icon.ico", "icon.icns")):
        run([sys.executable, ROOT / "packaging" / "make_icons.py"])


def pyinstaller() -> None:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            DIST,
            "--workpath",
            WORK,
            ROOT / "packaging" / "nettransfer.spec",
        ]
    )


def smoke_test() -> None:
    cli = DIST / ("nettransfer-cli.exe" if IS_WIN else "nettransfer-cli")
    out = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or __version__ not in out.stdout:
        sys.exit(f"CLI smoke test failed: rc={out.returncode} out={out.stdout!r} err={out.stderr!r}")
    digest = subprocess.run(
        [str(cli), "hash", str(ROOT / "assets" / "icon.png")], capture_output=True, text=True, timeout=120
    )
    if digest.returncode != 0 or "SHA256" not in digest.stdout:
        sys.exit(f"CLI hash smoke test failed: {digest.stdout!r} {digest.stderr!r}")
    gui = (
        DIST / "NetTransfer.app" / "Contents" / "MacOS" / "NetTransfer"
        if IS_MAC
        else DIST / ("NetTransfer.exe" if IS_WIN else "nettransfer")
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    # the selftest builds the whole GUI offscreen and touches every dynamically-imported dependency
    result = subprocess.run([str(gui), "--selftest"], capture_output=True, text=True, timeout=180, env=env)
    if result.returncode != 0 or (not IS_WIN and "selftest ok" not in result.stdout):
        sys.exit(f"GUI selftest failed: rc={result.returncode}\n{result.stdout}\n{result.stderr}")
    print("smoke tests passed (CLI --version/hash, GUI --selftest)")


def tar_gz(src: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src, arcname=src.name)  # keeps the executable bit


def collect() -> list[Path]:
    shutil.rmtree(RELEASE, ignore_errors=True)
    RELEASE.mkdir(parents=True)
    tag = f"{__version__}-{OS_NAME}-{ARCH}"
    out: list[Path] = []
    if IS_WIN:
        for src, name in (("NetTransfer.exe", "NetTransfer"), ("nettransfer-cli.exe", "nettransfer-cli")):
            out.append(shutil.copy2(DIST / src, RELEASE / f"{name}-{tag}.exe"))
    elif IS_MAC:
        app = DIST / "NetTransfer.app"
        zipped = RELEASE / f"NetTransfer-{tag}.zip"
        # ditto keeps symlinks, resource forks and signatures intact (shutil/zipfile would break the bundle)
        run(["ditto", "-c", "-k", "--keepParent", app, zipped])
        out.append(zipped)
        cli_tar = RELEASE / f"nettransfer-cli-{tag}.tar.gz"
        tar_gz(DIST / "nettransfer-cli", cli_tar)
        out.append(cli_tar)
    else:
        for src, name in (("nettransfer", "NetTransfer"), ("nettransfer-cli", "nettransfer-cli")):
            archive = RELEASE / f"{name}-{tag}.tar.gz"
            tar_gz(DIST / src, archive)
            out.append(archive)
    sums = RELEASE / f"SHA256SUMS-{OS_NAME}-{ARCH}.txt"
    sums.write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in out), encoding="utf-8")
    return [*out, sums]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-test", action="store_true", help="skip the smoke tests")
    parser.add_argument("--stage", choices=("all", "build", "collect"), default="all")
    args = parser.parse_args()
    if args.stage in ("all", "build"):
        ensure_icons()
        pyinstaller()
        if not args.no_test:
            smoke_test()
    if args.stage in ("all", "collect"):
        for path in collect():
            print(f"{path.stat().st_size / 1e6:8.1f} MB  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
