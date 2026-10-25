"""单一 exe 入口：启动面板 / 命令行 / pytest 三种模式。

双击 exe（不带参数）→ 打开启动面板。

面板用子进程驱动工作流，打包后 ``sys.executable`` 就是本 exe，所以需要把
「命令行」和「pytest」也做成同一个 exe 的子命令：

    WorkflowPanel.exe --cli run --task ttl_cache --mock
    WorkflowPanel.exe --pytest -q --no-header -p no:cacheprovider tests

这样 ``core/verify.py`` 在打包环境里也能跑测试，而不必假设系统装了 Python。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 源码方式运行（python launcher\app.py）时，把仓库根加入 sys.path，
# 这样 `import workflow` 才能解析；打包运行时模块已在归档里，不影响。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _rebind_std_streams() -> None:
    """windowed exe 的 sys.stdout/stderr 可能是 None；子进程模式下要接回管道。"""
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(fd, "w", encoding="utf-8", errors="replace",
                                        buffering=1, closefd=False))
            except OSError:
                pass


def _selftest() -> int:
    """在打包环境里自检面板：斜杠指令是否可用（不打开窗口）。"""
    import tkinter as tk

    from launcher.panel import BUILD_TIME, Panel

    root = tk.Tk()
    root.withdraw()
    panel = Panel(root)
    panel.clear_chat()
    panel._handle_command("/help")
    text = panel.chat.get("1.0", "end")
    slash_ok = "/key" in text and "/mode" in text
    panel._handle_command("/version")
    version_ok = "面板构建" in panel.chat.get("1.0", "end")
    root.destroy()

    print(f"build: {BUILD_TIME}")
    print(f"slash-commands: {'OK' if slash_ok else 'MISSING'}")
    print(f"version-command: {'OK' if version_ok else 'MISSING'}")
    return 0 if (slash_ok and version_ok) else 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("--cli", "--pytest", "--version", "-V", "--selftest"):
        _rebind_std_streams()
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    if args and args[0] == "--selftest":
        return _selftest()

    if args and args[0] in ("--version", "-V"):
        from launcher.panel import BUILD_TIME
        print(f"WorkflowPanel 构建：{BUILD_TIME}")
        print(f"支持指令：/help /key /mode /model /base /rounds /parallel /status /version /report /clear /stop /run")
        return 0

    if args and args[0] == "--cli":
        from workflow.cli import main as cli_main
        return int(cli_main(args[1:]))

    if args and args[0] == "--pytest":
        import pytest
        return int(pytest.main(args[1:]))

    from launcher.panel import launch
    return int(launch())


if __name__ == "__main__":
    sys.exit(main())
