"""并行审查：两个审查 Agent 并发执行，结论取交集。

用脚本化假模型按「Agent 名字」返回不同结论，验证合并规则：
* 一个通过、一个驳回 → 合并驳回（不会因为一个说通过就放行）；
* 都通过 → 合并通过，且两个 Agent 都真的被调用过。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflow.orchestrator import Orchestrator  # noqa: E402
from workflow.task import Task  # noqa: E402

CODE = '''\
import threading
from collections import OrderedDict


class MiniCache:
    """最小可过静态检查的实现。"""

    def __init__(self, capacity: int = 8, ttl_seconds: float = 1.0) -> None:
        self._lock = threading.RLock()
        self._data = OrderedDict()

    def get(self, key):
        """读。"""
        with self._lock:
            return self._data.get(key)

    def put(self, key, value):
        """写。"""
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
'''

REJECT = {"approved": False, "score": 50, "issues": [], "summary": "主审查驳回"}
APPROVE = {"approved": True, "score": 95, "issues": [], "summary": "通过"}


class PerAgentLLM:
    provider = "scripted"

    def __init__(self, reviews):
        self.reviews = reviews
        self.calls = []

    def chat_code(self, agent: str, system: str, user: str) -> str:
        self.calls.append(agent)
        return CODE

    def chat_json(self, agent: str, system: str, user: str):
        self.calls.append(agent)
        return dict(self.reviews.get(agent, APPROVE))

    @property
    def call_log(self):
        return []


def run_case(tmp_path: Path, reviews, parallel: bool, run_id: str):
    task = Task.from_prompt("写个缓存", task_id="p", root=tmp_path / "p",
                            target_file="demo.py", module_name="demo")
    llm = PerAgentLLM(reviews)
    orch = Orchestrator(task, run_id=run_id, runs_root=tmp_path / "runs",
                        overrides={"provider": "mock"}, verbose=False,
                        llm=llm, parallel_review=parallel, max_rounds=1)
    return orch.run(), llm


def test_parallel_review_rejects_when_only_one_approves(tmp_path: Path) -> None:
    state, llm = run_case(tmp_path, {"reviewer": REJECT}, parallel=True, run_id="p1")

    assert state.phase == "REJECTED"
    assert "security_reviewer" in llm.calls and "reviewer" in llm.calls
    assert state.review["approved"] is False
    assert set(state.review["agents"]) == {"reviewer", "security_reviewer"}


def test_parallel_review_approves_when_both_agree(tmp_path: Path) -> None:
    state, llm = run_case(tmp_path, {"reviewer": APPROVE, "security_reviewer": APPROVE},
                          parallel=True, run_id="p2")

    assert state.phase == "APPROVED"
    assert state.review["approved"] is True
    assert state.review["score"] == 95


def test_sequential_mode_uses_single_reviewer(tmp_path: Path) -> None:
    state, llm = run_case(tmp_path, {"reviewer": APPROVE}, parallel=False, run_id="p3")

    assert state.phase == "APPROVED"
    assert "security_reviewer" not in llm.calls
