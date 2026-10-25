"""命令行入口。

    python -m workflow.cli run --task ttl_cache --mock      # 离线跑通整条流水线
    python -m workflow.cli run --task ttl_cache --live      # 接真模型
    python -m workflow.cli verify --task ttl_cache          # 只跑验收，不起 Agent
    python -m workflow.cli tasks                            # 看有哪些任务
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.verify import verify_standalone
from workflow.orchestrator import RUNS_ROOT, Orchestrator
from workflow.task import PROJECT_ROOT, Task

EXAMPLES = """示例
  python -m workflow.cli run --task ttl_cache --mock
  python -m workflow.cli run --task ttl_cache --live --rounds 3 --apply
  python -m workflow.cli run --task ttl_cache --resume --run-id ttl_cache-20250101-120000-001
  python -m workflow.cli verify --task ttl_cache
  python -m workflow.cli tasks
"""


def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _task_from_prompt(args: argparse.Namespace) -> Task:
    """把聊天输入变成一次性任务（不写 tasks/，任务文件放在 runs/_prompts/<id>/）。"""
    prompt = args.prompt or ""
    if args.prompt_file:
        path = Path(args.prompt_file)
        if not path.is_file():
            raise SystemExit(f"找不到 --prompt-file：{path}")
        prompt = path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise SystemExit("任务描述为空：用 --prompt 或 --prompt-file 给一句话需求")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    alias = re.sub(r"[^0-9a-zA-Z]+", "_", prompt.strip())[:24].strip("_") or "task"
    task_id = f"chat-{stamp}-{alias}"[:60]
    module_name = "chat_" + re.sub(r"[^0-9a-zA-Z]+", "_", stamp)
    root = Path(args.runs_root) / "_prompts" / task_id
    return Task.from_prompt(
        prompt, task_id=task_id, root=root,
        target_file=f"{module_name}.py", module_name=module_name,
    )


def cmd_run(args: argparse.Namespace) -> int:
    task = _task_from_prompt(args) if (args.prompt or args.prompt_file) else Task.load(args.task)
    overrides: Dict[str, Any] = {}
    if args.mock:
        overrides["provider"] = "mock"
    elif args.live:
        overrides["provider"] = "openai"
    if args.model:
        overrides["model"] = args.model
    if args.base_url:
        overrides["base_url"] = args.base_url

    events = bool(getattr(args, "events", False))
    sink = (lambda e: print(json.dumps(e, ensure_ascii=False), flush=True)) if events else None

    orch = Orchestrator(
        task,
        run_id=args.run_id,
        runs_root=Path(args.runs_root),
        max_rounds=args.rounds,
        config_path=Path(args.config) if args.config else None,
        overrides=overrides,
        verbose=not args.quiet and not events,
        dynamic_verify=not args.no_dynamic_verify,
        apply_solution=args.apply,
        event_sink=sink,
        parallel_review=True if args.parallel_review else None,
    )
    state = orch.run(resume=args.resume)

    if events:
        # 事件流模式：只输出 JSONL，人类可读摘要交给订阅方渲染
        return 0 if state.phase == "APPROVED" else 1

    print("", flush=True)
    print("=" * 66)
    print(f"run_id : {state.run_id}")
    print(f"结果   : {state.phase}  （{state.round}/{state.max_rounds} 轮）")
    print(f"报告   : {orch.store.root / 'REPORT.md'}")
    print(f"决策   : {orch.store.root / 'DECISIONS.md'}")
    print(f"产物   : {orch.store.root / 'artifacts' / 'latest.py'}")
    if state.error:
        print(f"错误   : {state.error}")
    print("=" * 66)
    return 0 if state.phase == "APPROVED" else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """链路自检：文件 / 目录 / 配置 / Key / 模型连通 / pytest / 事件流。"""
    from workflow.doctor import run_checks, summary

    checks = run_checks(live=not getattr(args, "offline", False))
    ok = all(c["ok"] for c in checks)

    if getattr(args, "events", False):
        for check in checks:
            print(json.dumps({"type": "check", **check}, ensure_ascii=False), flush=True)
        print(json.dumps({"type": "doctor", "ok": ok, "summary": summary(checks)},
                         ensure_ascii=False), flush=True)
    else:
        print("链路自检")
        for check in checks:
            mark = "✅" if check["ok"] else "❌"
            print(f"  {mark} {check['name']}: {check['detail']}")
            if not check["ok"] and check.get("hint"):
                print(f"     → {check['hint']}")
        print("")
        print(f"结论：{'✅ ' if ok else '❌ '}{summary(checks)}")
    return 0 if ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    task = Task.load(args.task)
    path = Path(args.file) if args.file else task.solution_path
    if not path.is_file():
        print(f"找不到文件：{path}")
        return 2
    code = path.read_text(encoding="utf-8")
    result = verify_standalone(
        code,
        module_name=task.module_name,
        class_name=task.class_name,
        test_files=task.test_files or None,
        run_tests_too=not args.no_tests,
    )
    print(f"文件   : {path}")
    print(f"结论   : {'✅ 通过' if result['passed'] else '❌ 未通过'}")
    print(f"摘要   : {result['summary']}")
    print("")
    for c in result["static"]["checks"]:
        mark = "✅" if c["ok"] else ("❌" if c.get("hard") else "⚠️")
        print(f"  {mark} {c['name']}: {c['detail']}")
    dyn = result["dynamic"]
    if dyn.get("ran"):
        print(f"\npytest : {dyn.get('summary')}")
        if not dyn.get("passed"):
            print(dyn.get("output", "")[-2000:])
    return 0 if result["passed"] else 1


def cmd_tasks(args: argparse.Namespace) -> int:
    ids = Task.available()
    if not ids:
        print("tasks/ 下没有任务")
        return 1
    for tid in ids:
        task = Task.load(tid)
        reqs = [ln for ln in task.requirements_text.splitlines() if ln.strip().startswith("- **REQ")]
        print(f"{tid:<16} 类={task.class_name:<12} 测试={len(task.test_files)} 个  验收项={len(reqs)} 条")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    root = Path(args.runs_root)
    if not root.is_dir():
        print("还没有任何运行记录")
        return 0
    rows = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
    for p in rows[:20]:
        state_file = p / "state.json"
        phase = "-"
        if state_file.is_file():
            import json
            try:
                phase = json.loads(state_file.read_text(encoding="utf-8")).get("phase", "-")
            except Exception:  # noqa: BLE001
                pass
        print(f"{p.name:<40} {phase}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workflow", description="三 Agent 代码审查 Workflow（编码 → 审查 → 修复）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EXAMPLES,
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="跑一次完整工作流")
    r.add_argument("--task", default="ttl_cache", help="任务 id（tasks/ 下的目录名）")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--mock", action="store_true", help="离线脚本化模式（默认）")
    g.add_argument("--live", action="store_true", help="调用真实模型（需 AGENT_API_KEY）")
    r.add_argument("--rounds", type=int, default=None, help="审查轮次上限")
    r.add_argument("--run-id", default=None, help="指定 run_id（续跑时用）")
    r.add_argument("--resume", action="store_true", help="从 state.json 续跑")
    r.add_argument("--runs-root", default=str(RUNS_ROOT), help="运行记录根目录")
    r.add_argument("--config", default=None, help="配置文件路径")
    r.add_argument("--model", default=None, help="覆盖模型名")
    r.add_argument("--base-url", default=None, help="覆盖 API base_url")
    r.add_argument("--apply", action="store_true", help="通过后覆盖 solutions/ 下的交付文件")
    r.add_argument("--no-dynamic-verify", action="store_true", help="跳过 pytest（只做静态检查）")
    r.add_argument("--prompt", default=None,
                   help="聊天模式：直接把这句话作为任务（不需要 tasks/ 目录）")
    r.add_argument("--prompt-file", default=None, help="从文件读任务描述（避免命令行转义）")
    r.add_argument("--events", action="store_true", help="输出 JSONL 事件流（给面板/程序用）")
    r.add_argument("--parallel-review", action="store_true",
                   help="开启并行审查：主审查 + 安全审查两个 Agent 同时跑")
    r.add_argument("--quiet", action="store_true", help="少打印")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("verify", help="只跑自动验收，不起 Agent")
    v.add_argument("--task", default="ttl_cache")
    v.add_argument("--file", default=None, help="待验收文件（默认 solutions/<target>）")
    v.add_argument("--no-tests", action="store_true", help="跳过 pytest")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("doctor", help="链路自检：文件 / 配置 / Key / 模型连通 / pytest")
    d.add_argument("--events", action="store_true", help="输出 JSONL 事件流")
    d.add_argument("--offline", action="store_true", help="跳过模型连通检查（不发网络请求）")
    d.set_defaults(func=cmd_doctor)

    t = sub.add_parser("tasks", help="列出任务")
    t.set_defaults(func=cmd_tasks)

    s = sub.add_parser("runs", help="列出历史运行")
    s.add_argument("--runs-root", default=str(RUNS_ROOT))
    s.set_defaults(func=cmd_runs)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
