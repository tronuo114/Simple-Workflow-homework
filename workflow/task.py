"""任务定义：把「题目」变成编排器能读的结构。

任务放在 ``tasks/<task_id>/`` 下，四个文件各管一件事：

* ``task.md``         人读的需求说明（也就是喂给编码 Agent 的题面）
* ``requirements.md`` 机器与人都能对照的验收项，编号 REQ-1…REQ-n
* ``meta.json``       测试文件、模块名、类名、轮次上限
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = PROJECT_ROOT / "tasks"


@dataclass
class Task:
    task_id: str
    root: Path
    meta: Dict[str, Any]

    # -- 加载 --------------------------------------------------------------- #
    @classmethod
    def load(cls, task_id: str, tasks_root: Path = TASKS_ROOT) -> "Task":
        root = Path(tasks_root) / task_id
        if not (root / "task.md").is_file():
            raise FileNotFoundError(f"找不到任务 {task_id}：{root / 'task.md'} 不存在")
        meta_path = root / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        meta.setdefault("module_name", task_id)
        meta.setdefault("class_name", "TTLCache")
        meta.setdefault("target_file", f"{task_id}.py")
        meta.setdefault("tests", [])
        return cls(task_id=task_id, root=root, meta=meta)

    @staticmethod
    def available(tasks_root: Path = TASKS_ROOT) -> List[str]:
        if not Path(tasks_root).is_dir():
            return []
        return sorted(p.name for p in Path(tasks_root).iterdir() if (p / "task.md").is_file())

    # -- 内容 --------------------------------------------------------------- #
    @property
    def spec_text(self) -> str:
        return (self.root / "task.md").read_text(encoding="utf-8").strip()

    @property
    def requirements_text(self) -> str:
        path = self.root / "requirements.md"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    @property
    def module_name(self) -> str:
        return self.meta["module_name"]

    @property
    def class_name(self) -> str:
        return self.meta["class_name"]

    @property
    def target_file(self) -> str:
        return self.meta["target_file"]

    @property
    def test_files(self) -> Dict[str, Path]:
        """{文件名: 路径}，测试文件默认从仓库 tests/ 取。"""
        out: Dict[str, Path] = {}
        for name in self.meta.get("tests", []):
            path = PROJECT_ROOT / "tests" / name
            if path.is_file():
                out[name] = path
        return out

    @property
    def solution_path(self) -> Path:
        return PROJECT_ROOT / "solutions" / self.target_file
