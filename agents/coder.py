"""编码 Agent：按任务说明产出第一版可运行代码。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agents.base import BaseAgent
from core.bus import Message

SYSTEM = """你是一名资深 Python 后端工程师。任务：按需求实现一个类，要求能直接跑、能过测试。

硬性写作规范：
1. 只输出一个 ```python 代码块，代码块内是**完整可运行**的模块，不要省略号、不要 TODO。
2. 不要写 if __name__ == "__main__" 之外的示例代码；不要引入第三方依赖。
3. 类型注解齐全，公开方法写 docstring，复杂不变量用注释说明。
4. 线程安全用什么原语、复杂度怎么保证，要在注释里写清楚。
5. **如果给了「验收测试」，它就是唯一标准**：构造函数的参数名（含关键字参数）、
   方法名、`get` 的默认值参数、被测试直接读取的内部属性（如 `_expiry`）都必须与测试一致。
   写之前先把测试里对该类的每一次调用列出来，逐条对齐签名，不要凭题面猜接口。

你写完后会有另一个 Agent 逐条审查，请一次写到位。代码块之后可以用「说明：」写 3-6 行设计取舍。"""


class CoderAgent(BaseAgent):
    name = "coder"
    role = "编码 Agent"
    system_prompt = SYSTEM
    handles = ["task"]

    def process(self, msg: Message) -> Optional[Message]:
        task = msg.payload.get("task_spec", "")
        requirements = msg.payload.get("requirements", "")
        target = msg.payload.get("target_file", "ttl_cache.py")
        class_name = (msg.payload.get("class_name") or "").strip()

        self.log(f"收到任务（第 {msg.round} 轮），开始编写 {target}")

        delivery = f"单文件模块 {target}"
        delivery += f"，顶层定义类 {class_name}。" if class_name else "，顶层定义接口清晰的类或函数。"

        user = self.build_user_prompt({
            "任务说明": task,
            "必须满足的验收项": requirements,
            "交付形式": delivery,
            "验收测试（必须全部跑绿，接口以它为准）": msg.payload.get("test_contract", ""),
            "上一轮遗留": msg.payload.get("carry_over", "") or "（首轮，无）",
        })

        code = self.llm.chat_code("coder", self.system_prompt, user)
        self.log(f"产出 {len(code)} 字符代码")

        return self.reply(msg, "code", {
            "code": code,
            "author": self.name,
            "notes": "首版实现：按验收项逐条设计。",
            "addressed": [],
            "target_file": target,
        })
