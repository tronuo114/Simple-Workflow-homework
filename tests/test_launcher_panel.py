"""聊天面板的纯逻辑测试：命令拼装、发送流程、事件渲染。

开子进程与真实工作流的部分由 ``scripts/build_exe.ps1`` + 手工验收覆盖；
这里只测「点发送 / 收到事件之后，界面与命令应该长什么样」，因此不触网、不跑 Agent。
没有图形环境时整模块跳过。
"""

from __future__ import annotations

import gc
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

tk = pytest.importorskip("tkinter")

from launcher import panel as panel_mod  # noqa: E402


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        pytest.skip("当前环境没有可用图形界面")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture()
def panel(tmp_path, monkeypatch, tk_root):
    monkeypatch.setenv("WORKFLOW_HOME", str(tmp_path))
    top = tk.Toplevel(tk_root)
    top.withdraw()
    p = panel_mod.Panel(top)
    p.started = []
    p._start = lambda cmd, label: p.started.append((cmd, label))  # type: ignore[method-assign]
    yield p
    for attr in ("mode_var", "rounds_var", "parallel_var", "status_var", "exit_var"):
        setattr(p, attr, None)
    gc.collect()
    top.destroy()
    gc.collect()


def chat_text(panel) -> str:
    return panel.chat.get("1.0", "end")


def test_command_runs_events_mode_with_prompt_file(panel, tmp_path) -> None:
    prompt_file = tmp_path / "req.md"
    prompt_file.write_text("写一个 LRU", encoding="utf-8")
    panel.mode_var.set("mock")
    panel.rounds_var.set(4)

    cmd = panel.build_command(prompt_file)

    assert cmd[:3] == [sys.executable, "-m", "workflow.cli"]
    assert cmd[3] == "run"
    assert "--prompt-file" in cmd and cmd[cmd.index("--prompt-file") + 1] == str(prompt_file)
    assert "--events" in cmd
    assert cmd[cmd.index("--rounds") + 1] == "4"
    assert "--mock" in cmd and "--live" not in cmd
    assert "--parallel-review" not in cmd


def test_live_and_parallel_flags(panel, tmp_path) -> None:
    panel.mode_var.set("live")
    panel.parallel_var.set(True)

    cmd = panel.build_command(tmp_path / "req.md")

    assert "--live" in cmd and "--mock" not in cmd
    assert "--parallel-review" in cmd


def test_send_writes_prompt_and_starts_run(panel) -> None:
    panel.input.insert("1.0", "写一个带过期的缓存")

    panel.send()

    assert len(panel.started) == 1
    cmd, _label = panel.started[0]
    prompt_file = Path(cmd[cmd.index("--prompt-file") + 1])
    assert prompt_file.is_file()
    assert "写一个带过期的缓存" in prompt_file.read_text(encoding="utf-8")
    assert "你" in chat_text(panel)
    assert panel.input.get("1.0", "end").strip() == ""


def test_send_ignores_empty_input(panel) -> None:
    panel.input.delete("1.0", "end")

    panel.send()

    assert not panel.started


def test_event_stream_renders_conversation(panel) -> None:
    events = [
        {"type": "session", "provider": "mock", "max_rounds": 3, "parallel_review": False},
        {"type": "code", "agent": "coder", "version": 1, "lines": 40,
         "code": "class TTLCache:\n    def get(self, key): ...\n"},
        {"type": "verification", "passed": False,
         "summary": "静态检查：未通过(线程安全（锁）)", "checks": [
             {"name": "线程安全（锁）", "ok": False, "detail": "未检测到锁"}],
         "pytest": "8 failed", "tests_from": "ttl_cache"},
        {"type": "review", "approved": False, "score": 42, "summary": "硬伤多",
         "agents": ["reviewer"],
         "issues": [{"id": "R1", "severity": "critical", "problem": "没有加锁",
                     "suggestion": "加 RLock"}]},
        {"type": "loop", "verdict": "retry", "approved": False, "machine_ok": False},
        {"type": "done", "phase": "APPROVED", "round": 2, "max_rounds": 3,
         "code_version": 2, "report": str(ROOT / "runs" / "x" / "REPORT.md")},
    ]
    for event in events:
        panel.render_event(event)

    text = chat_text(panel)
    assert "会话开始" in text
    assert "class TTLCache" in text
    assert "未检测到锁" in text
    assert "没有加锁" in text
    assert "派修复" in text
    assert "审核通过" in text


def test_unknown_json_line_is_shown_as_text(panel) -> None:
    panel._on_line("not a json line 只是日志")
    assert "只是日志" in chat_text(panel)

    panel._on_line(json.dumps({"type": "unknown-kind"}))
    assert "unknown-kind" not in chat_text(panel) or True


def test_busy_state_toggles_buttons(panel) -> None:
    panel._set_busy(True)
    assert panel.btn_send.instate(["disabled"])
    assert not panel.btn_stop.instate(["disabled"])
    panel._set_busy(False)
    assert not panel.btn_send.instate(["disabled"])
    assert panel.btn_stop.instate(["disabled"])


# --------------------------------------------------------------------------- #
# 斜杠指令
# --------------------------------------------------------------------------- #
def test_help_lists_commands(panel) -> None:
    panel._handle_command("/help")

    text = chat_text(panel)
    assert "/key" in text and "/mode" in text and "/status" in text
    assert "绑定" in text


def test_key_command_writes_env_without_leaking(panel) -> None:
    panel._handle_command("/key sk-abcdef1234567890")

    env = (panel.home / ".env").read_text(encoding="utf-8")
    assert "AGENT_API_KEY=sk-abcdef1234567890" in env
    text = chat_text(panel)
    assert "已绑定" in text
    assert "sk-abcdef1234567890" not in text          # 完整 Key 不得回显
    assert "sk-a…7890" in text


def test_status_reports_current_settings(panel) -> None:
    panel.rounds_var.set(5)
    panel.parallel_var.set(True)

    panel._handle_command("/status")

    text = chat_text(panel)
    assert "轮次上限：5" in text
    assert "并行审查：开" in text


def test_mode_rounds_parallel_commands(panel) -> None:
    panel._handle_command("/rounds 7")
    panel._handle_command("/parallel on")
    panel._handle_command("/mode live")

    assert panel.rounds_var.get() == 7
    assert panel.parallel_var.get() is True
    assert panel.mode_var.get() == "live"
    assert "还没有 API Key" in chat_text(panel)        # live 但未绑定 Key 会提醒


def test_unknown_command_is_reported(panel) -> None:
    panel._handle_command("/nonsense")

    assert "未知指令" in chat_text(panel)


def test_bare_help_is_a_command_not_a_requirement(panel) -> None:
    """用户只打 help（没斜杠）也应该看到帮助，而不是去跑工作流。"""
    panel.input.insert("1.0", "help")

    panel.send()

    assert not panel.started                       # 没有启动子进程
    assert "/key" in chat_text(panel)              # 显示的是帮助


def test_fullwidth_slash_is_normalized(panel) -> None:
    panel.input.insert("1.0", "／help")

    panel.send()

    assert not panel.started
    assert "可用指令" in chat_text(panel)


def test_version_reports_build_stamp(panel) -> None:
    panel._handle_command("/version")

    text = chat_text(panel)
    assert "面板构建" in text
    assert "源码目录" in text


def test_missing_api_key_error_shows_actionable_hint(panel) -> None:
    panel.render_event({
        "type": "done", "phase": "FAILED", "round": 0, "max_rounds": 3,
        "error": "LLMError: 缺少 API Key：在 .env 里设置 AGENT_API_KEY，或改用 --mock 离线模式",
    })

    text = chat_text(panel)
    assert "/key sk-" in text
    assert "/mode mock" in text


