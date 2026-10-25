"""Workflow 编排器：显式循环体 + 状态机 + 消息驱动 + 验收把关。

拓扑（代码审查流程）
--------------------
整个流程只有一个核心结构：**一个循环体，直到代码审核通过**。

::

    INIT ──task──▶ coder ──code──▶ REVIEWING  ◀────────────────────────┐
                                        │                              │
                                        ▼                              │
                              ┌─ 循环体（每次 = 1 轮）─────────────────┐ │
                              │  1. 机器验收（AST + pytest）          │ │
                              │  2. reviewer 出结论（review_request） │ │
                              │  3. 分支判断：                        │ │
                              │     approved 且机器过 ──▶ break 出循环 │ │
                              │     不通过且还有预算 ───▶ 4. 修复 ────┼─┘
                              │     不通过且预算耗尽 ───▶ break 熔断   │
                              └───────────────────────────────────────┘
                                        │
                                        ▼
                              APPROVED / REJECTED

三条不可让步的规则：

1. **机器验收优先于模型判断**：LLM 说通过、测试是红的，按不通过处理。
2. **循环必须有出口**：只有「审查通过 + 机器验收通过」才离开循环；预算耗尽走熔断出口，
   异常走 FAILED 出口，不存在无限循环。
3. **每次决策都留 DEC，每次循环都留轨迹**：进入修复、强制驳回、熔断、产物落地全部带编号落盘，
   每轮的分支结果同时写进 ``state.loop_trace``，报告里渲染成「循环轨迹」表。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from agents.registry import build_pipeline
from core.bus import Message, MessageBus
from core.dec import DecRecorder
from core.llm_client import LLMClient, load_config
from core.state import Phase, StateStore, WorkflowState
from core.verify import verify_code
from workflow.report import build_report
from workflow.task import PROJECT_ROOT, Task, infer_tests

RUNS_ROOT = PROJECT_ROOT / "runs"

# 循环体的三种分支结果（也是循环的三个出口/去向）
VERDICT_APPROVED = "approved"   # 审核通过 → 离开循环
VERDICT_REJECTED = "rejected"   # 预算耗尽 → 熔断离开循环
VERDICT_RETRY = "retry"         # 未通过且还有预算 → 修复后回到循环开头


class WorkflowError(RuntimeError):
    """编排过程中不可恢复的错误。"""


class Orchestrator:
    def __init__(
        self,
        task: Task,
        *,
        run_id: Optional[str] = None,
        runs_root: Path = RUNS_ROOT,
        max_rounds: Optional[int] = None,
        config_path: Optional[Path] = None,
        overrides: Optional[Dict[str, str]] = None,
        verbose: bool = True,
        dynamic_verify: bool = True,
        apply_solution: bool = False,
        llm: Optional[LLMClient] = None,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        parallel_review: Optional[bool] = None,
    ) -> None:
        self.task = task
        self.runs_root = Path(runs_root)
        self.run_id = run_id or self._new_run_id(task.task_id)
        self.verbose = verbose
        self.dynamic_verify = dynamic_verify
        self.apply_solution = apply_solution
        self.event_sink = event_sink

        self.config = load_config(config_path, overrides)
        if max_rounds:
            self.config.setdefault("limits", {})["max_rounds"] = max_rounds
        self.max_rounds = int(self.config.get("limits", {}).get("max_rounds", 3))
        if parallel_review is None:
            parallel_review = bool(self.config.get("limits", {}).get("parallel_review", False))
        self.parallel_review = bool(parallel_review)

        # 模型客户端可注入：单测里换成脚本化假模型，不必真的调 API
        self.llm = llm or LLMClient(self.config, verbose=verbose)
        self.store = StateStore(self.runs_root, self.run_id)
        self.bus = MessageBus(self.run_id, self.store.messages_path)
        self.dec = DecRecorder(f"run:{self.run_id}", self.store.root,
                               title=f"运行决策 · {self.run_id}")
        # 流水线由 config["pipeline"] 决定，新增 Agent 不需要改这里（见 agents/registry.py）
        pipeline = build_pipeline(self.config, self.llm, self.bus,
                                  verbose=verbose, parallel=self.parallel_review)
        self.coder = pipeline["coder"]
        self.reviewer = pipeline["reviewer"]
        self.extra_reviewers = pipeline["extra_reviewers"]
        self.security_reviewer = self.extra_reviewers[0] if self.extra_reviewers else None
        self.fixer = pipeline["fixer"]

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_run_id(task_id: str) -> str:
        return f"{task_id}-{datetime.now():%Y%m%d-%H%M%S}-{datetime.now().microsecond // 1000:03d}"

    def run(self, resume: bool = False) -> WorkflowState:
        """跑完整条流水线。``resume=True`` 时从 state.json 接着跑。

        结构上只有三件事：**准备**（新跑 / 续跑）→ **循环体**（直到审核通过）→ **收尾**（出报告）。
        """
        state = self._prepare(resume)
        if state.phase in Phase.TERMINAL:
            return self._finish(state)

        try:
            self._loop_until_approved(state)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要留下终态
            self._fail(state, exc)

        return self._finish(state)

    # ------------------------------------------------------------------ #
    # 准备 / 循环体
    # ------------------------------------------------------------------ #
    def _prepare(self, resume: bool) -> WorkflowState:
        """新跑：建状态 → 记首条 DEC → 编码 Agent 出第一版；续跑：直接读盘。"""
        if resume and self.store.exists():
            state = self.store.load()
            self._load_message_history()
            self._log(f"续跑 {self.run_id}：从 {state.phase} 继续（round={state.round}）")
            self.dec.record(
                title="断点续跑",
                context=f"从 state.json 恢复运行，当前阶段 {state.phase}，已审查 {state.round} 轮。",
                options=["从头重跑（干净但费钱）", "从持久化状态续跑"],
                decision="从持久化状态续跑",
                rationale="状态已原子落盘，重跑会浪费已经产生的模型调用，且丢失去向可追溯性。",
                consequences="需要保证 Agent 的入参完全来自 state，而不依赖内存里的临时变量。",
                phase=state.phase, tags=["resume"],
            )
            # 停在编码阶段：编码未完成，重新派发（否则续跑会直接以 CODING 收尾）
            if state.phase == Phase.CODING:
                try:
                    self._phase_coding(state)
                except Exception as exc:  # noqa: BLE001
                    self._fail(state, exc)
            return state

        state = WorkflowState(
            run_id=self.run_id, task_id=self.task.task_id,
            max_rounds=self.max_rounds,
            provider=self.llm.provider, model=str(self.config.get("model", "")),
        )
        self.store.save(state)
        self._record_start(state)
        self._emit("session", run_id=self.run_id, task_id=self.task.task_id,
                   provider=self.llm.provider, model=str(self.config.get("model", "")),
                   max_rounds=self.max_rounds, class_name=self.task.class_name,
                   target_file=self.task.target_file, parallel_review=self.parallel_review)
        try:
            self._phase_coding(state)
        except Exception as exc:  # noqa: BLE001 - 编码阶段失败也要留下终态
            self._fail(state, exc)
        return state

    def _loop_until_approved(self, state: WorkflowState) -> WorkflowState:
        """核心循环体：审查 → 条件分支 →（不通过）修复 → 回到审查，直到代码审核通过。

        **这就是作业要求的「循环体」**：不使用「编码一次、审查一次」的单次调用，
        而是把「审查 + 修复」放进 ``while`` 里反复执行，循环条件由审查结论驱动。

        三种出口全部显式：
        * ``VERDICT_APPROVED``：审查通过且机器验收通过 —— 唯一的正常出口；
        * ``VERDICT_REJECTED``：预算耗尽 —— 熔断出口，交出遗留问题；
        * 异常 —— 由 ``run()`` 兜住转 ``FAILED``，本轮状态已落盘，可 ``--resume``。
        """
        # 续跑时若停在修复阶段，先把上一轮的修复做完，再进循环
        if state.phase == Phase.FIXING:
            self._phase_fix(state)

        while state.phase == Phase.REVIEWING:
            round_no = state.round + 1
            self._log(f"循环第 {round_no} 次：审查 → 条件分支")

            verdict = self._phase_review(state)      # 一次审查 + 分支判断
            self._trace_loop(state, round_no, verdict)

            if verdict != VERDICT_RETRY:
                break                                # 通过 / 熔断 → 离开循环
            self._phase_fix(state)                   # 未通过 → 修复 → 回到 while 条件

        return state


    # ------------------------------------------------------------------ #
    # 阶段
    # ------------------------------------------------------------------ #
    def _phase_coding(self, state: WorkflowState) -> None:
        # 新跑：INIT → CODING；续跑：阶段已是 CODING（上次在编码中途中断），直接重新派发
        if state.phase == Phase.INIT:
            state.transition(Phase.CODING, reason="派发任务给编码 Agent")
            self.store.save(state)
        else:
            self._log("续跑：重新派发编码任务（不重复迁移阶段）")
        self._status(state)

        self.bus.publish(
            "orchestrator", self.coder.name, "task",
            {
                "task_spec": self.task.spec_text,
                "requirements": self.task.requirements_text,
                "test_contract": self.task.contract_text,
                "target_file": self.task.target_file,
                "class_name": self.task.class_name,
                "carry_over": "",
            },
            round=0,
        )
        code = self._expect_code(state, self.coder, "task", author="coder")
        state.code = code
        state.code_version = 1
        self.store.save_artifact(code, "v1-coder")
        state.transition(Phase.REVIEWING, reason="首版代码就绪")
        self.store.save(state)
        self._log(f"首版代码 {len(code)} 字符，进入审查")
        self._emit("code", agent="coder", version=1, chars=len(code),
                   lines=code.count("\n") + 1, code=code)

    def _phase_review(self, state: WorkflowState) -> str:
        """跑一轮审查，并做分支判断。返回 ``VERDICT_*`` 之一，供循环体决定是否继续。"""
        state.round += 1
        self.store.save(state)

        # 1) 先跑机器验收，把证据摆到审查 Agent 面前
        state.verification = self._verify(state.code)
        self._log(f"自动验收：{state.verification['summary']}")
        self._emit("verification", **self._verification_event(state.verification))

        # 2) 请审查 Agent 出结论（parallel_review 时两个审查 Agent 并行）
        self._status(state)
        review = self._run_reviewers(state)
        state.review = review
        state.reviews.append(review)
        state.issues_open = list(review["issues"])
        self.store.save(state)
        self._emit("review", approved=review["approved"], score=review["score"],
                   summary=review["summary"], issues=review["issues"],
                   agents=review.get("agents", ["reviewer"]))

        machine_ok = bool(state.verification.get("passed"))
        approved = review["approved"] and machine_ok

        # 3) 模型与机器打架时，以机器为准
        if review["approved"] and not machine_ok:
            self.dec.record(
                title="审查通过但自动验收未过，强制驳回",
                context=(f"第 {state.round} 轮：审查 Agent 判定通过（评分 {review['score']}），"
                         f"但自动验收未通过：{state.verification.get('summary')}"),
                options=["相信 LLM 结论放行", "以机器验收为准驳回并进入修复"],
                decision="驳回，转入 FIXING",
                rationale="测试与静态检查是可复现的客观事实，LLM 结论不可复现；作业验收看的是测试绿。",
                consequences="多花一轮调用；但杜绝「报告漂亮、代码跑不起来」的情况。",
                phase=Phase.REVIEWING, tags=["guard"],
            )
            state.issues_open = list(state.issues_open) + [{
                "id": "R-AUTO", "severity": "high", "requirement": "",
                "location": "自动验收", "problem": state.verification.get("summary", ""),
                "suggestion": "让 pytest 全绿后再提交审查",
            }]

        if approved:
            self.dec.record(
                title=f"第 {state.round} 轮审查通过",
                context=(f"评分 {review['score']}；问题 {len(review['issues'])} 条；"
                         f"自动验收：{state.verification.get('summary')}"),
                options=["放行", "继续打磨"],
                decision="放行，进入 APPROVED",
                rationale="审查无 high 及以上问题，且自动验收全绿，满足全部验收项。",
                consequences="代码定格为 v%d；遗留 low 级别建议不再处理。" % state.code_version,
                phase=Phase.REVIEWING, tags=["verdict"],
            )
            state.transition(Phase.APPROVED, reason="审查通过且机器验收通过")
            self.store.save(state)
            self._log("✅ 审查通过，退出循环")
            return VERDICT_APPROVED

        # 4) 未通过：还能修就修，不能修就熔断
        if state.round >= state.max_rounds:
            self.dec.record(
                title=f"达到轮次上限 {state.max_rounds}，熔断",
                context=(f"第 {state.round} 轮仍未通过：{review['summary']}；"
                         f"遗留问题 {len(state.issues_open)} 条。"),
                options=["继续无限重试", "提高上限再试", "打住，输出遗留问题交人工"],
                decision="熔断，状态置为 REJECTED",
                rationale="没有上限的重试会烧钱且无收敛保证；把问题清单交给人比第 4 次同样失败有价值。",
                consequences="任务不算完成；但产出了可复核的问题清单与代码快照。",
                phase=Phase.REVIEWING, tags=["budget"],
            )
            state.transition(Phase.REJECTED, reason="达到轮次上限")
            self.store.save(state)
            self._log("❌ 未通过（已达轮次上限），熔断退出循环")
            return VERDICT_REJECTED

        self.dec.record(
            title=f"第 {state.round} 轮审查不通过，转入修复",
            context=(f"评分 {review['score']}；问题 {len(state.issues_open)} 条："
                     + "；".join(f"{i.get('id')}:{i.get('problem', '')[:40]}" for i in state.issues_open[:4])),
            options=["让编码 Agent 重写", "让修复 Agent 定点改", "打回人工"],
            decision="派给 FIXING 做定点修复",
            rationale="重写会把已通过的部分改坏；定向修复保留结构，审查意见与改动一一对应，便于下一轮核对。",
            consequences="代码版本 +1；若问题涉及整体设计，下一轮审查可能再次驳回。",
            phase=Phase.REVIEWING, tags=["loop"],
        )
        state.transition(Phase.FIXING, reason="审查不通过")
        self.store.save(state)
        self._log(f"↩ 审查未通过（{len(state.issues_open)} 条问题），进入修复，随后回到循环开头")
        return VERDICT_RETRY

    def _phase_fix(self, state: WorkflowState) -> None:
        self.bus.publish(
            "orchestrator", self.fixer.name, "fix_request",
            {
                "code": state.code,
                "issues": state.issues_open,
                "requirements": self.task.requirements_text,
                # 修复 Agent 必须看到「验收怎么跑、上次跑到哪一步炸的」：
                # 只给它 LLM 转述过的问题，它会照着措辞猜，改不动真正的签名/断言。
                "test_contract": self.task.contract_text,
                "verification": state.verification,
                "class_name": self.task.class_name,
                "target_file": self.task.target_file,
            },
            round=state.round,
        )
        code = self._expect_code(state, self.fixer, "fix_request", author="fixer")
        state.code = code
        state.code_version += 1
        self.store.save_artifact(code, f"v{state.code_version}-fixer")
        state.transition(Phase.REVIEWING, reason="修复完成，回到审查")
        self.store.save(state)
        self._log(f"修复完成，代码 v{state.code_version}，回到循环开头（审查）")
        self._emit("code", agent="fixer", version=state.code_version, chars=len(code),
                   lines=code.count("\n") + 1, code=code)

    def _trace_loop(self, state: WorkflowState, iteration: int, verdict: str) -> None:
        """把本轮「审查 → 分支判断」的结果记进循环轨迹，供报告与复盘使用。"""
        review = state.review or {}
        machine_ok = bool((state.verification or {}).get("passed"))
        action = {
            VERDICT_APPROVED: "放行，退出循环",
            VERDICT_REJECTED: "熔断，预算耗尽退出循环",
            VERDICT_RETRY: "派修复，回到循环开头",
        }.get(verdict, verdict)

        state.loop_trace.append({
            "iteration": iteration,
            "round": state.round,
            "verdict": verdict,
            "phase": state.phase,
            "approved": bool(review.get("approved")),
            "machine_ok": machine_ok,
            "score": review.get("score", 0),
            "issues": len(state.issues_open),
            "action": action,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })
        self.store.save(state)
        self._log(f"分支：{action}（llm_approved={bool(review.get('approved'))}，"
                  f"machine_ok={machine_ok}，round={state.round}/{state.max_rounds}）")
        self._emit("loop", iteration=iteration, round=state.round, verdict=verdict,
                   action=action, approved=bool(review.get("approved")),
                   machine_ok=machine_ok, score=review.get("score", 0),
                   issues=len(state.issues_open))


    # ------------------------------------------------------------------ #
    # 审查（可并行）
    # ------------------------------------------------------------------ #
    def _run_reviewers(self, state: WorkflowState) -> Dict[str, Any]:
        """请审查 Agent 出结论。``parallel_review`` 时两个审查 Agent 并发执行，join 后合并。"""
        agents = [self.reviewer, *self.extra_reviewers]

        payload = {
            "code": state.code,
            "requirements": self.task.requirements_text,
            "test_contract": self.task.contract_text,
            "verification": state.verification,
            "previous_issues": state.issues_open,
            "round": state.round,
        }
        for agent in agents:
            self.bus.publish("orchestrator", agent.name, "review_request", payload, round=state.round)

        def ask(agent: Any) -> Dict[str, Any]:
            msg = agent.receive("review_request")
            if msg is None:
                raise WorkflowError(f"{agent.name} 收件箱为空")
            reply = agent.handle(msg)
            if reply is None:
                raise WorkflowError(f"{agent.name} 未返回结论")
            return self._review_from_reply(reply, agent.name)

        if len(agents) == 1:
            return ask(agents[0])

        # 并行：两个审查 Agent 同时跑，主线程只做 join 与合并
        with ThreadPoolExecutor(max_workers=len(agents), thread_name_prefix="review") as pool:
            reviews = list(pool.map(ask, agents))
        return self._merge_reviews(reviews, [a.name for a in agents])

    @staticmethod
    def _review_from_reply(reply: Message, agent_name: str) -> Dict[str, Any]:
        return {
            "approved": bool(reply.payload.get("approved")),
            "score": int(reply.payload.get("score", 0) or 0),
            "issues": reply.payload.get("issues") or [],
            "summary": reply.payload.get("summary", ""),
            "agents": [agent_name],
            "round": reply.round,
        }

    @staticmethod
    def _merge_reviews(reviews: Dict[str, Any] | list, agent_names: list) -> Dict[str, Any]:
        """并行结论合并：都通过才算通过（取交集），问题取并集，分数取最低。"""
        issues: list = []
        seen = set()
        for r in reviews:
            for issue in r.get("issues") or []:
                key = (issue.get("problem") or "").strip()
                if key and key in seen:
                    continue
                seen.add(key)
                issues.append(issue)
        return {
            "approved": all(r["approved"] for r in reviews),
            "score": min(r["score"] for r in reviews),
            "issues": issues,
            "summary": " | ".join(r["summary"] for r in reviews if r.get("summary")),
            "agents": agent_names,
            "round": max((r.get("round") or 0) for r in reviews),
        }

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _expect_code(self, state: WorkflowState, agent: Any, kind: str, *, author: str) -> str:
        msg = agent.receive(kind)
        if msg is None:
            raise WorkflowError(f"{agent.name} 没收到 {kind} 消息")
        reply = agent.handle(msg)
        if reply is None:
            raise WorkflowError(f"{agent.name} 没有回复")
        code = (reply.payload.get("code") or "").strip()
        if not code:
            raise WorkflowError(f"{agent.name} 返回的代码为空")
        if self.task.class_name and self.task.class_name not in code:
            raise WorkflowError(
                f"{agent.name} 返回的代码里没有 class {self.task.class_name}，疑似输出被截断")
        self._log(f"{author} 交付代码（{len(code)} 字符）")
        return code + "\n"

    def _verify(self, code: str) -> Dict[str, Any]:
        """机器验收。自由输入任务没有测试时，尝试复用已知任务的测试套件。"""
        test_files = self.task.test_files
        # 聊天任务沿用已知套件时，报告里要写清「复用的是哪个任务的测试」
        tests_from = self.task.meta.get("adopted_from") or self.task.task_id
        module_name = self.task.module_name
        class_name = self.task.class_name
        if not test_files:
            inferred = infer_tests(code)
            if inferred:
                try:
                    known = Task.load(inferred)
                    test_files = known.test_files
                    tests_from = inferred
                    # 复用它的模块名/类名：测试里写的是 `from ttl_cache import TTLCache`
                    module_name = known.module_name
                    class_name = known.class_name
                    # 关键：把套件写回任务。否则第一轮挂了之后，修复 Agent 拿不到契约，
                    # 只能对着「构造函数 TypeError」猜（2026-10-25 那次熔断就是这么来的）。
                    self._adopt_suite(known)
                except Exception:  # noqa: BLE001
                    test_files = {}

        result = verify_code(
            code, self.store,
            module_name=module_name,
            class_name=class_name,
            test_files=test_files or None,
            run_tests_too=self.dynamic_verify,
        )
        result["tests_from"] = tests_from if test_files else None
        return result

    def _adopt_suite(self, known: Task) -> None:
        """首版代码命中已知任务：把它的套件与验收项挂到当前任务上（只挂一次）。

        这一步必须在**第一次修复之前**完成：修复 Prompt 里的「验收契约」来自
        ``task.contract_text``，任务没有 tests 时它就是空的。
        """
        if self.task.test_files:
            return
        self.task.meta.update({
            "module_name": known.module_name,
            "class_name": known.class_name,
            "tests": list(known.meta.get("tests", [])),
            "adopted_from": known.task_id,
        })
        try:
            (self.task.root / "requirements.md").write_text(
                known.requirements_text, encoding="utf-8")
        except OSError:
            pass
        self._log(f"首版代码命中已知任务 {known.task_id}，挂载其验收套件与验收项")
        self.dec.record(
            title=f"复用已知任务 {known.task_id} 的验收套件",
            context=("聊天需求没有固定类名，但首版代码定义了 "
                     f"{known.class_name}，命中 tasks/{known.task_id}。"),
            options=["只跑通用静态检查", "复用现成测试套件 + 验收契约"],
            decision=f"挂载 tasks/{known.task_id} 的验收项与测试，并把契约交给修复 Agent",
            rationale="有真测试才算验收；同时避免修复轮拿不到契约只能猜接口。",
            consequences="后续轮次的审查/修复都按该任务的 REQ 与测试判定。",
            phase=Phase.REVIEWING, tags=["contract"],
        )


    def _emit(self, kind: str, **payload: Any) -> None:
        """把结构化事件推给订阅方（CLI 的 --events / 聊天面板）。

        订阅方出错（管道断开、JSON 序列化失败）不应影响工作流本身，因此吞掉异常。
        """
        if not self.event_sink:
            return
        try:
            self.event_sink({"type": kind, **payload})
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _verification_event(v: Dict[str, Any]) -> Dict[str, Any]:
        static = v.get("static") or {}
        dyn = v.get("dynamic") or {}
        return {
            "passed": bool(v.get("passed")),
            "summary": v.get("summary", ""),
            "checks": [{"name": c["name"], "ok": c["ok"], "detail": c["detail"]}
                       for c in static.get("checks", [])],
            "pytest": dyn.get("summary") if dyn.get("ran") else None,
            "duration": dyn.get("duration") if dyn.get("ran") else None,
            "tests_from": v.get("tests_from"),
        }

    def _status(self, state: WorkflowState) -> None:
        self.bus.publish(
            "orchestrator", "*", "status",
            {"phase": state.phase, "round": state.round,
             "label": Phase.LABELS.get(state.phase, state.phase)},
            round=state.round,
        )

    def _record_start(self, state: WorkflowState) -> None:
        self.dec.record(
            title="确定工作流拓扑：编码→审查→修复",
            context=("作业要求 2-3 个 Agent 协作。题目「带过期时间的 LRU 缓存」"
                     "是一个正确性易错、验收可自动化的任务，适合用审查回路收敛。"),
            options=[
                "双 Agent：编码 → 审查（不修，靠重写）",
                "三 Agent：编码 → 审查 → 修复（本方案）",
                "四 Agent：再加一个测试 Agent",
            ],
            decision="采用三 Agent：Coder → Reviewer → Fixer，编排器持有状态机",
            rationale=("审查和修复职责分离，修复 Agent 只对「问题清单」负责，"
                       "避免重写带来的回归；第四个 Agent 的职责（跑测试）能由代码无争议地完成，"
                       "交给 LLM 反而更不稳定。"),
            consequences=("需要定义清楚的消息格式与状态迁移白名单；"
                          "多一轮调用开销，但换取可追溯的修复链路。"),
            phase=Phase.INIT, tags=["topology"],
        )
        self.dec.record(
            title=f"模型接口：{self.llm.provider}",
            context=("用户要求 Agent 的 API 可自行绑定/解除。三个 Agent 共用一套 "
                     "OpenAI 兼容接口，按角色配温度。"),
            options=["直连各家 SDK", "统一 OpenAI 兼容协议 + 配置化 base_url/model", "纯 mock，不接真模型"],
            decision=f"provider={self.llm.provider}，model={self.config.get('model')}，密钥走 AGENT_API_KEY",
            rationale=("兼容层让换模型只改 config/agents.json；mock provider 让无 Key 环境"
                       "也能完整演示编排；两者代码路径完全一致，不存在演示与真实两套逻辑。"),
            consequences="prompt 需要按模型能力调；json_mode 依赖服务端支持，失败时回落到文本解析。",
            phase=Phase.INIT, tags=["llm", "config"],
        )
        self.dec.record(
            title=f"轮次上限设为 {self.max_rounds}",
            context="审查不通过要重试，但重试必须有预算，否则失败任务会无限循环。",
            options=["不设上限", "3 轮（本方案）", "5 轮"],
            decision=f"max_rounds={self.max_rounds}",
            rationale="经验上 2-3 轮足够收敛；超过 3 轮通常说明问题在需求理解层面，继续重试是浪费。",
            consequences="达到上限会停在 REJECTED 并输出遗留问题，需要人工介入。",
            phase=Phase.INIT, tags=["budget"],
        )

    def _fail(self, state: WorkflowState, exc: Exception) -> None:
        state.error = f"{type(exc).__name__}: {exc}"
        try:
            if state.phase not in Phase.TERMINAL:
                state.transition(Phase.FAILED, reason=state.error)
        except Exception:  # noqa: BLE001 - 迁移失败也不能盖住原始错误
            state.phase = Phase.FAILED
        self.store.save(state)
        self.dec.record(
            title="运行异常终止",
            context=f"在处理阶段 {state.phase} 时抛出异常：{state.error}",
            options=["吞掉异常继续", "记录后终止"],
            decision="终止并落盘错误",
            rationale="状态机不允许带着未知错误继续，否则后续阶段基于脏状态运行。",
            consequences="可通过 --resume 从 state.json 续跑。",
            phase=state.phase, tags=["error"],
        )
        self._log(f"✗ 运行失败：{state.error}")

    def _load_message_history(self) -> None:
        path = self.store.messages_path
        if not path.is_file():
            return
        import json
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self.bus.publish_raw(Message.from_dict(json.loads(line)))
            except Exception:  # noqa: BLE001 - 历史日志损坏不应阻断续跑
                continue

    def _finish(self, state: WorkflowState) -> WorkflowState:
        if self.apply_solution and state.phase == Phase.APPROVED:
            self.task.solution_path.parent.mkdir(parents=True, exist_ok=True)
            self.task.solution_path.write_text(state.code, encoding="utf-8")
            self.dec.record(
                title="通过版本落地到 solutions/",
                context=f"代码 v{state.code_version} 通过审查与自动验收。",
                options=["只留在 runs/ 里", "覆盖 solutions/ 作为最终交付"],
                decision=f"写入 {self.task.solution_path.relative_to(PROJECT_ROOT)}",
                rationale="仓库对外展示的是 solutions/，落地后才算交付完成。",
                consequences="历史版本仍保留在 runs/<run_id>/artifacts/ 中可回溯。",
                phase=Phase.APPROVED, tags=["deliver"],
            )
            self._log(f"已写入 {self.task.solution_path}")

        self.bus.publish("orchestrator", "*", "done" if state.phase == Phase.APPROVED else "failed",
                         {"phase": state.phase, "round": state.round}, round=state.round)
        self.store.save(state)

        report = build_report(state, self.bus, self.dec, self.task, llm_calls=self.llm.call_log)
        (self.store.root / "REPORT.md").write_text(report, encoding="utf-8")
        self._log(f"报告：{self.store.root / 'REPORT.md'}")
        self._emit("done", phase=state.phase, round=state.round,
                   max_rounds=state.max_rounds, code_version=state.code_version,
                   code_chars=len(state.code),
                   report=str(self.store.root / "REPORT.md"),
                   run_dir=str(self.store.root), error=state.error,
                   loop_trace=state.loop_trace)
        return state

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"[workflow] {text}", flush=True)
