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
