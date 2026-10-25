# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把「启动面板 + 工作流 + pytest」打成单文件 exe。

构建：
    python -m PyInstaller --noconfirm --clean launcher.spec
产物：
    dist/WorkflowPanel.exe       双击打开面板；也支持 --cli / --pytest 子命令

设计要点
--------
* **数据一起打包**：tasks / tests / mock / config / solutions / conftest.py / pytest.ini，
  这样 exe 被单独拷到没有源码的机器上也能跑（此时 runs/ 写在 exe 旁边）。
* **pytest 必须进包**：``core/verify.py`` 在打包环境里通过 ``exe --pytest ...``
  调用同一份 exe 内的 pytest，所以把 pytest 及其插件全部收进来。
* **一份 exe 三种模式**：GUI（无参数）/ ``--cli`` / ``--pytest``，见 launcher/app.py。
"""

import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(SPECPATH)  # noqa: F821 - PyInstaller 注入

datas = [
    (os.path.join(ROOT, "tasks"), "tasks"),
    (os.path.join(ROOT, "tests"), "tests"),
    (os.path.join(ROOT, "mock"), "mock"),
    (os.path.join(ROOT, "config"), "config"),
    (os.path.join(ROOT, "solutions"), "solutions"),
    (os.path.join(ROOT, "conftest.py"), "."),
    (os.path.join(ROOT, "pytest.ini"), "."),
    (os.path.join(ROOT, "requirements.txt"), "."),
]
for pkg in ("pytest", "pluggy", "iniconfig", "packaging"):
    try:
        datas += copy_metadata(pkg)
    except Exception:  # noqa: BLE001 - 缺某个包时不影响其余数据
        pass

hiddenimports = [
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "packaging",
    "tkinter",
    "tkinter.ttk",
    "tkinter.scrolledtext",
    "tkinter.messagebox",
    "tkinter.font",
]
hiddenimports += collect_submodules("_pytest")

a = Analysis(
    [os.path.join(ROOT, "launcher", "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    # 覆盖社区 hook-workflow.py（它是给 PyPI 上的 workflow 包的，会报错）
    hookspath=[os.path.join(ROOT, "pyinstaller-hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "matplotlib", "PIL", "lxml", "docx", "pptx", "openpyxl"],
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
    name="WorkflowPanel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "assets", "workflow.ico"),
)
