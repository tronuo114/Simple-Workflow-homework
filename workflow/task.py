"""任务定义：把「题目」变成编排器能读的结构。

任务放在 ``tasks/<task_id>/`` 下，四个文件各管一件事：

* ``task.md``         人读的需求说明（也就是喂给编码 Agent 的题面）
* ``requirements.md`` 机器与人都能对照的验收项，编号 REQ-1…REQ-n
* ``meta.json``       测试文件、模块名、类名、轮次上限
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import app_root

PROJECT_ROOT = app_root()
TASKS_ROOT = PROJECT_ROOT / "tasks"

# 测试源码塞进 Prompt 的上限：够长到看清每个用例，又不至于挤掉需求本身。
CONTRACT_LIMIT = 14000


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
        静态检查因此退化为「通用检查」（语法、非空、无占位）。

        但如果这句话里已经点名了某个已知任务的类名（典型场景：
        「写一个带过期时间的 LRU 缓存 … class TTLCache」），就直接沿用那个任务的
        模块名、类名、验收项与测试套件——测试里写了 ``TTLCache(..., clock=clock)``
        这种接口约定，Agent 看不到测试就只能靠猜，猜出来的签名必然对不上。
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "task.md").write_text(prompt.strip() + "\n", encoding="utf-8")
        requirements = (
            "- **REQ-1** 代码完整可运行，无 TODO / 占位实现\n"
            "- **REQ-2** 接口有说明（docstring 或注释），关键逻辑可读\n"
            "- **REQ-3** 边界情况有处理，不吞异常\n"
            "- **REQ-4** 与用户需求一致，没有多余依赖\n"
        )
        meta: Dict[str, Any] = {
            "module_name": module_name,
            "class_name": class_name,
            "target_file": target_file,
            "tests": [],
            "from_prompt": True,
        }

        known = _match_known_task(prompt)
        if known is not None:
            meta["module_name"] = known.module_name
            meta["class_name"] = known.class_name
            meta["tests"] = list(known.meta.get("tests", []))
            meta["adopted_from"] = known.task_id
            requirements = known.requirements_text or requirements

        (root / "requirements.md").write_text(requirements, encoding="utf-8")
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

    @property
    def contract_text(self) -> str:
        """机器验收契约：测试里实际怎么调用这个类 + 测试源码本身。

        为什么要把测试源码给 Agent：验收跑的就是这些用例，接口细节（关键字参数、
        ``get(key, default)``、``stats()`` / ``purge_expired()`` 等）只写在测试里。
        不给它，LLM 只能凭题面猜，猜错了就是「静态检查全绿、pytest 全 ERROR」。
        """
        files = self.test_files
        if not files:
            return ""
        blocks: List[str] = []
        summary = _api_summary(self.class_name, files)
        if summary:
            blocks.append(summary)
        blocks.append("**下面的 pytest 用例就是机器验收，必须全部通过：**")
        for name, path in files.items():
            try:
                src = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            blocks.append(f"### {name}\n```python\n{src}\n```")
        text = "\n\n".join(blocks)
        if len(text) > CONTRACT_LIMIT:
            text = text[:CONTRACT_LIMIT] + "\n\n...（测试源码过长，已截断）"
        return text


def _match_known_task(prompt: str, tasks_root: Path = TASKS_ROOT) -> Optional["Task"]:
    """需求文本里点名了哪个已知任务（按类名匹配），没有就返回 None。"""
    for tid in Task.available(tasks_root):
        try:
            task = Task.load(tid, tasks_root)
        except Exception:  # noqa: BLE001 - 坏任务目录直接跳过
            continue
        if task.class_name and task.class_name in prompt and task.test_files:
            return task
    return None


def _api_summary(class_name: str, files: Dict[str, Path]) -> str:
    """从测试源码里抽出「这个类必须长什么样」，给 Agent 一个可核对的清单。

    只做保守提取：构造函数的**关键字参数**、被当作实例用的变量上的方法和属性、
    以及用例名。读不出来的东西不猜，交给下面的测试源码本身。
    """
    if not class_name:
        return ""
    ctor_kwargs: List[str] = []
    methods: List[str] = []
    attrs: List[str] = []
    test_names: List[str] = []

    for path in files.values():
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue

        # 第一遍：哪些变量是「这个类的实例」——直接由构造函数赋值的，
        # 以及测试函数接收的、内部构造了该类实例的 fixture 参数。
        instance_vars: set = set()
        fixture_vars: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == class_name:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            instance_vars.add(target.id)
            if isinstance(node, ast.FunctionDef):
                is_fixture = any(
                    (isinstance(d, ast.Attribute) and d.attr == "fixture")
                    or (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "fixture")
                    for d in node.decorator_list
                )
                builds_instance = any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == class_name
                    for n in ast.walk(node)
                )
                if is_fixture and builds_instance:
                    fixture_vars.add(node.name)

        # 第二遍：用例名、构造参数、实例上的方法与属性
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                test_names.append(node.name)
                instance_vars.update(a.arg for a in node.args.args if a.arg in fixture_vars)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == class_name:
                for kw in node.keywords:
                    if kw.arg and kw.arg not in ctor_kwargs:
                        ctor_kwargs.append(kw.arg)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in instance_vars:
                    _add(methods, func.attr)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in instance_vars and node.attr.startswith("_"):
                    _add(attrs, node.attr)

    lines: List[str] = []
    if ctor_kwargs:
        shown = ", ".join(f"{k}=..." for k in ctor_kwargs)
        lines.append(f"- 构造签名必须接受这些关键字参数：{class_name}({shown})")
    if methods:
        lines.append("- 实例上会出现这些方法调用：" + ", ".join(methods))
    if attrs:
        lines.append("- 测试会直接读这些内部属性（名字不能改）：" + ", ".join(attrs))
    if test_names:
        lines.append(f"- 共 {len(test_names)} 个用例必须全绿，例如："
                     + ", ".join(test_names[:6]) + (" …" if len(test_names) > 6 else ""))
    if not lines:
        return ""
    return "## 接口契约（从验收测试自动提取）\n\n" + "\n".join(lines)


def _add(bucket: List[str], value: str) -> None:
    if value not in bucket:
        bucket.append(value)


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
