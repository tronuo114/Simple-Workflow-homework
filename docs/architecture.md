# 架构说明

> 配套阅读：`DECISIONS.md`（为什么这样设计）、`README.md`（怎么跑）。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ workflow/        编排层：状态机、轮次预算、验收把关、报告生成    │
│   orchestrator.py  task.py  report.py  cli.py                │
├──────────────────────────────────────────────────────────────┤
│ agents/          Agent 层：只做「读消息 → 组 Prompt → 回复」     │
│   base.py  coder.py  reviewer.py  fixer.py                   │
├──────────────────────────────────────────────────────────────┤
│ core/            基础设施：模型调用 / 消息总线 / 状态 / DEC / 验收 │
│   llm_client.py  bus.py  state.py  dec.py  verify.py         │
└──────────────────────────────────────────────────────────────┘
```

依赖方向严格单向：`workflow → agents → core`，`core` 不 import 上层。
所以三个 Agent 可以脱离编排器单独跑（喂一条消息即可），
`core/verify.py` 也能独立被 CLI 的 `verify` 子命令使用。

## 2. 状态机

| 当前阶段 | 可迁移到 | 触发条件 |
| --- | --- | --- |
| `INIT` | `CODING` / `FAILED` | 开始跑 / 初始化异常 |
| `CODING` | `REVIEWING` / `FAILED` | 编码 Agent 交付代码 / 调用失败 |
| `REVIEWING` | `FIXING` / `APPROVED` / `REJECTED` / `FAILED` | 审查不通过且还有预算 / 通过且机器验收过 / 达到轮次上限 / 异常 |
| `FIXING` | `REVIEWING` / `FAILED` | 修复完成 / 调用失败 |
| `APPROVED` / `REJECTED` / `FAILED` | — | 终态 |

非法迁移（如 `REVIEWING → CODING`）会抛 `IllegalTransition`，见 `core/state.py` 的
`Phase.TRANSITIONS`。`--resume` 只会从 `REVIEWING` / `FIXING` / `CODING` 继续，终态直接出报告。

## 3. 消息协议

每条消息：`msg_id / run_id / seq / sender / recipient / kind / round / payload / ts`。

| kind | 发送方 → 接收方 | 关键字段 |
| --- | --- | --- |
| `task` | 编排器 → 编码 | `task_spec`、`requirements`、`target_file` |
| `code` | 编码/修复 → 编排器 | `code`、`notes`、`addressed`、`author` |
| `review_request` | 编排器 → 审查 | `code`、`requirements`、`verification`、`previous_issues` |
| `review` | 审查 → 编排器 | `approved`、`score`、`issues[]`、`summary` |
| `fix_request` | 编排器 → 修复 | `code`、`issues[]`、`requirements` |
| `status` / `done` / `failed` | 编排器 → 全部 | `phase`、`round` |

`issues[]` 的每一项：`{id, severity, requirement, location, problem, suggestion}`。
`severity` 为 `critical/high` 时，`normalize_review` 会强制把 `approved` 改为 `false`——
防止模型「列了 5 个严重问题却判定通过」的自相矛盾。

## 4. 一轮审查发生了什么

```
1. orchestrator 读 state.code
2. core.verify.verify_code(code)
   ├── static_check()  AST：类/方法/锁/扫描/有序结构
   └── run_tests()     干净工作区 + pytest
3. publish review_request（带上 verification 证据）
4. reviewer → chat_json → normalize_review
   └── 若 verification.passed == False 而模型说 approved，强制驳回并追加 R-AUTO
5. orchestrator 决策
   ├── approved and machine_ok      → APPROVED
   ├── approved and not machine_ok  → 记 DEC「强制驳回」→ FIXING
   ├── not approved and 还有预算     → 记 DEC「定点修复」→ FIXING
   └── not approved and 预算耗尽     → 记 DEC「熔断」→ REJECTED
```

## 5. 一次运行的目录

```
runs/<run_id>/
├── state.json          唯一真相源（阶段/轮次/代码/审查结论），原子写入
├── messages.jsonl      全部消息，按 seq 有序
├── decisions.jsonl     运行级 DEC 正文
├── DECISIONS.md        运行级 DEC 索引表（自动重建）
├── REPORT.md           一页式报告：验收/审查/DEC/通信/模型调用/最终代码
├── artifacts/          v1-coder.py、v2-fixer.py、latest.py
└── workspace/          跑 pytest 的干净工作区（每轮重建）
```

`runs/` 在 `.gitignore` 中，不进仓库；代表性运行另存到 `examples/`。

## 6. 怎么扩展

**加一个 Agent（例如安全检查 Agent）**

1. 继承 `BaseAgent`，声明 `name` / `handles` / `system_prompt`，实现 `process()`；
2. 在 `Orchestrator.__init__` 里实例化（自动注册收件箱）；
3. 编排器在合适阶段 `publish(..., kind="security_request")` 并消费回复；
4. 若要参与放行判定，在 `_phase_review` 里把它的结论与 `machine_ok` 做与运算；
5. 加阶段就同步改 `Phase.TRANSITIONS`，否则迁移会被白名单挡住。

**换模型**

改 `config/agents.json`（或 `.env` 的 `AGENT_*`）：`provider` 改 `openai`，
配 `base_url` / `model` / `AGENT_API_KEY`。每个 Agent 可单独设 `model` 与 `temperature`。

**加任务**

在 `tasks/<id>/` 放 `task.md`、`requirements.md`、`meta.json`（模块名、类名、测试文件），
测试放 `tests/`，然后 `python -m workflow.cli run --task <id>`。

## 7. 已知边界

- Agent 输出靠「代码围栏 / JSON 解析」抽取，模型若返回超长截断会触发
  `WorkflowError`（提示「代码里没有 class TTLCache」），而不是静默写入半份代码。
- 机器验收只覆盖已写好的测试；测试没覆盖的行为，LLM 审查也未必能发现。
- 单机单写者：同一 `run_id` 不能被两个进程同时续跑。
- 轮次预算是唯一成本旋钮，没有 token 计费。
