"""聊天式工作流面板：输入一句话需求，看着 Agent 循环到审核通过。

交互模型
--------
用户在输入框写需求 → 面板把需求落成一次性任务（``--prompt-file``）→ 用子进程跑
``workflow.cli run --events`` → 编排器把每个动作以 JSONL 事件推回来 → 面板渲染成对话。

为什么用子进程 + 事件流：
* 界面不会被长任务卡死，「停止」能杀掉整个进程树；
* 每次运行都是干净进程，不残留上一次的模块缓存与状态；
* 事件流是纯数据，面板只负责「翻译成人话」，与 CLI 完全同构。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional

from core.paths import app_root, is_frozen

TITLE = "工作流聊天面板 · 三 Agent 代码审查"
UI_FONT = ("Microsoft YaHei UI", 10)
MONO_FONT = ("Consolas", 9)
CODE_PREVIEW_LINES = 14

EXAMPLE_PROMPT = "用 Python 写一个带过期时间的 LRU 缓存 TTLCache(capacity, ttl_seconds)，支持 get/put，线程安全，get/put 均摊 O(1)"

HELP_TEXT = """可用指令（在输入框输入后按 Enter）：
  /help                 查看本帮助
  /bind <API_KEY>       绑定模型 API Key（写入项目根 .env，只显示前后 4 位）
  /bind                 查看是否已绑定
  /unbind               解绑（从 .env 删除 AGENT_API_KEY）
  /test                 链路自检：文件 / 目录 / 配置 / Key / 模型连通 / pytest
  /status               查看当前配置（模式 / 轮次 / 并行 / 模型 / 接口 / 绑定状态）
  /model <名字>          设置模型，例：/model deepseek-chat
  /base <URL>           设置 OpenAI 兼容接口，例：/base https://api.deepseek.com/v1
  /version              查看面板构建时间（确认没在跑旧 exe）
  /rounds <1-20>        设置轮次上限（超过就熔断，交出遗留问题）
  /parallel on|off      并行审查：主审查 + 安全审查并发
  /run <需求>           提交需求（等价于直接输入文字）
  /report               打开最近一次运行报告
  /clear                清空对话
  /stop                 停止正在运行的工作流

模式：面板固定使用真实模型（离线 mock 已禁用），运行前请先 /bind 绑定 Key。
直接输入非 / 开头的文字 = 提交需求；Shift+Enter 换行。"""

# 这些词不带斜杠也当指令处理（避免「输入 help 结果去跑工作流」）
BARE_COMMANDS = {"help", "status", "report", "clear", "stop", "version"}

# `--version` 打印的指令清单：从 HELP_TEXT 里抽，避免再写一份会过期的硬编码
COMMANDS_TEXT = " ".join(dict.fromkeys(
    line.split()[0]
    for line in HELP_TEXT.splitlines()
    if line.strip().startswith("/")
))

try:  # 打包时由 scripts/build_exe.ps1 写入
    from launcher.build_info import BUILD_TIME  # type: ignore
except Exception:  # noqa: BLE001 - 源码运行时没有构建信息
    BUILD_TIME = "源码运行（未打包）"


def _normalize(text: str) -> str:
    """全角斜杠、零宽字符、首尾空白统一处理。"""
    text = text.replace("／", "/").replace("\u200b", "").replace("\ufeff", "")
    return text.strip()


def _mask(key: str) -> str:
    key = key.strip()
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}（共 {len(key)} 位）"

# 角色 → (显示名, 颜色)
ROLES = {
    "user": ("你", "#1f6feb"),
    "system": ("编排器", "#8b949e"),
    "coder": ("编码 Agent", "#2f81f7"),
    "reviewer": ("审查 Agent", "#b083f0"),
    "security_reviewer": ("安全审查", "#d29922"),
    "fixer": ("修复 Agent", "#39c5a0"),
    "verify": ("机器验收", "#e3b341"),
    "loop": ("循环分支", "#ff9f43"),
    "done": ("结果", "#3fb950"),
    "error": ("错误", "#f85149"),
}


class Panel:
    """聊天面板。对外入口：``launch()``。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.home = app_root()
        self.proc: Optional[subprocess.Popen] = None
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._last_run_dir: Optional[Path] = None

        self.root.title(TITLE)
        self.root.geometry("1040x820")
        self.root.minsize(900, 620)
        self._setup_style()
        self._build_ui()
        self.root.after(80, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._append("system",
                     "工作流聊天面板已就绪（真实模型，离线 mock 已禁用）。\n"
                     f"构建：{BUILD_TIME}　项目根：{self.home}\n"
                     "第一次使用：`/bind sk-你的Key` 绑定 API Key → `/test` 检查链路 → 直接输入需求。\n"
                     "输入 /help 查看全部指令（/bind /unbind /test /status /version …）。")
        self._refresh_key_state()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure(".", font=UI_FONT)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Hint.TLabel", foreground="#6e7681")
        style.configure("Send.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # -- 顶栏 ---------------------------------------------------------- #
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="工作流聊天面板", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="编码 → 循环（审查 → 分支 → 修复）直到通过",
                  style="Hint.TLabel").pack(side="left", padx=10, pady=(4, 0))

        ttk.Button(head, text="📄 最近报告", command=self.open_latest_report).pack(side="right")
        ttk.Button(head, text="🗑 清空", command=self.clear_chat).pack(side="right", padx=6)

        # -- 参数 ---------------------------------------------------------- #
        params = ttk.Frame(outer)
        params.pack(fill="x", pady=(8, 6))
        ttk.Label(params, text="模式").pack(side="left")
        ttk.Label(params, text="真实模型 live（离线 mock 已禁用）",
                  style="Hint.TLabel").pack(side="left", padx=(4, 0))
        self.key_state = ttk.Label(params, text="", style="Hint.TLabel")
        self.key_state.pack(side="left", padx=(12, 0))

        ttk.Label(params, text="轮次上限").pack(side="left", padx=(16, 4))
        self.rounds_var = tk.IntVar(value=3)
        ttk.Spinbox(params, from_=1, to=20, width=4,
                    textvariable=self.rounds_var).pack(side="left")

        self.parallel_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(params, text="并行审查（主审查 + 安全审查）",
                        variable=self.parallel_var).pack(side="left", padx=(16, 0))

        ttk.Button(params, text="示例需求", command=self.fill_example).pack(side="right")
        ttk.Button(params, text="/help", command=lambda: self._handle_command("/help")).pack(
            side="right", padx=(0, 6))

        # -- 对话区 -------------------------------------------------------- #
        chatbox = ttk.Frame(outer)
        chatbox.pack(fill="both", expand=True)
        self.chat = scrolledtext.ScrolledText(
            chatbox, wrap="word", font=UI_FONT, state="disabled", height=12,
            background="#0d1117", foreground="#e6edf3", insertbackground="#e6edf3",
            borderwidth=0, relief="flat", padx=12, pady=10, spacing1=2, spacing3=2,
        )
        self.chat.pack(fill="both", expand=True)
        for role, (_, color) in ROLES.items():
            self.chat.tag_configure(f"role_{role}", foreground=color,
                                    font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_configure("body", foreground="#e6edf3")
        self.chat.tag_configure("meta", foreground="#6e7681", font=("Microsoft YaHei UI", 9))
        self.chat.tag_configure("code", font=MONO_FONT, background="#161b22",
                                foreground="#c9d1d9", lmargin1=16, lmargin2=16)
        self.chat.tag_configure("input", foreground="#9ecbff")

        # -- 输入 ---------------------------------------------------------- #
        entry = ttk.Frame(outer)
        entry.pack(fill="x", pady=(8, 4))
        self.input = tk.Text(entry, height=3, wrap="word", font=UI_FONT,
                             padx=8, pady=6, relief="solid", borderwidth=1)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Shift-Return>", lambda e: None)

        actions = ttk.Frame(entry)
        actions.pack(side="left", fill="y", padx=(8, 0))
        self.btn_send = ttk.Button(actions, text="发送", style="Send.TButton", command=self.send)
        self.btn_send.pack(fill="x", pady=(0, 4))
        self.btn_stop = ttk.Button(actions, text="■ 停止", command=self.stop)
        self.btn_stop.pack(fill="x")
        self.btn_stop.state(["disabled"])

        # -- 状态栏 -------------------------------------------------------- #
        status = ttk.Frame(outer)
        status.pack(side="bottom", fill="x", pady=(4, 0))
        self.status_var = tk.StringVar(value="就绪 · 输入需求后按 Enter 发送")
        ttk.Label(status, textvariable=self.status_var, style="Hint.TLabel").pack(side="left")
        self.exit_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.exit_var, style="Hint.TLabel").pack(side="right")

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_mode(self) -> None:
        if self.mode_var.get() == "live" and not self._has_api_key():
            messagebox.showinfo(
                "需要 API Key",
                "live 模式需要 AGENT_API_KEY。\n\n"
                "在项目根的 .env 里写：\nAGENT_API_KEY=sk-xxxx\n"
                "（也可用环境变量 AGENT_API_KEY）\n\n"
                "没有 Key 请切回「离线 mock」，同样能完整跑通流程。",
                parent=self.root,
            )

    def _has_api_key(self) -> bool:
        if os.environ.get("AGENT_API_KEY"):
            return True
        env_file = self.home / ".env"
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip().startswith("AGENT_API_KEY="):
                        return bool(line.split("=", 1)[1].strip())
            except OSError:
                return False
        return False

    def fill_example(self) -> None:
        self.input.delete("1.0", "end")
        self.input.insert("1.0", EXAMPLE_PROMPT)
        self.input.focus_set()

    def clear_chat(self) -> None:
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")

    # -- 发送 ----------------------------------------------------------- #
    def _on_enter(self, event: tk.Event) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        prompt = _normalize(self.input.get("1.0", "end"))
        if not prompt:
            return
        is_command = prompt.startswith("/") or prompt.lower() in BARE_COMMANDS
        if self._busy and (not is_command or prompt.startswith("/run")):
            messagebox.showinfo("正在运行", "上一轮还没结束，先点「停止」或等它跑完。", parent=self.root)
            return
        self.input.delete("1.0", "end")

        if is_command:
            self._handle_command(prompt if prompt.startswith("/") else "/" + prompt)
            return

        self._append("user", prompt)
        self._append("system", "收到需求，开始跑工作流（编码 → 审查 → 分支 → 修复 → 再审）…")

        prompt_dir = self.home / "runs" / "_prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"request-{datetime.now():%Y%m%d-%H%M%S-%f}.md"
        prompt_file.write_text(prompt + "\n", encoding="utf-8")

        self._start(self.build_command(prompt_file), "运行工作流")

    # ------------------------------------------------------------------ #
    # 斜杠指令
    # ------------------------------------------------------------------ #
    def _handle_command(self, text: str) -> None:
        body = text.lstrip("/").strip()
        name, _, arg = body.partition(" ")
        name = name.lower()
        arg = arg.strip()

        # 回显时对 Key 做脱敏，避免把完整密钥留在对话里
        display = f"/{name} {_mask(arg)}" if name in ("bind", "key") and arg else text
        self._append("user", display)

        if name in ("help", "?", "h", ""):
            self._append("system", HELP_TEXT)
            return

        if name == "status":
            self._append("system", self._status_text())
            return

        if name == "version":
            self._append("system", f"面板构建：{BUILD_TIME}\n"
                                   f"可执行文件：{sys.executable}\n"
                                   f"源码目录：{self.home}\n"
                                   f"若这里的时间比 dist\\WorkflowPanel.exe 的修改时间旧，"
                                   f"说明你打开的是旧窗口，关掉重开即可。")
            return

        if name in ("bind", "key"):
            self._cmd_bind(arg)
            return

        if name in ("unbind", "release"):
            self._cmd_unbind()
            return

        if name in ("test", "doctor"):
            self._cmd_test()
            return

        if name in ("model", "base", "baseurl", "base_url"):
            self._cmd_env(name, arg)
            return

        if name == "mode":
            self._append("system", "离线 mock 已禁用，面板固定使用真实模型（live）。\n"
                                   "如需离线演示，请用命令行：python -m workflow.cli run --task ttl_cache --mock")
            return

        if name == "rounds":
            self._cmd_rounds(arg)
            return

        if name == "parallel":
            self._cmd_parallel(arg)
            return

        if name == "report":
            self.open_latest_report()
            self._append("system", "已打开最近一次运行报告。")
            return

        if name == "clear":
            self.clear_chat()
            self._append("system", "对话已清空。输入需求继续，或 /help 查看指令。")
            return

        if name == "stop":
            self.stop()
            return

        if name in ("run", "go"):
            if not arg:
                self._append("error", "用法：/run <需求>，例如 /run 写一个 LRU 缓存")
                return
            self.input.insert("1.0", arg)
            self.send()
            return

        self._append("error", f"未知指令 /{name}。输入 /help 查看可用指令。")

    def _cmd_bind(self, arg: str) -> None:
        if not arg:
            key = self._read_env().get("AGENT_API_KEY", "")
            if key:
                self._append("system", f"已绑定 API Key：{_mask(key)}\n"
                                       f"文件：{self.home / '.env'}\n"
                                       f"更换：/bind sk-新的Key　解绑：/unbind")
            else:
                self._append("system", "还没有绑定 API Key（离线 mock 已禁用，没有 Key 无法运行）。\n"
                                       "绑定：/bind sk-xxxxxxxx\n"
                                       "（写入项目根 .env 的 AGENT_API_KEY；该文件被 .gitignore 忽略）\n"
                                       "绑定后直接输入需求即可；可用 /test 检查链路。")
            return

        if len(arg) < 8:
            self._append("error", "Key 看起来太短了。用法：/bind sk-xxxxxxxx")
            return

        self._write_env({"AGENT_API_KEY": arg})
        self._refresh_key_state()
        self._append("system", f"✅ 已绑定 API Key：{_mask(arg)}\n"
                               f"已写入 {self.home / '.env'}（不入库）\n"
                               f"下一步：/test 检查链路，然后直接输入需求。")

    def _cmd_unbind(self) -> None:
        key = self._read_env().get("AGENT_API_KEY", "")
        if not key:
            self._append("system", "当前没有绑定 API Key，无需解绑。")
            return
        self._remove_env("AGENT_API_KEY")
        self._refresh_key_state()
        self._append("system", f"✅ 已解绑 API Key（原 {_mask(key)}）。\n"
                               f".env 中的 AGENT_API_KEY 已删除；重新绑定用 /bind sk-xxxxxxxx。")

    def _cmd_test(self) -> None:
        if self._busy:
            self._append("error", "正在运行工作流，等它结束再 /test。")
            return
        self._append("system", "开始链路自检（文件 / 目录 / 配置 / Key / 模型连通 / pytest）…")
        cmd = self._cli_base() + ["doctor", "--events"]
        self._start(cmd, "链路自检")

    def _cli_base(self) -> List[str]:
        if is_frozen():
            return [sys.executable, "--cli"]
        return [sys.executable, "-m", "workflow.cli"]

    def _cmd_env(self, name: str, arg: str) -> None:
        key = {"model": "AGENT_MODEL", "base": "AGENT_BASE_URL",
               "baseurl": "AGENT_BASE_URL", "base_url": "AGENT_BASE_URL"}[name]
        label = {"AGENT_MODEL": "模型", "AGENT_BASE_URL": "接口地址"}[key]
        if not arg:
            self._append("system", f"当前{label}：{self._read_env().get(key) or '（未设置，使用 config/agents.json 默认值）'}")
            return
        self._write_env({key: arg})
        self._append("system", f"✅ 已设置{label}：{arg}（写入 .env）")

    def _cmd_rounds(self, arg: str) -> None:
        if not arg.isdigit() or not (1 <= int(arg) <= 20):
            self._append("system", f"当前轮次上限：{self.rounds_var.get()}（用法：/rounds 5，范围 1-20）")
            return
        self.rounds_var.set(int(arg))
        self._append("system", f"✅ 轮次上限已设为 {arg}（超过就熔断并交出遗留问题）")

    def _cmd_parallel(self, arg: str) -> None:
        value = arg.lower()
        if value in ("on", "1", "true", "yes", "开"):
            self.parallel_var.set(True)
        elif value in ("off", "0", "false", "no", "关"):
            self.parallel_var.set(False)
        else:
            self._append("system", f"并行审查：{'开' if self.parallel_var.get() else '关'}"
                                   f"（用法：/parallel on|off）")
            return
        self._append("system", f"✅ 并行审查已{'开启（主审查 + 安全审查并发）' if self.parallel_var.get() else '关闭'}")

    # -- .env ----------------------------------------------------------- #
    def _read_env(self) -> Dict[str, str]:
        data: Dict[str, str] = {}
        path = self.home / ".env"
        if not path.is_file():
            return data
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        return data

    def _write_env(self, updates: Dict[str, str]) -> None:
        path = self.home / ".env"
        existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        remaining = dict(updates)
        lines: List[str] = []
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    lines.append(f"{key}={remaining.pop(key)}")
                    continue
            lines.append(line)
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _remove_env(self, key: str) -> None:
        path = self.home / ".env"
        if not path.is_file():
            return
        kept = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped \
                    and stripped.split("=", 1)[0].strip() == key:
                continue
            kept.append(line)
        path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")

    def _refresh_key_state(self) -> None:
        if not hasattr(self, "key_state"):
            return
        key = self._read_env().get("AGENT_API_KEY", "")
        self.key_state.configure(text=f"Key：{_mask(key)}" if key else "Key：未绑定（/bind 绑定）")

    def _status_text(self) -> str:
        env = self._read_env()
        key = env.get("AGENT_API_KEY", "")
        return "\n".join([
            f"项目根：{self.home}",
            "模式：真实模型 live（离线 mock 已禁用）",
            f"轮次上限：{self.rounds_var.get()}",
            f"并行审查：{'开' if self.parallel_var.get() else '关'}",
            f"API Key：{_mask(key) if key else '未绑定（/bind sk-xxxx）'}",
            f"模型：{env.get('AGENT_MODEL') or '默认（config/agents.json）'}",
            f"接口：{env.get('AGENT_BASE_URL') or '默认（config/agents.json）'}",
            "指令：/help 查看全部；/test 检查链路。",
        ])

    # -- 命令 ----------------------------------------------------------- #
    def build_command(self, prompt_file: Path) -> List[str]:
        args = ["run", "--prompt-file", str(prompt_file), "--events", "--live",
                "--rounds", str(self.rounds_var.get())]
        if self.parallel_var.get():
            args.append("--parallel-review")
        return self._cli_base() + args

    def _start(self, cmd: List[str], label: str) -> None:
        self._set_busy(True)
        self.exit_var.set("")
        self.status_var.set(f"{label} · 运行中…")

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "WORKFLOW_HOME": str(self.home)}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(self.home), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            self._append("error", f"启动失败：{exc}")
            self.status_var.set("启动失败")
            self._set_busy(False)
            return
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.queue.put(("line", line.rstrip()))
        finally:
            self.queue.put(("done", proc.wait()))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "line":
                    self._on_line(str(payload))
                elif kind == "done":
                    self._on_done(int(payload))
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self._append("system", "正在停止子进程…")
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.proc.terminate()
        except Exception as exc:  # noqa: BLE001
            self._append("error", f"停止失败：{exc}")

    # ------------------------------------------------------------------ #
    # 事件 → 对话
    # ------------------------------------------------------------------ #
    def _on_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            self._append("system", line)
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self._append("system", line)
            return
        self.render_event(event)

    def render_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")

        if kind == "check":
            mark = "✅" if event.get("ok") else "❌"
            text = f"{mark} {event.get('name')}：{event.get('detail')}"
            if not event.get("ok") and event.get("hint"):
                text += f"\n    → {event.get('hint')}"
            self._append("verify" if event.get("ok") else "error", text)
            return

        if kind == "doctor":
            ok = bool(event.get("ok"))
            self._append("done" if ok else "error",
                         ("✅ 链路自检通过：" if ok else "❌ 链路自检未通过：")
                         + str(event.get("summary", "")))
            return

        if kind == "session":
            mode = "mock（脚本化）" if event.get("provider") == "mock" else f"live · {event.get('model')}"
            extra = "，并行审查已开启" if event.get("parallel_review") else ""
            self._append("system", f"会话开始：{mode}，轮次上限 {event.get('max_rounds')}{extra}")

        elif kind == "code":
            agent = event.get("agent", "coder")
            role = "coder" if agent == "coder" else "fixer"
            title = "初版代码" if role == "coder" else "修复后的代码"
            self._append_code(role, f"{title} · v{event.get('version')}（{event.get('lines')} 行）",
                              event.get("code") or "")

        elif kind == "verification":
            self._render_verification(event)

        elif kind == "review":
            self._render_review(event)

        elif kind == "loop":
            verdict = event.get("verdict")
            if verdict == "approved":
                self._append("loop", "分支：审查通过 → 退出循环")
            elif verdict == "rejected":
                self._append("loop", "分支：轮次预算耗尽 → 熔断（交给人）")
            else:
                self._append("loop", f"分支：不通过 → 派修复，回到循环开头"
                                     f"（LLM={event.get('approved')}，机器={event.get('machine_ok')}）")

        elif kind == "done":
            self._render_done(event)

    def _render_verification(self, event: Dict[str, Any]) -> None:
        passed = bool(event.get("passed"))
        lines = [("✅ " if passed else "❌ ") + ("通过" if passed else "未通过") + "：" + str(event.get("summary", ""))]

        failed = [c for c in event.get("checks") or [] if not c.get("ok")]
        for c in failed[:4]:
            lines.append(f"  · {c.get('name')}: {c.get('detail')}")
        if event.get("pytest"):
            lines.append(f"  · pytest：{event.get('pytest')}")
        if event.get("tests_from"):
            lines.append(f"  · 复用的测试套件：tasks/{event.get('tests_from')}")

        self._append("verify", "\n".join(lines))

    def _render_review(self, event: Dict[str, Any]) -> None:
        approved = bool(event.get("approved"))
        agents = " + ".join(event.get("agents") or ["reviewer"])
        head = ("✅ 通过" if approved else "❌ 不通过") + f"，评分 {event.get('score')}（{agents}）"
        if event.get("summary"):
            head += f"\n{event.get('summary')}"
        issues = event.get("issues") or []
        if issues:
            head += "\n"
            for issue in issues[:6]:
                sev = issue.get("severity", "?")
                head += (f"  · [{sev}] {issue.get('id', '')} {issue.get('problem', '')}"
                         f"\n      建议：{issue.get('suggestion', '')}\n")
        self._append("reviewer", head.strip())

    def _render_done(self, event: Dict[str, Any]) -> None:
        phase = event.get("phase")
        report = event.get("report")
        if phase == "APPROVED":
            self._append("done", f"✅ 审核通过（{event.get('round')} 轮，代码 v{event.get('code_version')}）\n"
                                 f"报告：{report}")
        elif phase == "REJECTED":
            self._append("error", f"❌ 未通过（用尽 {event.get('max_rounds')} 轮预算），已输出遗留问题\n"
                                  f"报告：{report}")
        else:
            error = str(event.get("error") or "")
            hint = ""
            if "缺少 API Key" in error or "AGENT_API_KEY" in error:
                hint = ("\n\n👉 当前是 live 模式但没有 Key：\n"
                        "   ① 输入 /key sk-xxxxxxxx 绑定（只写本地 .env，不回显完整 Key）\n"
                        "   ② 或输入 /mode mock 改用离线演示（不需要 Key）")
            elif "401" in error or "鉴权" in error:
                hint = "\n\n👉 Key 可能无效或过期：用 /key sk-新Key 重新绑定，或 /mode mock 离线演示。"
            self._append("error", f"运行结束：{phase}\n{error}{hint}")

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _append(self, role: str, text: str) -> None:
        name, _ = ROLES.get(role, (role, ""))
        self.chat.configure(state="normal")
        self.chat.insert("end", f"{name}  ", f"role_{role}")
        self.chat.insert("end", f"{datetime.now():%H:%M:%S}\n", "meta")
        self.chat.insert("end", text.rstrip() + "\n\n", "body")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def _append_code(self, role: str, title: str, code: str) -> None:
        lines = code.rstrip().splitlines()
        preview = lines[:CODE_PREVIEW_LINES]
        more = len(lines) - len(preview)
        body = "\n".join(preview)
        if more > 0:
            body += f"\n… 还有 {more} 行，完整代码见 runs/<run_id>/artifacts/"
        name, _ = ROLES.get(role, (role, ""))
        self.chat.configure(state="normal")
        self.chat.insert("end", f"{name}  ", f"role_{role}")
        self.chat.insert("end", f"{datetime.now():%H:%M:%S} · {title}\n", "meta")
        self.chat.insert("end", body + "\n", "code")
        self.chat.insert("end", "\n", "body")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    # -- 状态 ----------------------------------------------------------- #
    def _on_done(self, code: int) -> None:
        self._set_busy(False)
        self.exit_var.set(f"退出码：{code}")
        if code == 0:
            self.status_var.set("完成 · 审核通过 ✅（可以继续输入下一个需求）")
        elif code == 1:
            self.status_var.set("结束 · 未通过 ❌（看上方遗留问题，或加大轮次再试）")
        else:
            self.status_var.set(f"结束（退出码 {code}）")
            self._append("error", f"进程异常退出（退出码 {code}），看上方日志。")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_send.state(["disabled"] if busy else ["!disabled"])
        self.btn_stop.state(["!disabled"] if busy else ["disabled"])

    # -- 文件 ----------------------------------------------------------- #
    def _runs_dir(self) -> Path:
        return self.home / "runs"

    def _latest_run_dir(self) -> Optional[Path]:
        runs = self._runs_dir()
        if not runs.is_dir():
            return None
        candidates = [p for p in runs.iterdir() if p.is_dir() and (p / "REPORT.md").is_file()]
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    def open_latest_report(self) -> None:
        target = self._latest_run_dir()
        if not target:
            messagebox.showinfo("还没有运行记录", "先发一条需求，报告会生成在 runs/<run_id>/REPORT.md。",
                                parent=self.root)
            return
        self._open(target / "REPORT.md")

    def _open(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开", f"{path}\n\n{exc}", parent=self.root)

    def _on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("任务进行中", "工作流还在跑，确定停止并退出？", parent=self.root):
                return
            self.stop()
        self.root.destroy()


def launch() -> int:
    root = tk.Tk()
    Panel(root)
    root.mainloop()
    return 0
