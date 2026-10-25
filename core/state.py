"""Workflow 状态：显式状态机 + 原子落盘 + 断点续跑。

状态管理是这个作业最容易糊弄过去、也最容易出问题的部分。这里做三件事：

1. **状态只有一个真相源**：``WorkflowState`` 是一个 dataclass，阶段、轮次、
   代码、审查结论全部在里面；Agent 不各自记状态。
2. **每次迁移都落盘**：``StateStore.save`` 用「写临时文件 + 替换」保证不会写出半个
   JSON；进程被 kill 也能从 ``state.json`` 恢复。
3. **阶段迁移合法**：``can_transition`` 白名单校验，避免出现
   ``REVIEWING -> CODING`` 这类隐含 bug 的跳转。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #
class Phase:
    INIT = "INIT"
    CODING = "CODING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    ALL = [INIT, CODING, REVIEWING, FIXING, APPROVED, REJECTED, FAILED]
    TERMINAL = {APPROVED, REJECTED, FAILED}

    # 合法迁移白名单
    TRANSITIONS = {
        INIT: {CODING, FAILED},
        CODING: {REVIEWING, FAILED},
        REVIEWING: {FIXING, APPROVED, REJECTED, FAILED},
        FIXING: {REVIEWING, FAILED},
        APPROVED: set(),
        REJECTED: set(),
        FAILED: set(),
    }

    LABELS = {
        INIT: "初始化", CODING: "编码中", REVIEWING: "审查中", FIXING: "修复中",
        APPROVED: "已通过", REJECTED: "未通过", FAILED: "失败",
    }


def can_transition(src: str, dst: str) -> bool:
    return dst in Phase.TRANSITIONS.get(src, set())


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@dataclass
class WorkflowState:
    run_id: str
    task_id: str
    phase: str = Phase.INIT
    round: int = 0
    max_rounds: int = 3
    code: str = ""
    code_version: int = 1
    review: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    # 循环轨迹：每跑一次「审查 → 分支判断」追加一条，是「循环体」的唯一账本
    loop_trace: List[Dict[str, Any]] = field(default_factory=list)
    issues_open: List[Dict[str, Any]] = field(default_factory=list)
    issues_resolved: List[Dict[str, Any]] = field(default_factory=list)
    dec_ids: List[str] = field(default_factory=list)
    provider: str = "mock"
    model: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # -- 迁移 --------------------------------------------------------------- #
    def transition(self, dst: str, *, reason: str = "") -> None:
        if not can_transition(self.phase, dst):
            raise IllegalTransition(f"非法状态迁移 {self.phase} -> {dst}（{reason}）")
        self.phase = dst
        self.touch()

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")

    # -- 序列化 ------------------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary_line(self) -> str:
        return (f"[{self.phase:<9}] round={self.round}/{self.max_rounds} "
                f"code_v{self.code_version} ({len(self.code)} B) "
                f"open_issues={len(self.issues_open)}")


class IllegalTransition(RuntimeError):
    """状态机迁移非法。"""


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
class StateStore:
    """一次运行的目录布局：

    runs/<run_id>/
        state.json       当前状态（唯一真相源）
        messages.jsonl   全部 Agent 消息
        decisions.jsonl  本次运行的全部 DEC 记录
        artifacts/       每一版代码快照
        workspace/       跑测试用的临时工作区
        REPORT.md        运行报告（结束后生成）
    """

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = Path(root) / run_id
        self.run_id = run_id
        self.state_path = self.root / "state.json"
        self.messages_path = self.root / "messages.jsonl"
        self.decisions_path = self.root / "decisions.jsonl"
        self.artifacts_dir = self.root / "artifacts"
        self.workspace_dir = self.root / "workspace"
        for d in (self.root, self.artifacts_dir, self.workspace_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- 状态 --------------------------------------------------------------- #
    def save(self, state: WorkflowState) -> None:
        state.touch()
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)  # 原子替换，避免写出半个 JSON

    def load(self) -> WorkflowState:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return WorkflowState.from_dict(data)

    def exists(self) -> bool:
        return self.state_path.is_file()

    # -- 代码快照 ----------------------------------------------------------- #
    def save_artifact(self, code: str, label: str) -> Path:
        path = self.artifacts_dir / f"{label}.py"
        path.write_text(code, encoding="utf-8")
        latest = self.artifacts_dir / "latest.py"
        latest.write_text(code, encoding="utf-8")
        return path

    def prepare_workspace(self, code: str, test_files: Dict[str, Path], module_name: str) -> Path:
        """按「模块在根、测试在 tests/」的布局铺一个干净工作区。"""
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / f"{module_name}.py").write_text(code, encoding="utf-8")
        tests_dir = self.workspace_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        for name, src in test_files.items():
            (tests_dir / name).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
        (self.workspace_dir / "conftest.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
            encoding="utf-8",
        )
        return self.workspace_dir
