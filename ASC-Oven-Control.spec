# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)

a = Analysis(
    [str(project_root / "asc_oven_control" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "assets" / "asc_oven_icon.png"), "assets")],
    hiddenimports=["serial.tools.list_ports"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy"],
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
    icon=str(project_root / "assets" / "ASC-Oven.icns"),
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

app = BUNDLE(
    collection,
    name="ASC Oven Control.app",
    icon=str(project_root / "assets" / "ASC-Oven.icns"),
    bundle_identifier="edu.umn.asc.oven-control",
    info_plist={
        "CFBundleDisplayName": "ASC Oven Control",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
