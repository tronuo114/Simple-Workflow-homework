"""运行报告生成：把一次 Workflow 跑出来的东西整理成人能看的 REPORT.md。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from core.bus import MessageBus
from core.dec import DecRecorder
from core.state import Phase, WorkflowState


def build_report(
    state: WorkflowState,
    bus: MessageBus,
    dec: DecRecorder,
    task: Any,
    *,
    llm_calls: List[Dict[str, Any]],
) -> str:
    phase_label = Phase.LABELS.get(state.phase, state.phase)
    verdict = "✅ 通过" if state.phase == Phase.APPROVED else "❌ 未通过"

    lines: List[str] = [
        f"# Workflow 运行报告 · {state.task_id}",
        "",
        f"- **run_id**：`{state.run_id}`",
        f"- **结果**：{verdict}　阶段 `{state.phase}`（{phase_label}）",
        f"- **轮次**：审查 {state.round} / 上限 {state.max_rounds}",
        f"- **代码版本**：v{state.code_version}（{len(state.code)} 字符）",
        f"- **模型**：{state.provider} / {state.model or '-'}",
        f"- **开始 / 结束**：{state.created_at} → {state.updated_at}",
    ]
    if state.error:
        lines.append(f"- **错误**：`{state.error}`")

    lines += ["", "## 一、验收结果", ""]
    v = state.verification or {}
    if v:
        lines.append(f"- 总体：{'通过' if v.get('passed') else '未通过'}")
        lines.append(f"- 摘要：{v.get('summary', '')}")
        static = v.get("static") or {}
        for c in static.get("checks", []):
            mark = "✅" if c["ok"] else ("❌" if c.get("hard") else "⚠️")
            lines.append(f"- {mark} {c['name']}：{c['detail']}")
        dyn = v.get("dynamic") or {}
        if dyn.get("ran"):
            lines.append(f"- pytest：{dyn.get('summary')}（{dyn.get('duration')}s）")
    else:
        lines.append("- （未执行验收）")

    lines += ["", "## 二、审查记录", ""]
    if state.reviews:
        lines += [
            "| 轮次 | 结论 | 评分 | 问题数 | 摘要 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in state.reviews:
            lines.append(
                f"| {r.get('round')} | {'通过' if r.get('approved') else '驳回'} | "
                f"{r.get('score')} | {len(r.get('issues') or [])} | "
                f"{(r.get('summary') or '').replace('|', '/')} |"
            )
    else:
        lines.append("（无）")

    if state.issues_open:
        lines += ["", "### 最后一轮遗留问题", ""]
        for i in state.issues_open:
            lines.append(f"- `{i.get('id')}` [{i.get('severity')}] {i.get('problem')}")

    lines += ["", "## 三、决策记录（DEC）", ""]
    decisions = dec.all()
    if decisions:
        lines += ["| 编号 | 阶段 | 标题 | 决定 |", "| --- | --- | --- | --- |"]
        for d in decisions:
            lines.append(
                f"| {d.dec_id} | {d.phase or '-'} | {d.title} | "
                f"{d.decision.replace('|', '/')} |"
            )
        lines.append("")
        lines.append(f"完整内容见 `{dec.jsonl.name}`；索引见 `DECISIONS.md`。")
    else:
        lines.append("（无）")

    lines += ["", "## 四、Agent 通信流水", ""]
    msgs = bus.history()
    if msgs:
        lines += ["| # | 发送 | 接收 | 类型 | 轮次 | 摘要 |", "| --- | --- | --- | --- | --- | --- |"]
        for m in msgs:
            body = _payload_brief(m.payload)
            lines.append(
                f"| {m.seq} | {m.sender} | {m.recipient} | `{m.kind}` | {m.round} | {body} |"
            )
    else:
        lines.append("（无）")

    lines += ["", "## 五、模型调用", ""]
    if llm_calls:
        lines += ["| Agent | Provider | 字符数 | 耗时(s) | 状态 |", "| --- | --- | --- | --- | --- |"]
        for c in llm_calls:
            lines.append(
                f"| {c.get('agent')} | {c.get('provider')} | {c.get('chars', '-')} | "
                f"{c.get('latency', '-')} | {'OK' if c.get('ok') else 'FAIL'} |"
            )
        total = sum(c.get("chars", 0) or 0 for c in llm_calls)
        lines.append("")
        lines.append(f"合计 {len(llm_calls)} 次调用，约 {total} 字符。")
    else:
        lines.append("（无）")

    lines += ["", "## 六、最终代码", "", "```python", state.code.rstrip(), "```", ""]

    lines += [
        "---",
        f"报告生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"任务：`{task.task_id}` · 需求文件：`tasks/{task.task_id}/task.md`",
    ]
    return "\n".join(lines) + "\n"


def _payload_brief(payload: Dict[str, Any], limit: int = 70) -> str:
    if payload.get("label"):
        return f"{payload.get('label')}（round {payload.get('round', '-')}）"
    for key in ("summary", "notes", "code"):
        if payload.get(key):
            text = str(payload[key]).replace("\n", " ").replace("|", "/")
            return text if len(text) <= limit else text[: limit - 1] + "…"
    if payload.get("issues"):
        return f"{len(payload['issues'])} 条问题"
    return ""
