"""编排层的循环体测试：证明「循环直到审核通过」真的是循环，而不是单次调用。

用脚本化假模型替换 ``LLMClient``（``Orchestrator(llm=...)``），不触网、不花钱，
可以精确控制「第几轮审查通过」，从而把循环的每个出口都测到：

1. 连续驳回两次 → 第三次通过：循环执行 3 次、修复 2 次；
2. 一直驳回且预算耗尽：循环在 ``max_rounds`` 处熔断，不会多跑；
3. LLM 说通过但机器验收不过：仍留在循环里，直到熔断；
4. 停在修复阶段的状态续跑：先把修复做完，再回到审查循环。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import Phase, WorkflowState  # noqa: E402
from workflow.orchestrator import (  # noqa: E402
    VERDICT_APPROVED,
    VERDICT_REJECTED,
    VERDICT_RETRY,
    Orchestrator,
)
from workflow.task import Task  # noqa: E402

# 能过静态检查的最小实现：有锁、有 with 临界区、get/put 无全表扫描
GOOD_CODE = '''\
import threading
from collections import OrderedDict


class Demo:
    """测试用最小实现。"""

    def __init__(self, capacity: int = 8) -> None:
        self._lock = threading.RLock()
        self._data = OrderedDict()
        self._capacity = capacity

    def get(self, key):
        """取一个 key。"""
        with self._lock:
            return self._data.get(key)

    def put(self, key, value):
        """写一个 key。"""
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)
'''

# 故意不满足静态检查（没有锁），用来验证「机器验收优先于 LLM 结论」
NO_LOCK_CODE = '''\
from collections import OrderedDict


class Demo:
    """没有锁的实现。"""

    def __init__(self, capacity: int = 8) -> None:
        self._data = OrderedDict()

    def get(self, key):
        """取一个 key。"""
        return self._data.get(key)

    def put(self, key, value):
        """写一个 key。"""
        self._data[key] = value
'''

REJECT = {
    "approved": False,
    "score": 55,
    "issues": [
        {"id": "R1", "severity": "medium", "requirement": "REQ-1",
         "location": "Demo.get", "problem": "缺少边界处理", "suggestion": "补上判空"},
    ],
    "summary": "还不满足验收项",
}
APPROVE = {"approved": True, "score": 96, "issues": [], "summary": "全部验收项满足"}


class ScriptedLLM:
    """按调用顺序返回预设内容的假模型客户端。"""

    provider = "scripted"

    def __init__(self, *, code: str, fixes, reviews) -> None:
        self._code = code
        self._fixes = list(fixes)
        self._reviews = list(reviews)
        self.calls = {"coder": 0, "fixer": 0, "reviewer": 0}

    def chat_code(self, agent: str, system: str, user: str) -> str:
        self.calls[agent] = self.calls.get(agent, 0) + 1
        if agent == "coder":
            return self._code
        # 修复：最后一份一直复用（模拟「修了但没修好」）
        return self._fixes.pop(0) if len(self._fixes) > 1 else self._fixes[0]

    def chat_json(self, agent: str, system: str, user: str):
        self.calls[agent] = self.calls.get(agent, 0) + 1
        return dict(self._reviews.pop(0) if len(self._reviews) > 1 else self._reviews[0])

    @property
    def call_log(self):
        return []


@pytest.fixture()
def task(tmp_path: Path) -> Task:
    root = tmp_path / "tasks" / "demo"
    root.mkdir(parents=True)
    (root / "task.md").write_text("# 演示任务\n实现一个类 Demo。\n", encoding="utf-8")
    (root / "requirements.md").write_text(
        "- **REQ-1** 支持 get/put\n- **REQ-2** 线程安全\n", encoding="utf-8")
    (root / "meta.json").write_text(
        json.dumps({"module_name": "demo", "class_name": "Demo",
                    "target_file": "demo.py", "tests": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return Task.load("demo", tasks_root=tmp_path / "tasks")


def build_orchestrator(task: Task, tmp_path: Path, llm: ScriptedLLM, **kwargs) -> Orchestrator:
    return Orchestrator(
        task,
        run_id=kwargs.pop("run_id", "loop-case"),
        runs_root=tmp_path / "runs",
        config_path=None,
        overrides={"provider": "mock"},
        verbose=False,
        llm=llm,
        **kwargs,
    )


def test_loop_repeats_until_review_passes(task: Task, tmp_path: Path) -> None:
    """核心断言：驳回两次后第三次通过 —— 循环体真的在重复，而不是只跑一次。"""
    llm = ScriptedLLM(code=GOOD_CODE, fixes=[GOOD_CODE], reviews=[REJECT, REJECT, APPROVE])
    orch = build_orchestrator(task, tmp_path, llm, max_rounds=5)

    state = orch.run()

    assert state.phase == Phase.APPROVED
    assert state.round == 3                                    # 审查循环跑了 3 次
    assert [t["verdict"] for t in state.loop_trace] == [
        VERDICT_RETRY, VERDICT_RETRY, VERDICT_APPROVED,
    ]
    assert [t["iteration"] for t in state.loop_trace] == [1, 2, 3]
    assert llm.calls == {"coder": 1, "reviewer": 3, "fixer": 2}
    assert state.code_version == 3                             # 两个修复版本

    kinds = [m.kind for m in orch.bus.history()]
    assert kinds.count("review_request") == 3
    assert kinds.count("fix_request") == 2


def test_loop_stops_at_round_budget(task: Task, tmp_path: Path) -> None:
    """一直不通过时，循环必须在 max_rounds 处熔断，不能无限转。"""
    llm = ScriptedLLM(code=GOOD_CODE, fixes=[GOOD_CODE], reviews=[REJECT])
    orch = build_orchestrator(task, tmp_path, llm, max_rounds=2)

    state = orch.run()

    assert state.phase == Phase.REJECTED
    assert state.round == 2 == state.max_rounds
    assert [t["verdict"] for t in state.loop_trace] == [VERDICT_RETRY, VERDICT_REJECTED]
    assert llm.calls["reviewer"] == 2
    assert llm.calls["fixer"] == 1                             # 最后一次驳回直接熔断，不再修复
    assert state.issues_open                                  # 遗留问题要交给人


def test_machine_verdict_keeps_loop_running_when_llm_says_ok(task: Task, tmp_path: Path) -> None:
    """LLM 每轮都说通过、但静态检查不过：机器判定优先，循环不能退出。"""
    llm = ScriptedLLM(code=NO_LOCK_CODE, fixes=[NO_LOCK_CODE], reviews=[APPROVE])
    orch = build_orchestrator(task, tmp_path, llm, max_rounds=1)

    state = orch.run()

    assert state.phase == Phase.REJECTED                       # 没被 LLM 放行
    assert state.loop_trace[0]["approved"] is False            # 结论已被机器验收改判
    assert state.loop_trace[0]["machine_ok"] is False          # 机器说不过
    assert state.loop_trace[0]["verdict"] == VERDICT_REJECTED
    assert "强制驳回" in state.review["summary"]                # 冲突留痕（审查层）


def test_resume_from_coding_redoes_first_version(task: Task, tmp_path: Path) -> None:
    """模拟「进程停在编码阶段」：续跑应重新派发编码，而不是以 CODING 草草收尾。"""
    llm = ScriptedLLM(code=GOOD_CODE, fixes=[GOOD_CODE], reviews=[APPROVE])
    orch = build_orchestrator(task, tmp_path, llm, run_id="resume-coding")

    state = WorkflowState(
        run_id=orch.run_id, task_id=task.task_id, max_rounds=3,
        phase=Phase.CODING, round=0,
    )
    orch.store.save(state)

    resumed = orch.run(resume=True)

    assert resumed.phase == Phase.APPROVED
    assert llm.calls["coder"] == 1
    assert resumed.code_version == 1
    assert [t["verdict"] for t in resumed.loop_trace] == [VERDICT_APPROVED]


def test_resume_from_fixing_finishes_fix_then_loops(task: Task, tmp_path: Path) -> None:
    """模拟「进程停在修复阶段」：续跑应先完成修复，再回到审查循环。"""
    llm = ScriptedLLM(code=GOOD_CODE, fixes=[GOOD_CODE], reviews=[APPROVE])
    orch = build_orchestrator(task, tmp_path, llm, run_id="resume-case")

    # 手工造一个「刚被打回、还没修」的状态，等价于进程在 FIXING 阶段被 kill
    state = WorkflowState(
        run_id=orch.run_id, task_id=task.task_id, max_rounds=3,
        phase=Phase.FIXING, round=1, code=GOOD_CODE, code_version=1,
        issues_open=[REJECT["issues"][0]],
    )
    orch.store.save(state)

    resumed = orch.run(resume=True)

    assert resumed.phase == Phase.APPROVED
    assert llm.calls["fixer"] == 1                              # 先补完修复
    assert [t["verdict"] for t in resumed.loop_trace] == [VERDICT_APPROVED]
    assert resumed.round == 2
