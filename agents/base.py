"""三个 Agent 的共同骨架。

每个 Agent 只做三件事：从收件箱取消息 → 组 Prompt 调模型 → 把结果发回去。
它不碰状态机、不碰文件系统，所以可以单独跑、单独测、单独换模型。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.bus import Message, MessageBus
from core.llm_client import LLMClient


class BaseAgent:
    name: str = "agent"
    role: str = "通用 Agent"
    system_prompt: str = ""
    handles: List[str] = []

    def __init__(self, llm: LLMClient, bus: MessageBus, *, verbose: bool = True) -> None:
        self.llm = llm
        self.bus = bus
        self.verbose = verbose
        self.bus.register(self.name)
        self.inbox_count = 0

    # -- 通信 --------------------------------------------------------------- #
    def receive(self, kind: Optional[str] = None) -> Optional[Message]:
        msg = self.bus.consume_one(self.name, kind)
        if msg:
            self.inbox_count += 1
        return msg

    def reply(self, request: Message, kind: str, payload: Dict[str, Any]) -> Message:
        return self.bus.publish(
            sender=self.name, recipient=request.sender, kind=kind,
            payload=payload, round=request.round,
        )

    # -- 处理 --------------------------------------------------------------- #
    def handle(self, msg: Message) -> Optional[Message]:
        if msg.kind not in self.handles:
            self.log(f"忽略未知消息类型 {msg.kind}")
            return None
        return self.process(msg)

    def process(self, msg: Message) -> Optional[Message]:
        raise NotImplementedError

    # -- 工具 --------------------------------------------------------------- #
    def log(self, text: str) -> None:
        if self.verbose:
            print(f"  [{self.name}] {text}", flush=True)

    def build_user_prompt(self, sections: Dict[str, str]) -> str:
        parts = []
        for title, body in sections.items():
            if body:
                parts.append(f"## {title}\n\n{body.strip()}")
        return "\n\n".join(parts)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name}>"


def issues_brief(issues: List[Dict[str, Any]]) -> str:
    """把问题清单压成给修复 Agent 看的紧凑文本。"""
    if not issues:
        return "（无）"
    lines = []
    for i, it in enumerate(issues, 1):
        sev = it.get("severity", "?")
        req = f" [{it.get('requirement')}]" if it.get("requirement") else ""
        lines.append(
            f"{i}. ({sev}{req}) {it.get('problem', '')}\n"
            f"   位置: {it.get('location', '未指明')}\n"
            f"   建议: {it.get('suggestion', '')}"
        )
    return "\n".join(lines)


def verification_brief(v: Dict[str, Any], head_lines: int = 18, tail_lines: int = 16) -> str:
    """把机器验收结果压成给 Agent 看的文本，含 pytest 报错的**开头与结尾**。

    审查 Agent 与修复 Agent 共用：修复最需要的是**原始 traceback**，
    而不是别人转述过的「构造函数 TypeError」。pytest 输出开头是第一个失败用例的
    完整异常（真正的报错消息在这里），结尾是失败清单，中间的重复 traceback 没有增量信息。
    """
    if not v:
        return "（无自动验收结果）"
    lines = [f"- 总体：{'通过' if v.get('passed') else '未通过'}",
             f"- 摘要：{v.get('summary', '')}"]
    static = v.get("static") or {}
    for c in static.get("checks", []):
        mark = "✅" if c["ok"] else ("❌" if c.get("hard") else "⚠️")
        lines.append(f"- {mark} {c['name']}：{c['detail']}")
    dyn = v.get("dynamic") or {}
    if dyn.get("ran"):
        lines.append(f"- pytest：{dyn.get('summary')}")
        if not dyn.get("passed") and dyn.get("output"):
            all_lines = dyn["output"].splitlines()
            if len(all_lines) > head_lines + tail_lines:
                shown = (all_lines[:head_lines] + ["…（中间重复的报错已省略）…"]
                         + all_lines[-tail_lines:])
            else:
                shown = all_lines
            lines.append("```\n" + "\n".join(shown) + "\n```")
    return "\n".join(lines)


def normalize_review(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把模型返回的审查结论文档化，缺字段也不炸。"""
    issues: List[Dict[str, Any]] = []
    for i, it in enumerate(raw.get("issues") or [], 1):
        if isinstance(it, str):
            issues.append({"id": f"R{i}", "severity": "medium", "problem": it,
                           "suggestion": "", "location": "", "requirement": ""})
            continue
        issues.append({
            "id": it.get("id") or f"R{i}",
            "severity": str(it.get("severity", "medium")).lower(),
            "problem": it.get("problem") or it.get("issue") or "",
            "suggestion": it.get("suggestion") or "",
            "location": it.get("location") or it.get("line") or "",
            "requirement": it.get("requirement") or "",
        })
    approved = bool(raw.get("approved"))
    # 有 high/critical 问题却给 approved，一律按不通过处理
    if any(i["severity"] in ("high", "critical", "blocker") for i in issues):
        approved = False
    return {
        "approved": approved,
        "score": int(raw.get("score", 0) or 0),
        "issues": issues,
        "summary": str(raw.get("summary", "")).strip(),
        "raw_keys": sorted(raw.keys()),
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
