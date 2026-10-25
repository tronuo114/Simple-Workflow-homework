# DEC-017 · Agent 注册表与可插拔流水线

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-002、DEC-014、DEC-016

## 背景

评分维度里「可扩展性 20%：架构是否支持新增 Agent、修改流程」。
改造前，编排器里写死了三行：

```python
self.coder = CoderAgent(...)
self.reviewer = ReviewerAgent(...)
self.fixer = FixerAgent(...)
```

新增一个 Agent 要改 import、实例化、审查流程三处；把「主审查」换成别的角色，
只能改代码。这既有维护成本，也说不清「怎么扩展」。

## 可选项

1. 保持写死，文档里写「照着改即可」——口说无凭，评审看不到机制。
2. 引入插件框架（entry points / importlib 扫描）——灵活，但对 4 个 Agent 是过度设计。
3. **显式注册表 + 配置驱动流水线**（本方案）：名字 → 构造函数的字典，流水线由配置决定。
4. 用 DI 容器——同样过重。

## 决定

新增 `agents/registry.py`：

```python
register_agent("my_reviewer", lambda llm, bus, verbose: MyReviewer(llm, bus, verbose=verbose))
```

`build_pipeline(config, llm, bus, parallel=…)` 读取 `config["pipeline"]`：

```json
{ "pipeline": { "coder": "coder", "fixer": "fixer",
                "reviewers": ["reviewer", "security_reviewer"] } }
```

- 第一个审查者是主审查，其余在 `--parallel-review` 时并发（顺序模式只建主审查，省一次调用）；
- 默认值与改造前完全一致，因此行为不变、测试全部保留；
- 编排器只依赖 `pipeline["coder" / "reviewer" / "fixer" / "extra_reviewers"]`，
  **不再 import 具体 Agent 类**。

新增一个 Agent 的三步（`tests/test_extensibility.py` 里有一个 10 行的完整例子）：

1. 继承 `BaseAgent`，声明 `name/handles/system_prompt`，实现 `process()`；
2. 模块导入时 `register_agent("名字", ...)`；
3. 把名字加进 `config/agents.json` 的 `pipeline.reviewers`（或用 `--pipeline`/配置覆盖）。

## 理由

- **扩展成本从「改 3 处源码」降到「加 1 行注册 + 1 行配置」**，且能被测试直接证明；
- 注册表是纯数据结构，不是框架：没有生命周期、没有依赖注入，出问题一眼能看懂；
- 配置驱动让「同一个工作流跑不同流水线」成为可能（例如只跑主审查以省成本）。

## 代价与后果

- 多了一层间接：`Orchestrator` 里看不到具体 Agent 类型，读代码要跳到 `registry.py`；
  用类型注解与默认配置表把这点说明白。
- 注册靠模块导入副作用：新增 Agent 必须在某处被 import，否则注册不生效。
  约定放在 `agents/__init__.py` 或 `registry.py` 底部，测试里显式 import。
- 名字冲突以覆盖方式处理（便于测试替换实现），生产环境需靠命名规范避免。

## 复核条件

若流水线需要「按条件启用某个 Agent」（如仅当 `pytest` 覆盖不足时启用测试审查），
当前「静态 reviewers 列表」不够用，应升级为「阶段 + 条件」的声明式流水线，
并保持 `loop_trace` / 事件流字段兼容。
