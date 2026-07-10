# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build configuration.

Build the installer-ready executable with:
    pyinstaller amazon_listing_generator.spec

The config/ and templates/ folders are bundled next to the executable so
mappings and rules stay editable after packaging (no code changes needed
for new field mappings).
"""

import customtkinter
from pathlib import Path

ctk_path = Path(customtkinter.__file__).parent

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("config", "config"),
        ("templates", "templates"),
        (str(ctk_path), "customtkinter"),
    ],
    hiddenimports=[
        "customtkinter",
        "PIL._tkinter_finder",
        "openpyxl.cell._writer",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AmazonListingGenerator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed desktop app
    disable_windowed_traceback=False,
    icon=None,              # drop an .ico path here for branding
)
