"""验收执行器：把「代码能不能过」从嘴巴变成机器判定。

审查 Agent 是 LLM，会漏看、会顺着代码说好话。所以每版代码都要过两道自动关卡：

1. **静态检查**（AST）：类和方法签名、是否真的用了锁、有没有在 get/put 里
   线性扫描全表（O(n) 隐患）。
2. **动态验收**（pytest）：把代码铺到干净工作区，跑仓库里的测试用例。

两项结果都会塞进审查 Agent 的上下文，让它「看到证据再下结论」。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.paths import app_root

PROJECT_ROOT = app_root()


def _pytest_command(args: List[str]) -> List[str]:
    """构造 pytest 子进程命令。

    源码运行时用 ``python -m pytest``；PyInstaller 打包运行时 ``sys.executable``
    是面板 exe 自己，不能加 ``-m``，改为「exe --pytest …」转发给打包内的 pytest。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--pytest", *args]
    return [sys.executable, "-m", "pytest", *args]


# --------------------------------------------------------------------------- #
# 静态检查
# --------------------------------------------------------------------------- #
REQUIRED_METHODS = {"get", "put", "__init__"}


def _generic_checks(tree: ast.AST, code: str, add) -> None:
    """自由输入任务（聊天模式）的通用检查：只看「是不是完整的、能用的代码」。"""
    add("代码非空", len(code.strip()) >= 40, f"{len(code.strip())} 字符")
    has_def = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  for n in ast.walk(tree))
    add("包含函数或类定义", has_def, "检测到 class/def" if has_def else "没有任何定义")

    placeholders: List[str] = []
    for node in ast.walk(tree):
        # 函数体里只有 pass 或 ... 视为占位实现
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) == 1:
            only = node.body[0]
            if isinstance(only, ast.Pass):
                placeholders.append(f"{node.name} 只有 pass")
            if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) \
                    and only.value.value is Ellipsis:
                placeholders.append(f"{node.name} 是 ... 占位")
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "NotImplementedError":
            placeholders.append("抛出 NotImplementedError")
    add("没有占位实现", not placeholders, "；".join(sorted(set(placeholders))) or "未发现 pass/... 占位")


def static_check(code: str, class_name: str = "TTLCache") -> Dict[str, Any]:
    """AST 静态检查，返回 {"passed": bool, "checks": [...], "warnings": [...]}。"""
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def add(name: str, ok: bool, detail: str, *, hard: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hard": hard})
        if not ok and not hard:
            warnings.append(f"{name}: {detail}")

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {
            "passed": False,
            "checks": [{"name": "语法解析", "ok": False, "detail": str(exc), "hard": True}],
            "warnings": [],
        }

    add("语法解析", True, "ast.parse 通过")

    # 自由输入的任务（聊天模式）没有固定类名：只做通用检查
    if not class_name:
        _generic_checks(tree, code, add)
        passed = all(c["ok"] for c in checks if c["hard"])
        return {"passed": passed, "checks": checks, "warnings": warnings, "generic": True}

    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    add(f"类 {class_name} 存在", cls is not None, "顶层定义" if cls else "未找到该类的顶层定义")
    methods = {n.name for n in (cls.body if cls else []) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = REQUIRED_METHODS - methods
    add("必需方法齐全", not missing, f"缺少 {sorted(missing)}" if missing else f"{sorted(REQUIRED_METHODS)} 齐备")

    # get/put 签名
    if cls:
        get_node = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "get"), None)
        put_node = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "put"), None)
        if get_node:
            add("get 签名", len(get_node.args.args) >= 2,
                f"参数 {[a.arg for a in get_node.args.args]}")
        if put_node:
            add("put 签名", len(put_node.args.args) >= 3,
                f"参数 {[a.arg for a in put_node.args.args]}")

    # 线程安全
    lock_patterns = [
        "threading.Lock", "threading.RLock", "threading.Condition",
        "from threading import Lock", "from threading import RLock", "Lock()", "RLock()",
    ]
    has_lock = any(p in code for p in lock_patterns)
    uses_with = bool(re.search(r"with\s+self\._\w*lock\w*", code))
    add("线程安全（锁）", has_lock and uses_with,
        "检测到 with self._lock 临界区" if has_lock and uses_with
        else "未检测到锁或未用 with 保护")

    # O(1) 隐患：get/put 里出现整表线性扫描
    hot_nodes = []
    if cls:
        hot_nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("get", "put")]
    scan_hits: List[str] = []
    for fn in hot_nodes:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("sorted", "min", "max"):
                scan_hits.append(f"{fn.name}() 调用 {node.func.id}()")
            # for 循环 / 推导式遍历容器才是全表扫描；while 弹出队首属于均摊 O(1)，放行
            if isinstance(node, (ast.For, ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)):
                scan_hits.append(f"{fn.name}() 含 {type(node).__name__} 循环")
    add("get/put 无整表扫描", not scan_hits,
        "；".join(sorted(set(scan_hits))) if scan_hits else "未见 sorted/min/max 或循环", hard=False)

    # OrderedDict / 双链表迹象
    add("使用有序结构", ("OrderedDict" in code) or ("_data.move_to_end" in code),
        "OrderedDict 或 move_to_end" if ("OrderedDict" in code or "move_to_end" in code)
        else "未检测到 LRU 顺序结构", hard=False)

    # 过期条目清理
    add("含过期清理逻辑", ("expire" in code or "ttl" in code.lower()) and ("purge" in code or "del " in code),
        "检测到过期字段与删除动作" if ("purge" in code or "del " in code) else "未见过期清理",
        hard=False)

    passed = all(c["ok"] for c in checks if c["hard"])
    return {"passed": passed, "checks": checks, "warnings": warnings}


# --------------------------------------------------------------------------- #
# 动态验收
# --------------------------------------------------------------------------- #
def run_tests(workspace: Path, timeout: int = 180) -> Dict[str, Any]:
    """在 workspace 里跑 pytest，返回结构化结果。"""
    started = time.time()
    cmd = _pytest_command(["-q", "--no-header", "-p", "no:cacheprovider", "tests"])
    try:
        proc = subprocess.run(
            cmd, cwd=str(workspace), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": True, "passed": False, "returncode": -1, "duration": timeout,
            "summary": f"pytest 超时（>{timeout}s），疑似死锁", "output": "",
        }
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ran": True,
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "duration": round(time.time() - started, 2),
        "summary": _pytest_summary(output),
        "output": output.strip()[-6000:],
    }


def _pytest_summary(output: str) -> str:
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            return line.strip().strip("=").strip()
    return output.strip().splitlines()[-1][:200] if output.strip() else "无输出"


# --------------------------------------------------------------------------- #
# 组合
# --------------------------------------------------------------------------- #
def layout_workspace(ws: Path, code: str, module_name: str, test_files: Dict[str, Path]) -> Path:
    """把代码与测试铺成「模块在根、测试在 tests/」的最小可跑布局。"""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / f"{module_name}.py").write_text(code, encoding="utf-8")
    tests_dir = ws / "tests"
    tests_dir.mkdir(exist_ok=True)
    for name, src in test_files.items():
        (tests_dir / name).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
    (ws / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    return ws


def verify_standalone(
    code: str,
    *,
    module_name: str = "ttl_cache",
    class_name: str = "TTLCache",
    test_files: Optional[Dict[str, Path]] = None,
    run_tests_too: bool = True,
) -> Dict[str, Any]:
    """不依赖 runs/ 的独立验收（CLI ``verify`` 用）。"""
    import tempfile

    static = static_check(code, class_name=class_name)
    dynamic: Dict[str, Any] = {"ran": False, "passed": False, "summary": "未执行"}
    if run_tests_too and test_files:
        with tempfile.TemporaryDirectory(prefix="ttlcache-verify-") as tmp:
            ws = layout_workspace(Path(tmp), code, module_name, test_files)
            dynamic = run_tests(ws)
    passed = bool(static["passed"]) and (bool(dynamic["passed"]) if dynamic.get("ran") else True)
    return {"passed": passed, "static": static, "dynamic": dynamic,
            "summary": _join_summary(static, dynamic)}


def verify_code(
    code: str,
    store: Any,
    *,
    module_name: str = "ttl_cache",
    class_name: str = "TTLCache",
    test_files: Optional[Dict[str, Path]] = None,
    run_tests_too: bool = True,
) -> Dict[str, Any]:
    """静态 + 动态两关都过才算 passed。"""
    static = static_check(code, class_name=class_name)
    dynamic: Dict[str, Any] = {"ran": False, "passed": False, "summary": "未执行"}

    if run_tests_too and test_files:
        workspace = store.prepare_workspace(code, test_files, module_name)
        dynamic = run_tests(workspace)

    passed = bool(static["passed"]) and (bool(dynamic["passed"]) if dynamic.get("ran") else True)
    return {
        "passed": passed,
        "static": static,
        "dynamic": dynamic,
        "summary": _join_summary(static, dynamic),
    }


def _join_summary(static: Dict[str, Any], dynamic: Dict[str, Any]) -> str:
    hard_fail = [f"{c['name']}" for c in static["checks"] if c["hard"] and not c["ok"]]
    parts = [
        "静态检查：" + ("通过" if static["passed"] else "未通过(" + ", ".join(hard_fail) + ")"),
        "动态测试：" + (dynamic.get("summary", "未执行") if dynamic.get("ran") else "未执行"),
    ]
    if static.get("warnings"):
        parts.append("警告：" + "；".join(static["warnings"]))
    return " | ".join(parts)
