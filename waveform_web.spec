# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置 — 下肢血流监测平台
#
# 使用方法:
#   pip install pyinstaller
#   cd "D:\DesktopD\课题组\关于装置\waveform-web"
#   pyinstaller waveform_web.spec
#
# 产物: dist\waveform_web\ (文件夹模式, 启动快, 推荐)
#        dist\waveform_web\waveform_web.exe  ← 双击启动
#
# 迁移到其他电脑: 把整个 dist\waveform_web\ 文件夹拷过去即可, 无需安装 Python。

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPEC).parent   # waveform-web 根目录(和本 .spec 同级)

a = Analysis(
    [str(ROOT / 'server.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 把 static/ 目录一起打进包
        (str(ROOT / 'static'), 'static'),
    ],
    hiddenimports=[
        # pyserial 串口枚举
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'serial.tools.list_ports_windows',
        # scipy/numpy 有时需要显式指定
        'scipy.signal',
        'scipy.signal._upfirdn_apply',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要的大包, 减小体积
        'matplotlib', 'tkinter', 'PyQt5', 'PyQt6', 'wx',
        'IPython', 'jupyter', 'notebook',
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
    name='waveform_web',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # 保留控制台窗口, 方便看错误日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,             # 如有 .ico 文件改成: icon='icon.ico'
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='waveform_web',
)
