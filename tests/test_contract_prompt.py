"""验收契约测试：Agent 必须看到「测试到底怎么调用这个类」。

背景（2026-10-25 的真实事故）：
聊天里输入「写一个带过期时间的 LRU 缓存」，编码 Agent 写出的 ``TTLCache`` 静态检查
全绿，但 pytest 25 个用例全部 ERROR —— 因为测试写的是
``TTLCache(capacity=3, ttl_seconds=10.0, clock=clock)``，还调用了
``purge_expired()`` / ``stats()`` / ``items()`` / ``clear()``。
这些接口只存在于测试里，Agent 从没看到测试，只能靠猜；修复 Agent 也只拿到
「构造函数 TypeError」这种转述，注定改不对。

所以这里锁死三件事：
1. 需求里点名 ``TTLCache`` → 一次性任务直接沿用 ``tasks/ttl_cache`` 的测试与验收项；
2. 编码 Agent 的 Prompt 里带完整验收契约（含 ``clock`` / ``purge_expired``）；
3. 修复 Agent 的 Prompt 里带上次 pytest 的原始报错，而不是转述。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workflow.orchestrator import Orchestrator  # noqa: E402
from workflow.task import PROJECT_ROOT, Task  # noqa: E402

PROMPT = """题目：实现一个「带过期时间的 LRU 缓存」
需求：
class TTLCache:
    def __init__(self, capacity: int, ttl_seconds: float): ...
    def get(self, key) -> Any: ...
    def put(self, key, value) -> None: ...
"""

# 能过静态检查、但接口和测试对不上的首版：故意不接 clock，也没有 purge_expired
BROKEN = '''\
import threading
import time
from collections import OrderedDict


class TTLCache:
    """首版：漏掉了测试要求的 clock 注入与 purge_expired。"""

    def __init__(self, capacity: int = 128, ttl_seconds: float = 60.0) -> None:
        self._capacity = capacity
        self._ttl = float(ttl_seconds)
        self._data = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expire_at = item
            if time.monotonic() >= expire_at:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key, value):
        with self._lock:
            now = time.monotonic()
            if key in self._data:
                self._data[key] = (value, now + self._ttl)
                self._data.move_to_end(key)
                return
            while len(self._data) >= self._capacity:
                self._data.popitem(last=False)
            self._data[key] = (value, now + self._ttl)
'''

REVIEW_REJECT = {
    "approved": False,
    "score": 40,
    "issues": [{"id": "R1", "severity": "critical", "requirement": "REQ-2",
                "location": "TTLCache.__init__", "problem": "接口与测试不符",
                "suggestion": "按验收测试实现"}],
    "summary": "先让 pytest 跑起来",
}

APPROVE = {"approved": True, "score": 95, "issues": [], "summary": "全部验收项满足"}


class RecordingLLM:
    """记录每个 Agent 收到的 Prompt，按角色返回预设内容。"""

    provider = "recording"

    def __init__(self, *, first: str, fixed: str, reviews) -> None:
        self._first = first
        self._fixed = fixed
        self._reviews = list(reviews)
        self.prompts = {"coder": [], "fixer": [], "reviewer": []}

    def chat_code(self, agent: str, system: str, user: str) -> str:
        self.prompts.setdefault(agent, []).append(user)
        return self._first if agent == "coder" else self._fixed

    def chat_json(self, agent: str, system: str, user: str):
        self.prompts.setdefault(agent, []).append(user)
        return dict(self._reviews.pop(0) if len(self._reviews) > 1 else self._reviews[0])

    @property
    def call_log(self):
        return []


def test_prompt_task_adopts_known_suite(tmp_path: Path) -> None:
    """需求里点名 TTLCache → 直接挂上 ttl_cache 的验收项与 25 个用例。"""
    task = Task.from_prompt(PROMPT, task_id="chat-lru", root=tmp_path / "prompt")

    assert task.class_name == "TTLCache"
    assert task.module_name == "ttl_cache"          # 测试里 `from ttl_cache import TTLCache`
    assert list(task.test_files) == ["test_ttl_cache.py"]
    assert task.meta.get("adopted_from") == "ttl_cache"
    assert "REQ-6" in task.requirements_text         # 用的是真验收项，不是通用模板

    contract = task.contract_text
    assert "clock=..." in contract                   # 构造函数的关键字参数
    assert "purge_expired" in contract               # 测试会调用的方法
    assert "_expiry" in contract                     # 测试直接读的内部属性
    assert "def test_expiry_queue_stays_bounded" in contract


def test_generic_prompt_has_no_contract(tmp_path: Path) -> None:
    """与已知任务无关的需求：不挂测试，契约为空（静态检查退化为通用检查）。"""
    task = Task.from_prompt("帮我写两个小函数 add 和 safe_div",
                            task_id="chat-fn", root=tmp_path / "prompt")

    assert task.class_name == ""
    assert task.test_files == {}
    assert task.contract_text == ""


def test_case_name_only_in_code_still_gets_contract_before_fix(tmp_path: Path) -> None:
    """随手一句话（不含类名）：首版代码命中已知任务时，必须在**修复之前**补上契约。

    这是 2026-10-25 熔断的另一半原因：首轮验收靠 ``infer_tests`` 挂了测试，
    但任务本身仍「没有测试」，于是修复 Agent 的契约是空的。
    """
    task = Task.from_prompt("写一个带过期时间的 LRU 缓存，线程安全",
                            task_id="chat-casual", root=tmp_path / "prompt")
    assert task.test_files == {}                     # 需求里没有类名，开局不挂
    assert task.contract_text == ""

    fixed = (PROJECT_ROOT / "solutions" / "ttl_cache.py").read_text(encoding="utf-8")
    llm = RecordingLLM(first=BROKEN, fixed=fixed, reviews=[REVIEW_REJECT, APPROVE])
    orch = Orchestrator(
        task,
        run_id="chat-casual-run",
        runs_root=tmp_path / "runs",
        overrides={"provider": "mock"},
        verbose=False,
        llm=llm,
    )
    state = orch.run()

    assert task.meta.get("adopted_from") == "ttl_cache"    # 首版代码把套件挂上了
    assert list(task.test_files) == ["test_ttl_cache.py"]
    assert "REQ-6" in task.requirements_text                # 验收项也换成真的
    assert state.phase == "APPROVED"
    assert state.round == 2

    fixer_prompt = llm.prompts["fixer"][0]
    assert "clock=..." in fixer_prompt                      # 修复轮拿到契约
    assert "unexpected keyword argument" in fixer_prompt    # 以及原始报错


def test_coder_and_fixer_receive_acceptance_contract(tmp_path: Path) -> None:
    """首版接口对不上 → 修复拿到的必须是「契约 + 原始 pytest 报错」，修完即通过。"""
    task = Task.from_prompt(PROMPT, task_id="chat-lru-run", root=tmp_path / "prompt")
    fixed = (PROJECT_ROOT / "solutions" / "ttl_cache.py").read_text(encoding="utf-8")
    llm = RecordingLLM(first=BROKEN, fixed=fixed, reviews=[REVIEW_REJECT, APPROVE])

    events = []
    orch = Orchestrator(
        task,
        run_id="chat-contract-case",
        runs_root=tmp_path / "runs",
        overrides={"provider": "mock"},
        verbose=False,
        llm=llm,
        event_sink=events.append,
    )
    state = orch.run()

    # 第一轮机器验收就该认出「TTL 测试套件」并失败，第二轮修复后通过
    assert state.phase == "APPROVED"
    assert state.round == 2
    assert state.verification["tests_from"] == "ttl_cache"
    assert state.verification["passed"] is True

    coder_prompt = llm.prompts["coder"][0]
    assert "clock=..." in coder_prompt
    assert "purge_expired" in coder_prompt

    fixer_prompt = llm.prompts["fixer"][0]
    assert "验收测试" in fixer_prompt
    assert "clock=..." in fixer_prompt
    assert "pytest" in fixer_prompt
    assert "unexpected keyword argument" in fixer_prompt   # 原始报错，不是转述

    review_prompt = llm.prompts["reviewer"][0]
    assert "验收测试" in review_prompt

    verification_event = next(e for e in events if e["type"] == "verification")
    assert verification_event["tests_from"] == "ttl_cache"

    # 报告要能看出「用的是哪套测试」与「第几轮」，而不是 None / 不存在的路径
    assert state.reviews[0]["round"] == 1
    report = (orch.store.root / "REPORT.md").read_text(encoding="utf-8")
    assert "tasks/ttl_cache" in report
    assert "25 passed" in report
    assert "| 1 | 1 |" in report          # 审查记录首行：轮次 1、驳回
