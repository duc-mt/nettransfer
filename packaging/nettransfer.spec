# -*- mode: python ; coding: utf-8 -*-
# One spec for Windows, macOS and Linux.  PyInstaller cannot cross-compile: run it on each target OS
# (see packaging/build.py and .github/workflows/build.yml).
#
# Produces two programs:
#   GUI  Windows: NetTransfer.exe (one file, no console)   macOS: NetTransfer.app   Linux: nettransfer (one file)
#   CLI  nettransfer-cli[.exe]  (one file, console, no Qt -> small and fast to start)
#
# Optional environment variables:
#   CODESIGN_IDENTITY   macOS signing identity / Windows signtool is done outside PyInstaller (see docs)
#   TARGET_ARCH         macOS only: x86_64 | arm64 | universal2   (default: the build machine's architecture)
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent            # SPECPATH is provided by PyInstaller (this file's directory)
sys.path.insert(0, str(ROOT))
from app import __version__                        # noqa: E402

BASE_VERSION = __version__.split("-")[0]           # "2.1.0-rc.1" -> "2.1.0": OS version fields must be numeric

IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
ASSETS = ROOT / "assets"
ICON = str(ASSETS / ("icon.ico" if IS_WIN else "icon.icns" if IS_MAC else "icon.png"))
SIGN_ID = os.environ.get("CODESIGN_IDENTITY") or None
ENTITLEMENTS = str(ROOT / "packaging" / "entitlements.plist") if SIGN_ID else None
ARCH = os.environ.get("TARGET_ARCH") or None

EXCLUDES = ["tkinter", "pytest", "_pytest", "pyftpdlib"]
QT_UNUSED = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtQml",
             "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf",
             "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtBluetooth",
             "PySide6.QtNetworkAuth", "PySide6.QtTest"]

# ---------------------------------------------------------------- Windows version resource
VERSION_FILE = None
if IS_WIN:
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple(int(x) for x in (BASE_VERSION.split(".") + ["0"] * 4)[:4])
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", "Network File Transfer"), StringStruct("FileVersion", __version__),
            StringStruct("InternalName", "NetTransfer"), StringStruct("OriginalFilename", "NetTransfer.exe"),
            StringStruct("ProductName", "Network File Transfer"), StringStruct("ProductVersion", __version__)])]),
              VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    VERSION_FILE = str(Path(SPECPATH) / "version_info.generated.txt")
    Path(VERSION_FILE).write_text(str(info), encoding="utf-8")

# ---------------------------------------------------------------- GUI
gui = Analysis([str(ROOT / "run.py")], pathex=[str(ROOT)],
               datas=[(str(ASSETS / "icon.png"), "assets")],
               hiddenimports=["psutil"], excludes=EXCLUDES + QT_UNUSED)
gui_pyz = PYZ(gui.pure)

if IS_MAC:
    # macOS: an .app bundle (onedir inside) is the supported "single thing to distribute"; one-file + windowed
    # is deprecated by PyInstaller on macOS because it unpacks to a temp dir on every launch.
    gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name="NetTransfer", console=False, icon=ICON,
                  upx=False, target_arch=ARCH, codesign_identity=SIGN_ID, entitlements_file=ENTITLEMENTS)
    gui_coll = COLLECT(gui_exe, gui.binaries, gui.datas, upx=False, name="NetTransfer")
    app = BUNDLE(gui_coll, name="NetTransfer.app", icon=ICON, bundle_identifier="com.nettransfer.app",
                 version=BASE_VERSION, info_plist={
                     "CFBundleName": "NetTransfer", "CFBundleDisplayName": "Network File Transfer",
                     "CFBundleShortVersionString": BASE_VERSION, "NSHighResolutionCapable": True,
                     "LSMinimumSystemVersion": "11.0",
                     # macOS 15+ asks before an app talks to devices on the local network
                     "NSLocalNetworkUsageDescription":
                         "Network File Transfer serves files to, and pushes files to, devices on your network."})
else:
    gui_exe = EXE(gui_pyz, gui.scripts, gui.binaries, gui.datas, [],
                  name="NetTransfer" if IS_WIN else "nettransfer",
                  # Windows: no console window.  Linux keeps one so the privilege hints are visible when started
                  # from a terminal (needed for `sudo ./nettransfer` and TFTP port 69).
                  console=not IS_WIN, icon=ICON, version=VERSION_FILE, upx=False)

# ---------------------------------------------------------------- CLI (no Qt)
cli = Analysis([str(ROOT / "packaging" / "cli_entry.py")], pathex=[str(ROOT)],
               excludes=EXCLUDES + ["PySide6", "shiboken6", "app.ui", "app.bootstrap"])
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(cli_pyz, cli.scripts, cli.binaries, cli.datas, [], name="nettransfer-cli", console=True, icon=ICON,
              upx=False, target_arch=ARCH, codesign_identity=SIGN_ID, entitlements_file=ENTITLEMENTS)
