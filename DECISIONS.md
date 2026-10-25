# DECISIONS · 决策索引

> 每一次「选择」都留编号。本项目分两层：
>
> - **项目级 DEC**（`DEC-001` … `DEC-012`）：架构、技术选型、算法取舍等长期有效的决定，正文在 `docs/decisions/`。
> - **运行级 DEC**（每次运行 `DEC-001` 起，独立编号）：这次运行为什么进入修复、为什么强制驳回、
>   为什么熔断、为什么落地，自动写入 `runs/<run_id>/decisions.jsonl`，索引见同目录 `DECISIONS.md`，
>   并汇总进 `REPORT.md`。样例见 `examples/sample-run/`。

## 项目级决策

| 编号 | 主题 | 决定 | 主要代价 |
| --- | --- | --- | --- |
| [DEC-001](docs/decisions/DEC-001-tech-stack.md) | 技术栈 | 纯 Python 标准库 + `requests`，不用编排框架 | 代码量自担，无现成调度能力 |
| [DEC-002](docs/decisions/DEC-002-topology.md) | 工作流拓扑 | 编码 → 审查 → 修复 三 Agent，编排器持控制权 | 多一轮调用成本；Agent 输出格式须严格 |
| [DEC-003](docs/decisions/DEC-003-message-bus.md) | Agent 通信 | 收件箱式消息总线 + `messages.jsonl` 全量落盘 | 消息体积大；载荷无 schema 校验 |
| [DEC-004](docs/decisions/DEC-004-state.md) | 状态管理 | 单一 `WorkflowState` + 原子落盘 + 迁移白名单 | 单写者；加阶段要同步白名单 |
| [DEC-005](docs/decisions/DEC-005-llm.md) | 模型接入 | OpenAI 兼容层 + 配置化 + 内置 mock provider | 兼容层细微差异需自行处理 |
| [DEC-006](docs/decisions/DEC-006-machine-verdict.md) | 验收权威 | 机器验收优先于 LLM 判断（三层卡口） | 测试不全时机器通过≠正确 |
| [DEC-007](docs/decisions/DEC-007-ttlcache-data-structure.md) | 缓存结构 | `OrderedDict` + 到期双端队列 + 版本号；放弃最小堆 | 不支持 per-key TTL |
| [DEC-008](docs/decisions/DEC-008-expiry-policy.md) | 过期清理 | 惰性删除 + 容量满时优先清理，不起后台线程 | 极端场景过期条目多待一会儿 |
| [DEC-009](docs/decisions/DEC-009-testing-approach.md) | 测试策略 | 假时钟 + 结构上界 + 宽松比值测试 | 白盒断言绑定实现 |
| [DEC-010](docs/decisions/DEC-010-github-tool.md) | 上传工具 | 独立工具目录（`GH_PUSH_HOME` 引用）+ REST API | 无分支合并/LFS 等 git 能力 |
| [DEC-011](docs/decisions/DEC-011-round-budget.md) | 预算熔断 | 3 轮上限，超限 `REJECTED` 并交出问题清单 | 复杂任务可能被过早截断 |
| [DEC-012](docs/decisions/DEC-012-traceability.md) | 可追溯 | 项目级 + 运行级 DEC + 自动 `REPORT.md` | 运行产物不进仓库，需看 `examples/` |
| [DEC-013](docs/decisions/DEC-013-portability.md) | 可移植性 | 仓库内零绝对路径，只留 `PYTHON` / `GH_PUSH_HOME` 两个环境变量 | 首次使用需一次性配置 |

## 决策之间的依赖

```
DEC-001 技术栈
 ├── DEC-003 消息总线 ── DEC-004 状态管理 ── DEC-012 可追溯
 ├── DEC-005 模型接入 ── DEC-002 拓扑 ── DEC-011 轮次预算
 └── DEC-006 机器验收 ── DEC-009 测试策略
                        └── DEC-007 缓存结构 ── DEC-008 过期策略
DEC-010 上传工具（环境级，独立于代码设计）── DEC-013 可移植性（约束所有路径）
```

## 运行级 DEC 怎么看

```powershell
# 最近一次运行的决策索引
Get-Content (Get-ChildItem runs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName\DECISIONS.md

# 完整决策正文（JSONL）
Get-Content runs\<run_id>\decisions.jsonl
```

样例（离线 mock 运行，2 轮通过）已整理到 [`examples/sample-run/`](examples/sample-run/)。
