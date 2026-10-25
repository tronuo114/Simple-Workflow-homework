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
from typing import Any, Dict, List, Optional

from core.paths import app_root

PROJECT_ROOT = app_root()
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

    @classmethod
    def from_prompt(
        cls,
        prompt: str,
        *,
        task_id: str,
        root: Path,
        target_file: str = "solution.py",
        class_name: str = "",
        module_name: str = "solution",
    ) -> "Task":
        """把聊天框里的一句话变成一个临时任务（不写进 tasks/，只放在运行的目录里）。

        与 ``tasks/<id>/`` 的区别：没有固定验收项和测试用例。
        静态检查因此退化为「通用检查」（语法、非空、无占位），
        若生成的代码刚好实现了某个已知任务（如 TTLCache），会自动复用它的测试，
        见 :func:`infer_tests`。
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "task.md").write_text(prompt.strip() + "\n", encoding="utf-8")
        (root / "requirements.md").write_text(
            "- **REQ-1** 代码完整可运行，无 TODO / 占位实现\n"
            "- **REQ-2** 接口有说明（docstring 或注释），关键逻辑可读\n"
            "- **REQ-3** 边界情况有处理，不吞异常\n"
            "- **REQ-4** 与用户需求一致，没有多余依赖\n",
            encoding="utf-8",
        )
        meta = {
            "module_name": module_name,
            "class_name": class_name,
            "target_file": target_file,
            "tests": [],
            "from_prompt": True,
        }
        (root / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
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


def infer_tests(code: str, tasks_root: Path = TASKS_ROOT) -> Optional[str]:
    """自由输入任务：若代码里出现了某个已知任务的类名，就复用它的测试。

    例：聊天里让模型写「带过期时间的 LRU 缓存」，它定义了 ``TTLCache``，
    于是自动挂上 ``tasks/ttl_cache`` 的 25 个用例，机器验收照常生效。
    """
    for tid in Task.available(tasks_root):
        try:
            task = Task.load(tid, tasks_root)
        except Exception:  # noqa: BLE001 - 坏任务目录直接跳过
            continue
        if task.class_name and task.class_name in code and task.test_files:
            return tid
    return None
