"""审查 Agent：逐条对照验收项给结论，输出结构化 JSON。

关键设计：审查 Agent 不只看代码，还看**自动验收的证据**（pytest 结果 + AST 检查）。
LLM 容易「顺着代码说好话」，把机器判定摆在它面前能显著降低误放行。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agents.base import BaseAgent, normalize_review
from core.bus import Message

SYSTEM = """你是一名以严格著称的代码审查员。你的唯一目标是找出会让验收失败的问题。

审查方法：
1. 拿到「验收项」后逐条对照，每条都要给出结论：满足 / 不满足。
2. 拿到「自动验收结果」时，凡是测试失败或静态检查失败的，**必须**判为不通过，
   并把失败原因翻译成具体缺陷。
3. 特别留意这些高频缺陷：
   - get() 命中过期条目时没有顺带删除（惰性删除缺失），过期条目长期占容量；
   - 容量满时直接淘汰 LRU，而没有先清理已过期条目；
   - put() 覆盖已存在 key 时没有刷新 TTL；
   - 加锁粒度不当（把慢操作放进临界区）或压根没加锁；
   - get/put 里为了找过期项而全表扫描或排序，破坏均摊 O(1)。
4. 不要因为「代码风格」判不通过；只关注正确性、复杂度、线程安全。
5. 不要提无法执行的建议。

只输出一个 JSON 对象，不要任何额外文字，格式：
{
  "approved": true/false,
  "score": 0-100 的整数,
  "issues": [
    {
      "id": "R1",
      "severity": "critical|high|medium|low",
      "requirement": "REQ-x（对应哪个验收项，没有就留空）",
      "location": "类/方法/行号",
      "problem": "具体问题，一句话说清",
      "suggestion": "可执行的修改建议"
    }
  ],
  "summary": "一句话结论"
}

判定标准：只有「所有验收项都满足、自动验收通过、无 high 及以上问题」时 approved 才能为 true。"""


class ReviewerAgent(BaseAgent):
    name = "reviewer"
    role = "审查 Agent"
    system_prompt = SYSTEM
    handles = ["review_request"]

    def process(self, msg: Message) -> Optional[Message]:
        code = msg.payload.get("code", "")
        requirements = msg.payload.get("requirements", "")
        verification = msg.payload.get("verification") or {}
        round_no = msg.round

        self.log(f"开始第 {round_no} 轮审查（代码 {len(code)} 字节，自动验收：{verification.get('summary', '无')})")

        user = self.build_user_prompt({
            "验收项": requirements,
            "自动验收结果": self._format_verification(verification),
            "待审代码": f"```python\n{code}\n```",
            "上一轮问题（如已修复请确认）": self._format_prev(msg.payload.get("previous_issues") or []),
        })

        raw = self.llm.chat_json("reviewer", self.system_prompt, user)
        review = normalize_review(raw)

        # 机器证据优先：自动验收没过的，不许放行
        if verification and verification.get("passed") is False and review["approved"]:
            review["approved"] = False
            review["issues"].append({
                "id": "R-AUTO", "severity": "high", "requirement": "",
                "location": "自动验收", "problem": f"自动验收未通过：{verification.get('summary')}",
                "suggestion": "先让测试全绿再提交审查",
            })
            review["summary"] = (review["summary"] + "（自动验收未通过，已强制驳回）").strip()

        self.log(f"结论：{'通过' if review['approved'] else '不通过'}，"
                 f"评分 {review['score']}，问题 {len(review['issues'])} 条")

        return self.reply(msg, "review", {
            "approved": review["approved"],
            "score": review["score"],
            "issues": review["issues"],
            "summary": review["summary"],
            "verification_passed": verification.get("passed"),
            "reviewer": self.name,
        })

    @staticmethod
    def _format_verification(v: Dict[str, Any]) -> str:
        if not v:
            return "（无自动验收结果）"
        lines = [f"- 总体：{'通过' if v.get('passed') else '未通过'}", f"- 摘要：{v.get('summary', '')}"]
        static = v.get("static") or {}
        for c in static.get("checks", []):
            mark = "✅" if c["ok"] else ("❌" if c.get("hard") else "⚠️")
            lines.append(f"- {mark} {c['name']}：{c['detail']}")
        dyn = v.get("dynamic") or {}
        if dyn.get("ran"):
            lines.append(f"- pytest：{dyn.get('summary')}")
            if not dyn.get("passed") and dyn.get("output"):
                tail = "\n".join(dyn["output"].splitlines()[-25:])
                lines.append("```\n" + tail + "\n```")
        return "\n".join(lines)

    @staticmethod
    def _format_prev(issues) -> str:
        if not issues:
            return "（无）"
        return "\n".join(
            f"- {i.get('id')} [{i.get('severity')}] {i.get('problem')}" for i in issues
        )
