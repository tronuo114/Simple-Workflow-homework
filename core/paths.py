"""应用根目录解析：源码运行 / PyInstaller 打包运行 / 环境变量覆盖。

为什么要单独一个模块
--------------------
打包成 exe 后，``__file__`` 指向 PyInstaller 解包出来的临时目录（``sys._MEIPASS``），
再用 ``Path(__file__).parent.parent`` 推仓库根就会指错地方——配置、任务、``runs/``
都会落到临时目录里，程序退出即丢失。这里把「根目录在哪」收敛成一个函数：

查找顺序（先命中先用）：

1. ``WORKFLOW_HOME`` 环境变量（显式指定，优先级最高）；
2. 打包运行（``sys.frozen``）时，exe 所在目录及其上一级——只要该目录里有
   ``tasks/`` 与 ``workflow/`` 就认为是仓库根。把 exe 放在 ``dist/`` 里时，
   上一级正好是仓库根，于是 ``runs/`` 与 ``solutions/`` 仍写在仓库里、可追溯；
3. 打包运行且 exe 被单独拷走：退回到解包目录（只读数据随 exe 一起打包）；
4. 源码运行：本文件的上两级目录。

这样同一份代码在三种场景下都能找到正确的根目录，不需要在业务代码里判断环境。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "WORKFLOW_HOME"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里。"""
    return bool(getattr(sys, "frozen", False))


def _looks_like_project(path: Path) -> bool:
    return (path / "tasks").is_dir() and (path / "workflow").is_dir()


def app_root() -> Path:
    """返回应用根目录（含 ``tasks/``、``workflow/``、``config/`` 的那一层）。"""
    override = os.environ.get(ENV_HOME)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir, exe_dir.parent):
            if _looks_like_project(candidate):
                return candidate
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return exe_dir

    return Path(__file__).resolve().parent.parent
