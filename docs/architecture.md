# 架构说明

> 配套阅读：`DECISIONS.md`（为什么这样设计）、`README.md`（怎么跑）。

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ launcher/        桌面入口（可选）：GUI / --cli / --pytest      │
│   app.py  panel.py                                          │
├──────────────────────────────────────────────────────────────┤
│ workflow/        编排层：循环体、状态机、轮次预算、验收把关、报告生成 │
│   orchestrator.py  task.py  report.py  cli.py                │
├──────────────────────────────────────────────────────────────┤
│ agents/          Agent 层：只做「读消息 → 组 Prompt → 回复」     │
│   base.py  coder.py  reviewer.py  fixer.py  registry.py      │
├──────────────────────────────────────────────────────────────┤
│ core/            基础设施：模型 / 消息总线 / 状态 / DEC / 验收    │
│   llm_client.py  bus.py  state.py  dec.py  verify.py  paths.py │
└──────────────────────────────────────────────────────────────┘
```

依赖方向严格单向：`launcher → workflow → agents → core`，`core` 不 import 上层。
所以三个 Agent 可以脱离编排器单独跑（喂一条消息即可），
`core/verify.py` 也能独立被 CLI 的 `verify` 子命令使用。

`core/paths.app_root()` 是唯一决定「仓库根在哪」的地方：源码运行取上两级目录，
打包运行按 `WORKFLOW_HOME` → exe 目录/上一级（含 `tasks/`）→ 解包目录依次尝试。
桌面面板的打包约定见 [DEC-015](decisions/DEC-015-desktop-launcher.md)。

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
| `task` | 编排器 → 编码 | `task_spec`、`requirements`、`test_contract`、`target_file` |
| `code` | 编码/修复 → 编排器 | `code`、`notes`、`addressed`、`author` |
| `review_request` | 编排器 → 审查 | `code`、`requirements`、`test_contract`、`verification`、`previous_issues` |
| `review` | 审查 → 编排器 | `approved`、`score`、`issues[]`、`summary` |
| `fix_request` | 编排器 → 修复 | `code`、`issues[]`、`requirements`、`test_contract`、`verification` |
| `status` / `done` / `failed` | 编排器 → 全部 | `phase`、`round` |

`issues[]` 的每一项：`{id, severity, requirement, location, problem, suggestion}`。
`severity` 为 `critical/high` 时，`normalize_review` 会强制把 `approved` 改为 `false`——
防止模型「列了 5 个严重问题却判定通过」的自相矛盾。

## 4. 循环体：直到代码审核通过

整个流程的主结构是 `Orchestrator._loop_until_approved()` 里的一个 `while`：

```
        ┌──────────────────────────────────────────────────────────┐
        │  while state.phase == REVIEWING:                         │
        │      1. verify：AST 静态检查 + 干净工作区跑 pytest        │
        │      2. publish review_request → reviewer → review 结论   │
        │      3. 分支判断（_phase_review 返回 verdict）            │
        │         ├─ approved 且机器通过  → APPROVED    → break    │
        │         ├─ 不通过且预算耗尽      → REJECTED    → break    │
        │         └─ 不通过且还有预算      → FIXING → fixer → 回到 ①│
        └──────────────────────────────────────────────────────────┘
```

三个出口都是显式的，写在 `_phase_review` 的返回值（`VERDICT_APPROVED` /
`VERDICT_REJECTED` / `VERDICT_RETRY`）里；循环体只做「继续 or 离开」，
不判断业务细节。异常由 `run()` 兜住转 `FAILED`，此时状态已落盘，可 `--resume`。

每执行一次「审查 + 分支」，就往 `state.loop_trace` 追加一条记录
（`iteration / round / approved / machine_ok / score / issues / verdict / action`），
报告里的「循环轨迹」表直接由它渲染。设计理由见
[DEC-014](decisions/DEC-014-review-loop.md)。

一轮循环内部的时序：

```
1. orchestrator 读 state.code
2. core.verify.verify_code(code)
   ├── static_check()  AST：类/方法/锁/扫描/有序结构
   └── run_tests()     干净工作区 + pytest
3. publish review_request（带上 verification 证据）
4. reviewer → chat_json → normalize_review
   └── 若 verification.passed == False 而模型说 approved，强制驳回并追加 R-AUTO
5. orchestrator 决策（即循环的分支）
   ├── approved and machine_ok      → APPROVED（离开循环）
   ├── approved and not machine_ok  → 记 DEC「强制驳回」→ FIXING
   ├── not approved and 还有预算     → 记 DEC「定点修复」→ FIXING
   └── not approved and 预算耗尽     → 记 DEC「熔断」→ REJECTED（离开循环）
```

## 5. 一次运行的目录

```
runs/<run_id>/
├── state.json          唯一真相源（阶段/轮次/代码/审查结论/循环轨迹），原子写入
├── messages.jsonl      全部消息，按 seq 有序
├── decisions.jsonl     运行级 DEC 正文
├── DECISIONS.md        运行级 DEC 索引表（自动重建）
├── REPORT.md           一页式报告：验收/审查/DEC/通信/模型调用/最终代码
├── artifacts/          v1-coder.py、v2-fixer.py、latest.py
└── workspace/          跑 pytest 的干净工作区（每轮重建）
```

`runs/` 在 `.gitignore` 中，不进仓库；代表性运行另存到 `examples/`。

## 6. 怎么扩展

**加一个 Agent（不改编排器）**

`agents/registry.py` 把「有哪些 Agent」变成数据：`register_agent(name, factory)` 登记，
`build_pipeline(config, ...)` 按 `config["pipeline"]` 组装。编排器只认
`pipeline["coder" / "reviewer" / "fixer" / "extra_reviewers"]`，不再 import 具体 Agent 类。

1. 继承 `BaseAgent`，声明 `name` / `handles` / `system_prompt`，实现 `process()`；
2. 模块导入时 `register_agent("my_reviewer", lambda llm, bus, verbose: MyReviewer(llm, bus, verbose=verbose))`；
3. 把名字加进 `config/agents.json` 的 `pipeline.reviewers`（第一个是主审查，其余在
   `--parallel-review` 时并发）。

示例见 `tests/test_extensibility.py`（10 行的自定义审查 Agent，注册后立即参与循环），
取舍见 DEC-017。若要参与循环分支判断，结论会在 `_run_reviewers` 里被合并，循环体不用改。

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
