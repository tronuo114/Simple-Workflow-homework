"""聊天模式的后端测试：一句话需求 → 临时任务 → 事件流 → 循环。

覆盖三件事：
1. ``Task.from_prompt`` 把需求落成任务文件（不碰 ``tasks/``）；
2. ``infer_tests`` 能在自由输入里认出已知任务（TTLCache → 复用 25 个用例）；
3. ``--events`` 用的结构化事件会按顺序产出，面板才能据此渲染对话。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflow.orchestrator import Orchestrator  # noqa: E402
from workflow.task import Task, infer_tests  # noqa: E402

GENERIC_CODE = '''\
"""演示模块。"""


def add(a, b):
    """返回两数之和。"""
    return a + b


def safe_div(a, b):
    """除以零返回 None。"""
    if b == 0:
        return None
    return a / b
'''

CACHE_CODE = '''\
class TTLCache:
    """只是用来验证「能否认出已知任务」。"""

    def __init__(self, capacity, ttl_seconds):
        self.capacity = capacity
        self.ttl = ttl_seconds

    def get(self, key):
        return None

    def put(self, key, value):
        return None
'''


class ScriptedLLM:
    provider = "scripted"

    def __init__(self, code: str, review):
        self.code = code
        self.review = review
        self.calls = {"coder": 0, "fixer": 0, "reviewer": 0}

    def chat_code(self, agent: str, system: str, user: str) -> str:
        self.calls[agent] = self.calls.get(agent, 0) + 1
        return self.code

    def chat_json(self, agent: str, system: str, user: str):
        self.calls[agent] = self.calls.get(agent, 0) + 1
        return dict(self.review)

    @property
    def call_log(self):
        return []


APPROVE = {"approved": True, "score": 90, "issues": [], "summary": "满足要求"}


def make_task(tmp_path: Path) -> Task:
    return Task.from_prompt(
        "帮我写两个小函数：add 和 safe_div",
        task_id="chat-demo",
        root=tmp_path / "prompt",
        target_file="demo.py",
        module_name="demo",
    )


def test_from_prompt_writes_task_files(tmp_path: Path) -> None:
    task = make_task(tmp_path)

    assert task.task_id == "chat-demo"
    assert task.class_name == ""                      # 自由输入没有固定类名
    assert "add 和 safe_div" in task.spec_text
    assert "REQ-1" in task.requirements_text
    assert (task.root / "meta.json").is_file()
    assert task.test_files == {}                       # 没有现成测试


def test_infer_tests_finds_known_suite() -> None:
    assert infer_tests(CACHE_CODE) == "ttl_cache"
    assert infer_tests(GENERIC_CODE) is None


def test_free_prompt_run_emits_events_until_approved(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    events = []
    orch = Orchestrator(
        task,
        run_id="chat-demo-run",
        runs_root=tmp_path / "runs",
        overrides={"provider": "mock"},
        verbose=False,
        llm=ScriptedLLM(GENERIC_CODE, APPROVE),
        event_sink=events.append,
    )

    state = orch.run()

    assert state.phase == "APPROVED"
    kinds = [e["type"] for e in events]
    for expected in ("session", "code", "verification", "review", "loop", "done"):
        assert expected in kinds
    assert kinds[0] == "session" and kinds[-1] == "done"
    done = events[-1]
    assert done["phase"] == "APPROVED"
    assert done["round"] == 1
    assert done["report"].endswith("REPORT.md")
    # 自由输入没有测试，通用静态检查通过即算机器验收通过
    verification = next(e for e in events if e["type"] == "verification")
    assert verification["passed"] is True
    assert verification["tests_from"] is None
