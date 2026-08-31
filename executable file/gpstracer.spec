# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

vendor_datas = [
    (os.path.join("engine", "vendor", name), os.path.join("engine", "vendor"))
    for name in (
        "integration_blackbox.py",
        "integration_avi.py",
        "integration_mp4.py",
        "ENGINE_README.md",
        "ENGINE_ARCHITECT.md",
        "ENGINE_COMMIT.txt",
        "README_VENDOR.md",
        "LICENSE",
    )
]

web_datas = [
    (os.path.join("ui", "web", "map.html"), os.path.join("ui", "web")),
    (os.path.join("ui", "vendor", "maplibre-gl.js"), os.path.join("ui", "vendor")),
    (os.path.join("ui", "vendor", "maplibre-gl.css"), os.path.join("ui", "vendor")),
    (os.path.join("ui", "vendor", "pmtiles.js"), os.path.join("ui", "vendor")),
]

basemap_src = os.path.join("assets", "korea.pmtiles")
if os.path.isfile(basemap_src):
    web_datas.append((basemap_src, "assets"))

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=vendor_datas + web_datas,
    hiddenimports=[
        "engine_entry",
        "engine.registry",
        "core.paths",
        "argparse",
        "csv",
        "dataclasses",
        "datetime",
        "math",
        "mmap",
        "re",
        "shlex",
        "struct",
        "typing",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PySide6.QtQuick3D",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtSerialPort",
        "PySide6.QtTest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GPSTracer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GPSTracer",
)
