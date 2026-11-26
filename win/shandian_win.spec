# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir + 无控制台，web 资源随包。"""

a = Analysis(
    ["shandian_win.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "email", "html", "http.cookiejar"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShandianDownload",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ShandianDownload",
)
