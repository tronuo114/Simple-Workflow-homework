"""DEC（Decision）记录器：把每一次「选择」钉在纸上。

作业要求「每次决策都要留下记录 DEC 编号」。所以决策分两层：

* **项目级 DEC**：``docs/decisions/DEC-001.md`` …，记录架构/技术选型这类长期有效的决定，
  编号全局唯一、只追加不修改。
* **运行级 DEC**：``runs/<run_id>/decisions.jsonl`` + ``DECISIONS.md``，
  记录这一次运行里的每个动作：为什么进入修复、为什么放行、为什么熔断。
  编号 ``DEC-001`` 起，同一次运行内自增。

每条决策必须有：背景（context）、备选项（options）、选择（decision）、
理由（rationale）、代价（consequences）。没有这五项的不叫决策，叫流水账。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Decision:
    dec_id: str
    title: str
    context: str
    options: List[str]
    decision: str
    rationale: str
    consequences: str
    scope: str = "run"
    phase: str = ""
    actor: str = "orchestrator"
    tags: List[str] = field(default_factory=list)
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        opts = "\n".join(f"- {o}" for o in self.options) or "- （无）"
        return (
            f"### {self.dec_id} · {self.title}\n\n"
            f"- **时间**：{self.ts}\n"
            f"- **范围/主体**：{self.scope} / {self.actor}"
            + (f"（阶段 {self.phase}）" if self.phase else "") + "\n"
            + (f"- **标签**：{', '.join(self.tags)}\n" if self.tags else "")
            + f"\n**背景**\n\n{self.context}\n\n"
            f"**可选项**\n\n{opts}\n\n"
            f"**决定**\n\n{self.decision}\n\n"
            f"**理由**\n\n{self.rationale}\n\n"
            f"**代价 / 后果**\n\n{self.consequences}\n"
        )


class DecRecorder:
    """按作用域管理 DEC 编号并落盘。"""

    def __init__(self, scope: str, root: Path, *, title: str = "") -> None:
        self.scope = scope
        self.root = Path(root)
        self.title = title or scope
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.root / "decisions.jsonl"
        self.index = self.root / "DECISIONS.md"
        self._count = self._scan_count()

    # -- 编号 --------------------------------------------------------------- #
    def _scan_count(self) -> int:
        """从已有文件里恢复最大编号，保证续跑不会重号。"""
        max_n = 0
        if self.jsonl.is_file():
            for line in self.jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    dec_id = json.loads(line).get("dec_id", "")
                except json.JSONDecodeError:
                    continue
                m = re.search(r"DEC-(\d+)", dec_id or "")
                if m:
                    max_n = max(max_n, int(m.group(1)))
        for p in self.root.glob("DEC-*.md"):
            m = re.search(r"DEC-(\d+)", p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return max_n

    def next_id(self) -> str:
        self._count += 1
        return f"DEC-{self._count:03d}"

    # -- 记录 --------------------------------------------------------------- #
    def record(
        self,
        title: str,
        context: str,
        decision: str,
        rationale: str,
        *,
        options: Optional[List[str]] = None,
        consequences: str = "（未评估）",
        phase: str = "",
        actor: str = "orchestrator",
        tags: Optional[List[str]] = None,
    ) -> Decision:
        dec = Decision(
            dec_id=self.next_id(), title=title, context=context,
            options=options or [], decision=decision, rationale=rationale,
            consequences=consequences, scope=self.scope, phase=phase,
            actor=actor, tags=tags or [],
        )
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dec.to_dict(), ensure_ascii=False) + "\n")
        self._render_index(dec)
        return dec

    def _render_index(self, latest: Decision) -> None:
        """重建索引表（幂等，避免并发追加时表头重复）。"""
        rows: List[str] = []
        if self.jsonl.is_file():
            for line in self.jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(d)

        head = (
            f"# DECISIONS · {self.title}\n\n"
            "> 由 `core/dec.py` 自动生成，按编号只追加。每行一条决策，详情见 "
            f"`{self.jsonl.name}`。\n\n"
            "| 编号 | 阶段 | 标题 | 决定 | 理由摘要 | 时间 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
        )
        body = []
        for d in rows:
            body.append(
                "| {id} | {phase} | {title} | {decision} | {rationale} | {ts} |".format(
                    id=d.get("dec_id", ""),
                    phase=d.get("phase", "") or "-",
                    title=_cell(d.get("title", "")),
                    decision=_cell(d.get("decision", "")),
                    rationale=_cell(d.get("rationale", ""), 80),
                    ts=(d.get("ts", "") or "").replace("T", " "),
                )
            )
        self.index.write_text(head + "\n".join(body) + "\n", encoding="utf-8")

    # -- 查询 --------------------------------------------------------------- #
    def all(self) -> List[Decision]:
        out: List[Decision] = []
        if not self.jsonl.is_file():
            return out
        for line in self.jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(Decision(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return out

    def markdown(self) -> str:
        return "\n".join(d.to_markdown() for d in self.all())


def _cell(text: str, limit: int = 60) -> str:
    text = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
