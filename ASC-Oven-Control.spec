# -*- mode: python ; coding: utf-8 -*-
# Build:  pyinstaller ASC-Oven-Control.spec --noconfirm --clean
# Windows -> dist/ASC Oven Control/ASC Oven Control.exe (icon assets/asc_oven_icon.ico)
# macOS   -> dist/ASC Oven Control.app                  (icon assets/ASC-Oven.icns)

import sys
from pathlib import Path


project_root = Path(SPECPATH)
is_windows = sys.platform == "win32"
is_macos = sys.platform == "darwin"
icon = project_root / "assets" / ("asc_oven_icon.ico" if is_windows else "ASC-Oven.icns")

# Conda-based Pythons keep the C libraries behind sqlite3, ctypes, hashlib
# (used by multiprocessing), bz2 and lzma in <prefix>/Library/bin, where
# PyInstaller does not look; without them the exe fails at "import sqlite3".
conda_bin = Path(sys.base_prefix) / "Library" / "bin"
conda_dlls = []
if is_windows and conda_bin.is_dir():
    for pattern in ("sqlite3.dll", "ffi*.dll", "libcrypto-*.dll", "libssl-*.dll", "libbz2.dll",
                    "bzip2.dll", "liblzma.dll", "libexpat.dll", "expat.dll", "zlib.dll"):
        conda_dlls += [(str(path), ".") for path in conda_bin.glob(pattern)]

a = Analysis(
    [str(project_root / "asc_oven_control" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=conda_dlls,
    datas=[
        (str(project_root / "assets" / "asc_oven_icon.png"), "assets"),
        (str(project_root / "assets" / "asc_oven_icon.ico"), "assets"),
        (str(project_root / "assets" / "ui"), "assets/ui"),
    ],
    # The run controller runs in a spawned child process; make sure the
    # modules it imports by name are collected.
    hiddenimports=[
        "serial.tools.list_ports",
        "pyqtgraph",
        "asc_oven_control.services.run_controller",
        "asc_oven_control.services.oven_backend",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ASC Oven Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon),
)

collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ASC Oven Control",
)

if is_macos:
    app = BUNDLE(
        collection,
        name="ASC Oven Control.app",
        icon=str(icon),
        bundle_identifier="edu.umn.asc.oven-control",
        info_plist={
            "CFBundleDisplayName": "ASC Oven Control",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        },
    )
