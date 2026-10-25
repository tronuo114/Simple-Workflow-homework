"""审查 Agent：逐条对照验收项给结论，输出结构化 JSON。

关键设计：审查 Agent 不只看代码，还看**自动验收的证据**（pytest 结果 + AST 检查）。
LLM 容易「顺着代码说好话」，把机器判定摆在它面前能显著降低误放行。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agents.base import BaseAgent, normalize_review, verification_brief
from core.bus import Message

SYSTEM = """你是一名以严格著称的代码审查员。你的唯一目标是找出会让验收失败的问题。

审查方法：
1. 拿到「验收项」后逐条对照，每条都要给出结论：满足 / 不满足。
2. 拿到「自动验收结果」时，凡是测试失败或静态检查失败的，**必须**判为不通过，
   并把失败原因翻译成具体缺陷。
3. 通用检查（任何任务都要看）：
   - 有没有未实现的占位（pass / ... / NotImplementedError）；
   - 边界情况（空输入、极端值、异常路径）有没有处理，是否吞异常；
   - 复杂度是否合理，有没有明显的全表扫描/重复计算；
   - 是否引入了未声明的第三方依赖。
4. 如果任务是缓存/并发类，额外重点看这些高频缺陷：
   - get() 命中过期条目时没有顺带删除（惰性删除缺失），过期条目长期占容量；
   - 容量满时直接淘汰 LRU，而没有先清理已过期条目；
   - put() 覆盖已存在 key 时没有刷新 TTL；
   - 加锁粒度不当（把慢操作放进临界区）或压根没加锁；
   - get/put 里为了找过期项而全表扫描或排序，破坏均摊 O(1)。
5. 不要因为「代码风格」判不通过；只关注正确性、复杂度、线程安全。
6. 不要提无法执行的建议。

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
            "验收测试（机器跑的就是它）": msg.payload.get("test_contract", ""),
            "自动验收结果": verification_brief(verification),
            "待审代码": f"```python\n{code}\n```",
            "上一轮问题（如已修复请确认）": self._format_prev(msg.payload.get("previous_issues") or []),
        })

        raw = self.llm.chat_json(self.name, self.system_prompt, user)
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
        return verification_brief(v)

    @staticmethod
    def _format_prev(issues) -> str:
        if not issues:
            return "（无）"
        return "\n".join(
            f"- {i.get('id')} [{i.get('severity')}] {i.get('problem')}" for i in issues
        )


SECURITY_SYSTEM = """你是一名安全与健壮性审查员。你不看风格，只找「会在真实环境里出事」的问题。

逐条检查（每条都要给出结论）：
1. 并发安全：共享状态是否被保护；锁的粒度是否会导致死锁或长时间阻塞。
2. 资源与内存：是否有无界增长（缓存不淘汰、列表只增不减）、句柄/连接泄漏。
3. 异常处理：是否吞掉异常（except: pass）、是否把错误静默降级成错误结果。
4. 输入校验：外部输入是否可能触发崩溃、越界、注入或极端耗时。
5. 依赖与副作用：是否引入未声明依赖、是否有全局可变状态或隐藏的时间依赖。

评分与结论规则：存在 critical/high 问题时 approved 必须为 false。
输出格式与主审查员完全一致：只输出一个 JSON 对象：
{
  "approved": true/false,
  "score": 0-100,
  "issues": [{"id": "S1", "severity": "critical|high|medium|low",
              "requirement": "", "location": "类/方法", "problem": "...", "suggestion": "..."}],
  "summary": "一句话结论"
}"""


class SecurityReviewerAgent(ReviewerAgent):
    """并行审查里的第二个审查员：只盯安全与健壮性，和主审查员同时跑。"""

    name = "security_reviewer"
    role = "安全与健壮性审查 Agent"
    system_prompt = SECURITY_SYSTEM
