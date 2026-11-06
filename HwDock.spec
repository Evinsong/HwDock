# -*- mode: python ; coding: utf-8 -*-

import os

# 图标可能被改名，按存在顺序挑一个，避免打包因缺文件失败
_ICONS = ['assets/app.ico', 'assets/系统监视器.ico', 'assets/icon.ico']
_ICON = next((p for p in _ICONS if os.path.exists(p)), None)


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HwDock',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=[_ICON] if _ICON else [],
)
