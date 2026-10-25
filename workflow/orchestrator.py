"""Workflow 编排器：状态机 + 消息驱动 + 验收把关。

拓扑（代码审查流程）
--------------------
::

    ┌──────────────────────────── orchestrator（编排器）───────────────────────────┐
    │                                                                            │
    │  INIT ──task──▶ coder ──code──▶ 自动验收 ──review_request──▶ reviewer       │
    │                                        │                        │          │
    │                                        │                   review │         │
    │                                        ▼                        ▼          │
    │                              通过 ─▶ APPROVED         不通过 ─▶ FIXING      │
    │                                                              │             │
    │                                                       fix_request          │
    │                                                              ▼             │
    │                                                           fixer ──▶ 回审查  │
    └────────────────────────────────────────────────────────────────────────────┘

三条不可让步的规则：

1. **机器验收优先于模型判断**：LLM 说通过、测试是红的，按不通过处理。
2. **轮次上限熔断**：达到 ``max_rounds`` 仍未通过就停下，输出遗留问题，而不是无限烧钱。
3. **每次决策都留 DEC**：进入修复、强制驳回、熔断、产物落地，全部带编号落盘。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from agents.coder import CoderAgent
from agents.fixer import FixerAgent
from agents.reviewer import ReviewerAgent
from core.bus import Message, MessageBus
from core.dec import DecRecorder
from core.llm_client import LLMClient, load_config
from core.state import Phase, StateStore, WorkflowState
from core.verify import verify_code
from workflow.report import build_report
from workflow.task import PROJECT_ROOT, Task

RUNS_ROOT = PROJECT_ROOT / "runs"


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
    ) -> None:
        self.task = task
        self.runs_root = Path(runs_root)
        self.run_id = run_id or self._new_run_id(task.task_id)
        self.verbose = verbose
        self.dynamic_verify = dynamic_verify
        self.apply_solution = apply_solution

        self.config = load_config(config_path, overrides)
        if max_rounds:
            self.config.setdefault("limits", {})["max_rounds"] = max_rounds
        self.max_rounds = int(self.config.get("limits", {}).get("max_rounds", 3))

        self.llm = LLMClient(self.config, verbose=verbose)
        self.store = StateStore(self.runs_root, self.run_id)
        self.bus = MessageBus(self.run_id, self.store.messages_path)
        self.dec = DecRecorder(f"run:{self.run_id}", self.store.root,
                               title=f"运行决策 · {self.run_id}")
        self.coder = CoderAgent(self.llm, self.bus, verbose=verbose)
        self.reviewer = ReviewerAgent(self.llm, self.bus, verbose=verbose)
        self.fixer = FixerAgent(self.llm, self.bus, verbose=verbose)

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_run_id(task_id: str) -> str:
        return f"{task_id}-{datetime.now():%Y%m%d-%H%M%S}-{datetime.now().microsecond // 1000:03d}"

    def run(self, resume: bool = False) -> WorkflowState:
        """跑完整条流水线。``resume=True`` 时从 state.json 接着跑。"""
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
            if state.phase in Phase.TERMINAL:
                self._finish(state)
                return state
        else:
            state = WorkflowState(
                run_id=self.run_id, task_id=self.task.task_id,
                max_rounds=self.max_rounds,
                provider=self.llm.provider, model=str(self.config.get("model", "")),
            )
            self.store.save(state)
            self._record_start(state)
            try:
                self._phase_coding(state)
            except Exception as exc:  # noqa: BLE001 - 任何异常都要留下终态
                self._fail(state, exc)
                return self._finish(state)

        try:
            if state.phase == Phase.FIXING:
                self._phase_fix(state)
            while state.phase == Phase.REVIEWING:
                self._phase_review(state)
                if state.phase == Phase.FIXING:
                    self._phase_fix(state)
        except Exception as exc:  # noqa: BLE001
            self._fail(state, exc)

        return self._finish(state)

    # ------------------------------------------------------------------ #
    # 阶段
    # ------------------------------------------------------------------ #
    def _phase_coding(self, state: WorkflowState) -> None:
        state.transition(Phase.CODING, reason="派发任务给编码 Agent")
        self.store.save(state)
        self._status(state)

        self.bus.publish(
            "orchestrator", self.coder.name, "task",
            {
                "task_spec": self.task.spec_text,
                "requirements": self.task.requirements_text,
                "target_file": self.task.target_file,
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

    def _phase_review(self, state: WorkflowState) -> None:
        state.round += 1
        self.store.save(state)

        # 1) 先跑机器验收，把证据摆到审查 Agent 面前
        state.verification = self._verify(state.code)
        self._log(f"自动验收：{state.verification['summary']}")

        # 2) 请审查 Agent 出结论
        self._status(state)
        self.bus.publish(
            "orchestrator", self.reviewer.name, "review_request",
            {
                "code": state.code,
                "requirements": self.task.requirements_text,
                "verification": state.verification,
                "previous_issues": state.issues_open,
                "round": state.round,
            },
            round=state.round,
        )
        msg = self.reviewer.receive("review_request")
        if msg is None:
            raise WorkflowError("审查 Agent 收件箱为空")
        reply = self.reviewer.handle(msg)
        if reply is None:
            raise WorkflowError("审查 Agent 未返回结论")

        review = {
            "approved": bool(reply.payload.get("approved")),
            "score": int(reply.payload.get("score", 0) or 0),
            "issues": reply.payload.get("issues") or [],
            "summary": reply.payload.get("summary", ""),
            "round": state.round,
        }
        state.review = review
        state.reviews.append(review)
        state.issues_open = list(review["issues"])
        self.store.save(state)

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
            self._log("✅ 审查通过")
            return

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
            self._log("❌ 未通过（已达轮次上限）")
            return

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
        self._log(f"↩ 审查未通过（{len(state.issues_open)} 条问题），进入修复")

    def _phase_fix(self, state: WorkflowState) -> None:
        self.bus.publish(
            "orchestrator", self.fixer.name, "fix_request",
            {
                "code": state.code,
                "issues": state.issues_open,
                "requirements": self.task.requirements_text,
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
        self._log(f"修复完成，代码 v{state.code_version}，回到审查")

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
        if self.task.class_name not in code:
            raise WorkflowError(
                f"{agent.name} 返回的代码里没有 class {self.task.class_name}，疑似输出被截断")
        self._log(f"{author} 交付代码（{len(code)} 字符）")
        return code + "\n"

    def _verify(self, code: str) -> Dict[str, Any]:
        return verify_code(
            code, self.store,
            module_name=self.task.module_name,
            class_name=self.task.class_name,
            test_files=self.task.test_files or None,
            run_tests_too=self.dynamic_verify,
        )

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
        return state

    def _log(self, text: str) -> None:
        if self.verbose:
            print(f"[workflow] {text}", flush=True)
