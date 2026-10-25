# DEC-019 · 验收契约：把测试源码与接口清单下发给 Agent

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-006、DEC-009、DEC-016

## 背景

一次真实运行（`chat-20261025-130024-LRU_python_class_TTLCac-…-079`）用尽 3 轮预算熔断：

- 静态检查 9 项全绿（类、签名、锁、OrderedDict、过期清理都「看起来对」）；
- 动态验收 25 个用例全部 ERROR：`TTLCache.__init__() got an unexpected keyword argument 'clock'`；
- 审查给了 6 条问题，修复两轮后仍然同样报错，第三轮熔断。

把 `messages.jsonl` 和 `state.json` 摊开看，根因不是模型笨，而是**信息不对称**：

| 事实 | 只在哪里出现 |
| --- | --- |
| `TTLCache(capacity=…, ttl_seconds=…, clock=clock)` | `tests/test_ttl_cache.py` |
| `get(key, default)` 的第二个参数 | 同上 |
| `purge_expired()` / `stats()` / `items()` / `clear()` | 同上 |
| 测试直接读 `cache._expiry` 且断言长度上界 | 同上 |

而编码 Agent 只拿到「题面 + 4 条通用 REQ 模板」，修复 Agent 只拿到审查员**转述**的
「构造函数 TypeError」。测试里怎么调用、断言什么，两个 Agent 从头到尾没看见过，
只能猜；猜出来的签名必然对不上，赔掉两次修复预算。

## 决定

### 1. 契约随 Prompt 下发

`Task.contract_text` 生成「验收契约」，随 `task` / `review_request` / `fix_request` 三个
payload 一起发给 Agent，内容 = **验收测试源码原文** + 一段 AST 自动抽取的接口清单：

```
- 构造签名必须接受这些关键字参数：TTLCache(capacity=..., ttl_seconds=..., clock=...)
- 实例上会出现这些方法调用：put, get, items, stats, clear, purge_expired
- 测试会直接读这些内部属性（名字不能改）：_expiry
- 共 22 个用例必须全绿，例如：test_reject_invalid_arguments, …
```

抽取规则保守：只看「由该类构造出来的变量」和「接收该类 fixture 的测试参数」上的调用，
读不出来的不猜——测试源码原文才是最终依据。上限 14000 字符，超了截断并注明。

### 2. 聊天任务在编码前就挂上已知套件

`Task.from_prompt` 里若需求文本点名了已知任务的类名（「… class TTLCache」），
直接沿用该任务的 `module_name` / `class_name` / `requirements` / `tests`，
并记 `adopted_from`（报告与面板显示「复用的测试套件」）。
原先只在代码产出后用 `infer_tests()` 兜底，等于让第一版瞎猜——现在第一版就带着契约。

### 3. 修复必须看到原始 pytest 报错

`fix_request` 增加上一轮 `verification`；`BaseAgent.verification_brief()` 渲染时取
pytest 输出的**头 18 行 + 尾 16 行**。

配套修了 `core/verify.py` 的两个坑：

- 原来只保留输出**尾部 6000 字符**，而 pytest 会把每行尾部截成 `…`，
  真正的异常消息（`unexpected keyword argument 'clock'`）在开头，于是被吃掉；
  改为保留头 3000 + 尾 6000，中间注明省略。
- 仓库根 `pytest.ini` 的 `addopts = -q` 与子进程的 `-q` 叠成 `-qq`，
  连 `25 passed` 汇总行都被吞，报告里只剩一串点；改为传 `-o addopts=` 屏蔽。

## 理由

- **验收跑什么，就应该让被验收的人看到什么**。契约不是提示词技巧，而是把「判分标准」
  从隐含变成显式；否则循环体再严谨，也只在给一个信息不全的 Agent 反复判死刑。
- **修复 Agent 要证据，不要转述**。审查员的一句「签名不符」无法指导修改；
  一行 traceback 可以。
- **纯离线、零额外调用**：契约只是 Prompt 内容，不增加模型调用次数。

## 代价与后果

- Prompt 变长（本例约 +9 KB），输入 token 上升；换来的是「一轮就能改对」，
  总体调用次数与成本反而下降。
- 契约与测试强绑定：改测试就要意识到 Agent 的 Prompt 同时变了（这本来就是期望行为）。
- 无测试的自由输入任务仍然没有契约，静态检查退化为通用检查——这一点没变，
  对应的限制仍在 README §8 与 HANDOVER §8。

## 实测（2026-10-25 13:19–13:21，deepseek-chat）

| 输入 | 结果 |
| --- | --- |
| 原失败题面 | 第 1 轮 `25 passed` → APPROVED（此前 3 轮熔断） |
| 同题面重跑 | 1 轮通过 / 2 轮通过（首版漏了堆压缩，审查指出后修复） |
| 随手一句话（无类名） | 2 轮通过（首版失败 → `_adopt_suite` 补契约 → 修复通过） |
| 题面把参数说成 `ttl` | 1 轮通过（模型按契约实现 `ttl_seconds`） |
| 非缓存任务 | 1 轮通过（无套件，退回通用静态检查） |

连续 6 次全部 APPROVED，其中 2 次是真实的「发现 bug → 定点修复」循环。

## 复核条件

若模型在拿到契约后仍连续两轮修不好同一处，说明问题不在信息而在能力/上下文长度，
那时应改为「结构化接口约定（stub 签名 + 断言清单）」而不是继续堆测试源码。
