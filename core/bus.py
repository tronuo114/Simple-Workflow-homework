"""Agent 间通信：带持久化日志的收件箱式消息总线。

为什么不用「互相调用函数」
--------------------------
Agent 之间直接 ``coder.run(); reviewer.run()`` 会把调用顺序、数据格式、重试逻辑
全部焊死在编排器里，加一个 Agent 就要改一遍。这里退成消息传递：

* 每个 Agent 有独立**收件箱**，只认「消息类型 + 载荷」；
* 发送方 ``publish``，收件方 ``consume``，双方都不需要知道对方是谁；
* 所有消息按序落盘到 ``runs/<run_id>/messages.jsonl``，事后可完整复盘。

消息类型（kind）
-----------------
``task``            编排器 → 编码Agent      任务说明
``code``            编码/修复Agent → 编排器 完整代码 + 说明
``review_request``  编排器 → 审查Agent      待审代码 + 验收项 + 自动验收结果
``review``          审查Agent → 编排器      结构化结论
``fix_request``     编排器 → 修复Agent      待修代码 + 问题清单
``status``          编排器 → 全部          阶段变化
``done`` / ``failed``  编排器 → 全部        终态
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class Message:
    msg_id: str
    run_id: str
    seq: int
    sender: str
    recipient: str
    kind: str
    round: int
    payload: Dict[str, Any]
    ts: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id, "run_id": self.run_id, "seq": self.seq,
            "sender": self.sender, "recipient": self.recipient, "kind": self.kind,
            "round": self.round, "payload": self.payload, "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        return cls(
            msg_id=d["msg_id"], run_id=d["run_id"], seq=d["seq"],
            sender=d["sender"], recipient=d["recipient"], kind=d["kind"],
            round=d.get("round", 0), payload=d.get("payload", {}), ts=d.get("ts", _now()),
        )

    def summary(self, limit: int = 90) -> str:
        """一行摘要，给控制台和报告用。"""
        body = json.dumps(self.payload, ensure_ascii=False)
        body = body.replace("\n", " ")
        if len(body) > limit:
            body = body[:limit] + "…"
        return f"#{self.seq:03d} {self.sender} → {self.recipient} [{self.kind}] {body}"


class MessageBus:
    """收件箱式总线。线程安全，落盘为 JSONL。"""

    def __init__(self, run_id: str, log_path: Optional[Path] = None) -> None:
        self.run_id = run_id
        self._lock = threading.RLock()
        self._seq = 0
        self._inboxes: Dict[str, List[Message]] = {}
        self._all: List[Message] = []
        self._log_path = Path(log_path) if log_path else None
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- 注册 --------------------------------------------------------------- #
    def register(self, name: str) -> None:
        with self._lock:
            self._inboxes.setdefault(name, [])

    def names(self) -> List[str]:
        return sorted(self._inboxes)

    # -- 发送 --------------------------------------------------------------- #
    def publish(
        self,
        sender: str,
        recipient: str,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        round: int = 0,
    ) -> Message:
        with self._lock:
            self._seq += 1
            msg = Message(
                msg_id=uuid.uuid4().hex[:12], run_id=self.run_id, seq=self._seq,
                sender=sender, recipient=recipient, kind=kind,
                round=round, payload=payload or {},
            )
            targets = self.names() if recipient == "*" else [recipient]
            for t in targets:
                self._inboxes.setdefault(t, []).append(msg)
            self._all.append(msg)
            if self._log_path:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
            return msg

    def publish_raw(self, msg: Message) -> None:
        """把一条历史消息原样放回总线（续跑时恢复上下文用，不重复落盘）。"""
        with self._lock:
            self._seq = max(self._seq, msg.seq)
            self._all.append(msg)
            for t in (self.names() if msg.recipient == "*" else [msg.recipient]):
                self._inboxes.setdefault(t, []).append(msg)

    # -- 接收 --------------------------------------------------------------- #
    def consume(self, name: str) -> List[Message]:
        """取走该收件箱里的全部消息（FIFO）。"""
        with self._lock:
            msgs = self._inboxes.get(name, [])
            self._inboxes[name] = []
            return list(msgs)

    def consume_one(self, name: str, kind: Optional[str] = None) -> Optional[Message]:
        with self._lock:
            box = self._inboxes.get(name, [])
            for i, m in enumerate(box):
                if kind is None or m.kind == kind:
                    return box.pop(i)
            return None

    def pending(self, name: str) -> int:
        with self._lock:
            return len(self._inboxes.get(name, []))

    # -- 观察 --------------------------------------------------------------- #
    def history(self, kind: Optional[str] = None, sender: Optional[str] = None) -> List[Message]:
        with self._lock:
            msgs = list(self._all)
        if kind:
            msgs = [m for m in msgs if m.kind == kind]
        if sender:
            msgs = [m for m in msgs if m.sender == sender]
        return msgs

    def last(self, kind: str) -> Optional[Message]:
        msgs = self.history(kind=kind)
        return msgs[-1] if msgs else None

    def transcript(self) -> str:
        return "\n".join(m.summary() for m in self.history())

    def __len__(self) -> int:
        return len(self._all)
