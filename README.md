# Simple Workflow Homework · 三 Agent 代码审查工作流

一个**能真跑**的 2-3 Agent 协作 Workflow：编码 → 审查 → 修复，带状态机、消息总线、
自动验收和全链路决策记录（DEC）。最终用它完成了验收题「带过期时间的 LRU 缓存」。

[![tests](https://img.shields.io/badge/tests-25%20passed-brightgreen)](#五最终验收带过期时间的-lru-缓存)
[![workflow](https://img.shields.io/badge/workflow-APPROVED%202%2F3%20rounds-blue)](#六跑一遍)
[![decisions](https://img.shields.io/badge/decisions-12%20project%20%2B%20per--run-orange)](DECISIONS.md)

---

## 目录

- [一、需求分析](#一需求分析)
- [二、项目目标](#二项目目标)
- [三、技术方案](#三技术方案)
- [四、三个 Agent](#四三个-agent)
- [五、最终验收：带过期时间的 LRU 缓存](#五最终验收带过期时间的-lru-缓存)
- [六、跑一遍](#六跑一遍)
- [七、决策记录（DEC）](#七决策记录dec)
- [八、固定工具：GitHub 上传](#八固定工具github-上传)
- [九、目录结构](#九目录结构)
- [十、已知限制](#十已知限制)

---

## 一、需求分析

### 1.1 作业要什么

题目不是「写个脚本串两个函数」，而是要**把三个概念落成可运行的骨架**：

| 概念 | 真正要回答的问题 | 本项目的位置 |
| --- | --- | --- |
| Workflow 编排 | 谁决定下一步？失败/通过怎么分流？能不能停下来？ | `workflow/orchestrator.py`：显式状态机 + 轮次预算 |
| Agent 间通信 | Agent 怎么传数据？能不能单独替换？出错怎么复盘？ | `core/bus.py`：收件箱式消息总线 + JSONL 落盘 |
| 状态管理 | 进程崩了怎么办？续跑会不会重号/重跑？ | `core/state.py`：单一状态源 + 原子落盘 + 迁移白名单 |

### 1.2 为什么选「代码审查流程」

题目给了几个流程模板（如代码生成、代码审查、内容审核）。选**代码审查**的理由是它
**自带客观判据**：写得对不对，测试跑一遍就知道。这让「工作流是否真的有效」可被验证，
而不是靠 Agent 自己说「我做完了」——这也直接引出下面这个最关键的判断。

### 1.3 关键难点（也就是设计要点）

1. **LLM 会误放行**。这是工作流最危险的失效模式：审查 Agent 说「看起来不错」，
   测试却是红的。→ 必须引入**机器验收卡口**，且冲突时以机器为准（[DEC-006](docs/decisions/DEC-006-machine-verdict.md)）。
2. **修复会引入回归**。让编码 Agent 重写整份代码，会把已通过的部分改坏。→
   让**修复 Agent 只对问题清单负责**（[DEC-002](docs/decisions/DEC-002-topology.md)）。
3. **回路可能不收敛**。审查不过 → 修 → 又不过，无限循环烧钱。→ **3 轮上限 + 熔断**，
   超限输出遗留问题而不是继续重试（[DEC-011](docs/decisions/DEC-011-round-budget.md)）。
4. **状态必须能复盘**。否则「第几轮因为什么被打回」只能靠猜。→
   状态原子落盘 + 消息流水 + 运行级 DEC 自动编号（[DEC-004](docs/decisions/DEC-004-state.md)、[DEC-012](docs/decisions/DEC-012-traceability.md)）。
5. **模型必须可换**。需求要求「API 可自行绑定解除」。→ OpenAI 兼容层 + 配置化 + 离线 mock
   （[DEC-005](docs/decisions/DEC-005-llm.md)）。

### 1.4 非目标（明确不做）

- 不做通用编排平台（不支持 DAG、并行分支、人工审核节点）；
- 不做多轮对话式 Agent（每个 Agent 单次调用，无记忆）；
- 不做 token 计费与限流（轮次是唯一预算旋钮）；
- 不追求模型能力评测（mock 只证明编排正确）。

---

## 二、项目目标

可验收的目标，逐条对应实现与证据：

| # | 目标 | 验收方式 | 证据 |
| --- | --- | --- | --- |
| G1 | 三个独立 Agent，可单独替换、单独跑 | 每个 Agent 只依赖消息，不 import 编排器 | `agents/` 四个文件 |
| G2 | 闭环工作流：编码 → 审查 → 修复 → 复审 | 一次运行内完成至少一次打回与修复 | `examples/sample-run/REPORT.md`（2 轮通过） |
| G3 | 显式状态管理，支持断点续跑 | `--resume` 从 `state.json` 恢复 | `core/state.py`、`state.json` |
| G4 | Agent 通信可追溯 | 所有消息落 `messages.jsonl`，报告里成表 | `runs/<id>/messages.jsonl` |
| G5 | 代码质量不靠「模型自觉」 | AST 静态检查 + pytest 双层卡口，冲突以机器为准 | `core/verify.py` |
| G6 | 每次决策都有 DEC 编号 | 项目级 12 条 + 运行级自动编号 | [DECISIONS.md](DECISIONS.md) |
| G7 | 模型接口可自由绑定/解除 | 改配置即可换 provider/模型，无 Key 也能跑 | `config/agents.json`、`--mock` |
| G8 | 通过最终验收 | `python -m workflow.cli verify` 全绿 | 25 passed |

---

## 三、技术方案

### 3.1 总体架构

```
┌────────────────────────────────────────────────────────────────┐
│ workflow/  编排层                                                │
│   orchestrator.py   状态机 + 轮次预算 + 验收把关 + DEC           │
│   task.py           任务定义（task.md / requirements.md / meta）  │
│   report.py         REPORT.md 生成                               │
│   cli.py            run / verify / tasks / runs                  │
├────────────────────────────────────────────────────────────────┤
│ agents/    Agent 层（无状态，只认消息）                            │
│   coder.py 编码    reviewer.py 审查    fixer.py 修复             │
├────────────────────────────────────────────────────────────────┤
│ core/      基础设施                                              │
│   llm_client.py  OpenAI 兼容 + mock      bus.py   消息总线        │
│   state.py       状态机 + 原子落盘        dec.py   DEC 记录器      │
│   verify.py      AST 检查 + pytest 执行                           │
└────────────────────────────────────────────────────────────────┘
```

依赖方向单向：`workflow → agents → core`。所以 Agent 可以脱离编排器单独喂消息测试。

### 3.2 状态机

```
INIT ──▶ CODING ──▶ REVIEWING ──┬─▶ APPROVED        （审查过 + 机器验收过）
                                ├─▶ FIXING ──▶ REVIEWING（审查不过，还有预算）
                                └─▶ REJECTED        （审查不过，预算耗尽）
任意阶段 ──异常──▶ FAILED
```

迁移走白名单（`Phase.TRANSITIONS`），`REVIEWING → CODING` 这类跳转直接抛
`IllegalTransition`。设计理由见 [DEC-004](docs/decisions/DEC-004-state.md)。

### 3.3 消息协议

| kind | 方向 | 关键载荷 |
| --- | --- | --- |
| `task` | 编排器 → 编码 | `task_spec`、`requirements`、`target_file` |
| `code` | 编码/修复 → 编排器 | `code`、`notes`、`addressed` |
| `review_request` | 编排器 → 审查 | `code`、`requirements`、**`verification`（机器验收证据）** |
| `review` | 审查 → 编排器 | `approved`、`score`、`issues[]`、`summary` |
| `fix_request` | 编排器 → 修复 | `code`、`issues[]` |
| `status`/`done`/`failed` | 编排器 → 全部 | `phase`、`round` |

所有消息按序写入 `runs/<run_id>/messages.jsonl`，报告里直接渲染成「通信流水」表格。
设计理由见 [DEC-003](docs/decisions/DEC-003-message-bus.md)。

### 3.4 双层验收（本项目的核心机制）

```
state.code
   │
   ├─▶ 静态检查（AST）：类/方法签名、是否真用 with self._lock、get/put 内有无全表扫描
   │
   ├─▶ 动态验收：干净工作区 + pytest（25 个用例）
   │
   └─▶ 作为「证据」塞进审查 Prompt ──▶ LLM 出 JSON 结论
                                        │
                     approved 且 machine_ok ──▶ APPROVED
                     approved 但机器红     ──▶ 强制驳回 + 记一条 DEC
```

三层保险：Prompt 层给证据、`normalize_review` 层改模型结论、编排器层守终态
（[DEC-006](docs/decisions/DEC-006-machine-verdict.md)）。

---

## 四、三个 Agent

| Agent | 文件 | 职责 | 输入 | 输出 | 温度 |
| --- | --- | --- | --- | --- | --- |
| 编码 Agent | `agents/coder.py` | 按任务说明写第一版完整实现 | `task` 消息 | ```python 代码块 + 说明 | 0.1 |
| 审查 Agent | `agents/reviewer.py` | 逐条对照 REQ 审查，输出结构化结论 | `review_request`（含机器验收证据） | 严格 JSON | 0.0 |
| 修复 Agent | `agents/fixer.py` | 只改被指出的问题，保持结构 | `fix_request`（代码 + 问题清单） | 完整代码块 + 「已修复」清单 | 0.1 |

三个 Agent 的共同点：**无状态、无文件系统访问、只读写收件箱**。差异在 Prompt 与温度：

- 审查 Agent 温度 0：结论要稳定可复现，不能这次说行下次说不行；
- 修复 Agent 的 Prompt 里明确「只改问题清单指出的地方，其余保持原样」，防止顺手重写；
- 审查 Agent 的系统提示里列出五个高频缺陷（无锁、过期不删、覆盖不刷新 TTL、
  容量满不先清过期、全表扫描），这是把人工 review 的经验前置成检查表。

换模型只改 `config/agents.json`，可给每个 Agent 单独配 `model` / `temperature`：

```json
{ "provider": "openai", "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "agents": { "reviewer": { "temperature": 0.0 }, "coder": { "temperature": 0.1 } } }
```

---

## 五、最终验收：带过期时间的 LRU 缓存

### 5.1 交付物

- 实现：[`solutions/ttl_cache.py`](solutions/ttl_cache.py)（约 300 行，含设计注释）
- 测试：[`tests/test_ttl_cache.py`](tests/test_ttl_cache.py)（25 个用例，0.17s 跑完）
- 结果：`python -m pytest -q` → **25 passed**

### 5.2 六条要求怎么满足

| 要求 | 实现 | 对应测试 |
| --- | --- | --- |
| **REQ-1** 容量满淘汰最久未使用 | `OrderedDict` + `move_to_end()` / `popitem(last=False)`，两者 O(1) | `test_evicts_least_recently_used`、`test_get_updates_recency` |
| **REQ-2** TTL 过期，get 视为不存在且移除 | 命中过期分支执行 `del self._data[key]`（惰性删除），而不是只返回 None | `test_expired_entry_is_miss_and_removed`、`test_entry_expires_exactly_at_ttl` |
| **REQ-3** put 覆盖刷新 TTL | 覆盖分支同时刷新 `value` / `expire_at` / `version`，并 `move_to_end` | `test_put_existing_refreshes_ttl` |
| **REQ-4** 过期不长期占容量 | 写入前 `_purge_expired()` 批量清理队首到期条目，仍满才淘汰 LRU；另提供 `purge_expired()` | `test_expired_entries_do_not_block_new_writes`（断言 `evictions == 0`） |
| **REQ-5** 线程安全 | `threading.RLock` + `with` 临界区，统计计数也在锁内 | `test_concurrent_read_write_does_not_break_invariants`、`test_concurrent_put_never_exceeds_capacity` |
| **REQ-6** get/put 均摊 O(1) | 哈希定位 O(1) + 到期双端队列 O(1) 弹出；无排序、无全表扫描 | `test_put_many_with_small_capacity_is_fast`、`test_expiry_queue_stays_bounded` |

### 5.3 为什么这么设计（DEC-007 / DEC-008）

难点在「怎么 O(1) 找到过期条目」。三种方案对比：

| 方案 | 单次复杂度 | 结论 |
| --- | --- | --- |
| 每次扫描全表 | O(n) | ❌ 直接违反 REQ-6 |
| 最小堆存到期时间 | O(log n) | ⚠️ 严格口径不算 O(1)，实现也重 |
| **OrderedDict + 到期双端队列 + 版本号** | **均摊 O(1)** | ✅ 本方案 |

关键洞察：**TTL 全局固定 + 单调时钟** ⇒ 入队顺序天然等于到期顺序，队首就是最早到期，
不需要堆。条目带 `version`，覆盖刷新或淘汰后旧队列记录自动失效；队列长度超过
`2*capacity+32` 时压缩一次，摊销后仍是均摊 O(1)。

代价是不支持 per-key 差异化 TTL —— 题目没有这个要求，明确写进
[DEC-007](docs/decisions/DEC-007-ttlcache-data-structure.md) 的「代价」。

### 5.4 复杂度测试为什么不 flaky

不用 `sleep` 做语义测试，而是**注入假时钟**（`clock` 参数 + `FakeClock`），
TTL 用例全部「推进时间」而非「等待时间」。复杂度用两条稳定断言：
容量 8 vs 20000 的耗时比值（宽松 8 倍阈值，抓数量级退化）+ 内部队列长度上界。
详见 [DEC-009](docs/decisions/DEC-009-testing-approach.md)。

---

## 六、跑一遍

### 6.1 环境

- Python 3.9+（本机用 `D:\python\python.exe`，3.13.9）
- 依赖：`requests`（运行时）、`pytest`（测试）

```powershell
D:\python\python.exe -m pip install -r requirements.txt
```

### 6.2 离线跑通（不需要 API Key，30 秒）

```powershell
cd "D:\web扫雷\工作流"
D:\python\python.exe -m workflow.cli run --task ttl_cache --mock
```

输出（真实运行记录）：

```
[workflow] 首版代码 1213 字符，进入审查
[workflow] 自动验收：静态检查：未通过(线程安全（锁）) | 动态测试：8 failed, 2 passed, 15 errors
[reviewer] 结论：不通过，评分 42，问题 6 条
[workflow] ↩ 审查未通过（6 条问题），进入修复
[fixer]    修复完成，产出 8376 字符，覆盖问题 ['R1'..'R6']
[workflow] 自动验收：静态检查：通过 | 动态测试：25 passed in 0.17s
[reviewer] 结论：通过，评分 97，问题 1 条
[workflow] ✅ 审查通过
```

一次运行产出：

```
runs/<run_id>/
├── state.json          状态（唯一真相源）
├── messages.jsonl      Agent 通信全流水
├── decisions.jsonl     运行级 DEC 正文（DEC-001…005）
├── DECISIONS.md        运行级 DEC 索引
├── REPORT.md           一页式报告
└── artifacts/          v1-coder.py、v2-fixer.py、latest.py
```

### 6.3 接真模型

```powershell
Copy-Item .env.example .env      # 填 AGENT_API_KEY
D:\python\python.exe -m workflow.cli run --task ttl_cache --live --rounds 3 --apply
```

`--apply` 只在 `APPROVED` 时把产物写入 `solutions/`，并记一条 DEC。

### 6.4 常用命令

```powershell
python -m workflow.cli tasks                  # 列出任务
python -m workflow.cli verify --task ttl_cache # 只跑双层验收，不起 Agent
python -m workflow.cli runs                   # 历史运行
python -m workflow.cli run --task ttl_cache --resume --run-id <id>   # 断点续跑
powershell -File scripts\demo.ps1             # 一键演示
powershell -File scripts\verify.ps1           # 一键验收
```

### 6.5 mock 与 live 的区别（别误读）

| | mock | live |
| --- | --- | --- |
| 验证什么 | **编排、状态机、通信、验收、DEC、报告** 全部真实 | 上述全部 + **模型真实能力** |
| 结论怎么来 | `mock/` 下的脚本化内容 | 真实模型输出 |
| 需要 Key | 否 | 是 |
| 代码路径 | 与 live **完全相同**，只换 provider | 同左 |

mock 场景是「编排逻辑的单元测试」，不能作为「模型能写出好代码」的证据。

---

## 七、决策记录（DEC）

要求「每次决策都要留下记录 DEC 编号」，本项目分两层，共 12 条项目级 + N 条运行级：

| 编号 | 主题 | 决定 |
| --- | --- | --- |
| [DEC-001](docs/decisions/DEC-001-tech-stack.md) | 技术栈 | 标准库 + requests，不用编排框架 |
| [DEC-002](docs/decisions/DEC-002-topology.md) | 拓扑 | 三 Agent，定点修复而非重写 |
| [DEC-003](docs/decisions/DEC-003-message-bus.md) | 通信 | 收件箱总线 + JSONL |
| [DEC-004](docs/decisions/DEC-004-state.md) | 状态 | 单一状态源 + 原子落盘 + 迁移白名单 |
| [DEC-005](docs/decisions/DEC-005-llm.md) | 模型 | OpenAI 兼容 + 配置化 + mock |
| [DEC-006](docs/decisions/DEC-006-machine-verdict.md) | 验收 | 机器优先于 LLM |
| [DEC-007](docs/decisions/DEC-007-ttlcache-data-structure.md) | 结构 | OrderedDict + 到期队列，弃堆 |
| [DEC-008](docs/decisions/DEC-008-expiry-policy.md) | 过期 | 惰性删除 + 容量满优先清理，无线程 |
| [DEC-009](docs/decisions/DEC-009-testing-approach.md) | 测试 | 假时钟 + 上界 + 比值 |
| [DEC-010](docs/decisions/DEC-010-github-tool.md) | 上传工具 | 固化目录 + REST API |
| [DEC-011](docs/decisions/DEC-011-round-budget.md) | 预算 | 3 轮熔断 |
| [DEC-012](docs/decisions/DEC-012-traceability.md) | 可追溯 | 运行级 DEC + 自动报告 |

每条 DEC 都写了 **背景 / 可选项 / 决定 / 理由 / 代价与后果 / 复核条件**，
不是「记录我选了什么」，而是「记录为什么不是别的，以及这个选择的边界在哪」。

运行级 DEC 示例（一次 mock 运行，自动生成）：

| 编号 | 阶段 | 决定 |
| --- | --- | --- |
| DEC-001 | INIT | 采用三 Agent：Coder → Reviewer → Fixer |
| DEC-002 | INIT | provider=mock，model=deepseek-chat |
| DEC-003 | INIT | max_rounds=3 |
| DEC-004 | REVIEWING | 第 1 轮审查不通过 → 定点修复 |
| DEC-005 | REVIEWING | 第 2 轮审查通过 → APPROVED |

样例全文：[`examples/sample-run/`](examples/sample-run/)

---

## 八、固定工具：GitHub 上传

这台机器 `git` 不在 PATH 里。此前每次上传都是临时拼脚本，用完就散、无法追溯。
按 [DEC-010](docs/decisions/DEC-010-github-tool.md)，工具**固化**到：

```
C:\Users\47431\.dsh\tools\github-push\
├── ghpush.py      主程序（blob → tree → commit → update ref，一次一个原子 commit）
├── .env           GITHUB_TOKEN / DEFAULT_REPO（永不上传）
├── push.ps1       包装（参数透传）
├── push.cmd       cmd 版包装
├── README.md      用法与安全提醒
└── logs/push.jsonl 每次推送的落盘记录
```

用法：

```powershell
# 预览（不提交）
& D:\python\python.exe C:\Users\47431\.dsh\tools\github-push\ghpush.py `
    --repo owner/name --dir "D:\web扫雷\工作流" --dry-run

# 正式上传
& D:\python\python.exe C:\Users\47431\.dsh\tools\github-push\ghpush.py `
    --repo owner/name --dir "D:\web扫雷\工作流" -m "feat: 三 Agent 工作流"
```

设计要点：

- **零安装**：只要 Python + requests，不需要 git / gh；
- **原子提交**：一次上传 = 一个 commit，不是十几个 "Update xxx"；
- **增量**：本地用 git blob sha1 算内容指纹，只上传真正变化的文件；
- **安全**：默认忽略 `.env` / `runs/` / `__pycache__` / `.venv`，`--delete-missing` 才删远端文件；
- **留痕**：每次推送写一行 `logs/push.jsonl`（时间/仓库/commit/文件数）。

---

## 九、目录结构

```
.
├── README.md                 ← 你正在看
├── DECISIONS.md              项目级 DEC 索引
├── requirements.txt / .gitignore / .env.example / conftest.py
│
├── core/                     基础设施
│   ├── llm_client.py         OpenAI 兼容 + mock provider + 解析容错
│   ├── bus.py                消息总线（收件箱 + JSONL）
│   ├── state.py              状态机 / 迁移白名单 / 原子落盘
│   ├── dec.py                DEC 记录器（自动编号、索引重建）
│   └── verify.py             AST 检查 + pytest 执行
│
├── agents/                   三个 Agent
│   ├── base.py               共同骨架（收件箱/回复/Prompt）
│   ├── coder.py              编码
│   ├── reviewer.py           审查（含强制驳回保险）
│   └── fixer.py              修复
│
├── workflow/                 编排
│   ├── orchestrator.py       状态机主循环 + 决策点
│   ├── task.py               任务定义
│   ├── report.py             REPORT.md 生成
│   └── cli.py                命令行
│
├── tasks/ttl_cache/          任务：需求 / 验收项 / 元信息
├── solutions/ttl_cache.py    ★ 最终实现
├── tests/test_ttl_cache.py   ★ 25 个验收用例
├── mock/                     离线场景数据（含故意留坑的初稿）
├── docs/
│   ├── runbook.md            本地运行手册（命令/产物/排错/退出码）
│   ├── architecture.md       架构说明（分层/状态机/扩展指南）
│   └── decisions/DEC-*.md    12 条项目级决策正文
├── examples/sample-run/      一次完整运行的真实产出
└── scripts/                  demo.ps1 / verify.ps1
```

---

## 十、已知限制

1. **mock 不是模型能力证明**：它只证明编排正确，真实效果请 `--live`。
2. **测试决定上限**：机器验收只覆盖已写的 25 个用例；没覆盖的行为，LLM 审查也未必发现。
3. **单机单写者**：同一 `run_id` 不能被两个进程同时续跑（无文件锁）。
4. **不支持 per-key TTL**：这是 DEC-007 里明确接受的代价。
5. **成本无精细控制**：轮次是唯一预算旋钮，没有 token 计费与限流。
6. **静态检查是启发式**：只把「锁、签名、类存在、语法」设为硬性，
   其余（有序结构、过期清理迹象）降级为警告，避免误杀正确实现。

## 附：相关链接

- **本地运行手册（命令/产物/排错）**：[docs/runbook.md](docs/runbook.md)
- 项目决策索引：[DECISIONS.md](DECISIONS.md)
- 架构说明：[docs/architecture.md](docs/architecture.md)
- 样例运行报告：[examples/sample-run/REPORT.md](examples/sample-run/REPORT.md)
- 上传工具说明：`C:\Users\47431\.dsh\tools\github-push\README.md`
