# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 产出 dist/快手采集器/快手采集器.exe

用法:
    pyinstaller ks_gui.spec --noconfirm --clean

说明:
  * sign.js / jose.js 是 sig4 签名 VM，必须作为数据文件打进去；
  * node.exe 是 CI 里准备的便携版 Node（约 110MB），本地没有就跳过，
    运行时会自动回落到系统的 node；
  * 采用 onedir 模式而非 onefile：内置 node 让单文件体积过大，
    onefile 每次启动都要解压上百 MB，会很慢。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "sign.js"), "."),
    (str(ROOT / "jose.js"), "."),
]

node_exe = ROOT / "node.exe"
if node_exe.exists():                 # CI 打包前放到项目根目录
    datas.append((str(node_exe), "."))

binaries = []
hiddenimports = ["PyQt5.sip"]
for pkg in ("DrissionPage",):         # 带 ini 配置等数据文件
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

excludes = [
    # 用不到的 Qt 大模块
    "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngine",
    "PyQt5.QtWebChannel", "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuickWidgets",
    "PyQt5.Qt3DCore", "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning", "PyQt5.QtSensors",
    "PyQt5.QtSerialPort", "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtDesigner",
    "PyQt5.QtHelp", "PyQt5.QtOpenGL",
    # 没用到的第三方库
    "numpy", "matplotlib", "PIL", "scipy", "pandas", "IPython",
    # 标准库里的开发/测试工具
    "tkinter", "unittest", "pydoc", "doctest", "pytest",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "ks_gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="快手采集器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                    # GUI 程序，不弹黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "app.ico") if (ROOT / "app.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="快手采集器",
)
