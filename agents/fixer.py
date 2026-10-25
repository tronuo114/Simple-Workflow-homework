"""修复 Agent：只改被指出的问题，不重写一切。

「重写」是修复环节最大的风险：会把已通过的部分改坏，还会让审查意见和代码对不上。
所以 Prompt 里明确要求保持结构、只做定点修改，并在回复里逐条说明改了什么。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agents.base import BaseAgent, issues_brief
from core.bus import Message

SYSTEM = """你是一名外科手术式的代码修复工程师。你拿到的是「一份代码 + 一份审查问题清单」。

修复纪律：
1. **只改问题清单里指出的地方**，其余逻辑、命名、注释保持原样。
2. 每改一处，都要保证不破坏其他验收项。
3. 修完自检：线程安全是否仍成立、get/put 是否仍是均摊 O(1)、TTL 语义是否正确。
4. 输出完整的 ```python 代码块（整份文件），不要只给 diff、不要省略号。
5. 代码块之后用「已修复：」逐条列出对应的问题编号和改法。"""


class FixerAgent(BaseAgent):
    name = "fixer"
    role = "修复 Agent"
    system_prompt = SYSTEM
    handles = ["fix_request"]

    def process(self, msg: Message) -> Optional[Message]:
        code = msg.payload.get("code", "")
        issues = msg.payload.get("issues") or []
        requirements = msg.payload.get("requirements", "")
        round_no = msg.round

        self.log(f"收到 {len(issues)} 条问题，开始修复（第 {round_no} 轮）")

        user = self.build_user_prompt({
            "必须满足的验收项": requirements,
            "审查问题清单": issues_brief(issues),
            "当前代码": f"```python\n{code}\n```",
        })

        fixed = self.llm.chat_code("fixer", self.system_prompt, user)
        addressed = [i.get("id") for i in issues if i.get("id")]
        self.log(f"修复完成，产出 {len(fixed)} 字符，覆盖问题 {addressed}")

        return self.reply(msg, "code", {
            "code": fixed,
            "author": self.name,
            "notes": f"针对 {len(issues)} 条审查意见修复。",
            "addressed": addressed,
            "target_file": msg.payload.get("target_file", "ttl_cache.py"),
        })
