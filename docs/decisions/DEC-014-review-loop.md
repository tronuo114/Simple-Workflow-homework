# DEC-014 · 用显式循环体取代「单次调用」流程

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-002、DEC-011、DEC-012

## 背景

最早一版编排器把「编码 → 审查 → 修复」写成了直线流程：每个 Agent 在一次运行里
按固定顺序被调用一次或两次，重试的「循环」藏在状态迁移里：

```python
if state.phase == Phase.FIXING:
    self._phase_fix(state)
while state.phase == Phase.REVIEWING:     # 循环条件其实是状态机的副作用
    self._phase_review(state)
    if state.phase == Phase.FIXING:
        self._phase_fix(state)
```

这带来三个具体问题：

1. **循环不是一等公民**：读代码时看到的是「阶段」而不是「循环」，答辩时无法一句
   话说明「这套流程什么时候停」；作业要求的「循环控制」只能靠解释状态机来间接证明。
2. **调用次数被写死**：`coder / reviewer / fixer` 的调用点固定，想「再多审一轮」
   要改流程，而不是改循环条件。
3. **没有循环级的观测**：`state.reviews` 只记审查结论，没有把「这一轮 LLM 说了什么、
   机器说了什么、走了哪个分支」记在一起，复盘只能靠拼时间线。

## 可选项

1. **保持直线流程**：每个 Agent 单次调用，靠外部脚本重跑。实现最简单，但不满足
   「循环直到审核通过」的要求。
2. **引入通用编排框架**（LangGraph / prefect / Airflow）：循环是现成的 DAG/状态图能力，
   但要装依赖、写配置，MVE 的目标变成学框架。
3. **显式 while 循环体**（本方案）：把「审查 + 分支 + 修复」写进一个 `while`，
   条件由审查结论决定，循环轨迹单独记账。
4. **递归**：`review()` 不通过时递归调用自己。语义等价，但栈深与轮次预算更难讲清楚。

## 决定

选 **方案 3**。`workflow/orchestrator.py` 的 `_loop_until_approved()` 是唯一的循环体：

```python
while state.phase == Phase.REVIEWING:
    verdict = self._phase_review(state)   # 审查 + 分支判断，返回 approved/rejected/retry
    self._trace_loop(state, verdict)      # 本次循环进账本
    if verdict != VERDICT_RETRY:
        break                             # 通过或熔断 → 离开循环
    self._phase_fix(state)                # 不通过 → 修复 → 回到 while 条件
```

配套约束：

- **三个出口显式化**：`approved`（审核通过）、`rejected`（`max_rounds` 耗尽熔断）、
  异常（`run()` 兜住转 `FAILED`）。循环体内不存在其它 `return`/`break`。
- **每轮一条轨迹**：`state.loop_trace` 记录 `iteration / round / approved / machine_ok /
  score / issues / verdict / action`，随 `state.json` 落盘，报告里渲染成「循环轨迹」表。
- **预算仍在循环外判**：`max_rounds` 由 `_phase_review` 检查并返回 `rejected`，
  循环体本身不关心数字，只关心分支结果。想换策略（比如按分数收敛）只改 `_phase_review`。
- **状态机保留**：`Phase.TRANSITIONS` 仍在，循环体只是让「循环」这件事在代码里可见，
  不是替换状态机。两者职责不同：状态机保证迁移合法，循环体保证流程收敛。

## 理由

- 作业要求「至少 2 个 Agent 协作 + 流程控制（顺序执行）+ 推荐循环」。
  显式 `while` 让「循环」在源码里可指认，不需要读者先理解状态机。
- 循环条件与业务判定解耦：`_phase_review` 返回枚举值，循环体只做 `break / continue`。
  以后加「人工审核节点」只需多一个 verdict，循环骨架不动。
- 轨迹账本让「第几轮为什么被打回」变成结构化数据，报告、测试断言都能直接引用
  （见 `tests/test_workflow_loop.py`）。

## 代价与后果

- 状态机与循环体存在**概念重叠**（都能让流程回到审查）。为免两套逻辑打架，
  约定：**循环体负责「要不要再来一轮」，状态机只负责「这次迁移合不合法」**。
  新增阶段时必须同时检查这两处。
- `loop_trace` 会让 `state.json` 随轮次线性增长（每轮约 10 个字段，可忽略）。
- 测试需要可控的「第 N 轮结论」，因此在 `Orchestrator.__init__` 增加了可注入的
  `llm` 参数（生产路径仍默认构造 `LLMClient`）。这是为可测性付出的一个构造参数。

## 复核条件

若未来把「一轮」扩展成含并行审查（多个 reviewer 同时出结论），循环体的
「一次迭代 = 一次审查」假设会被打破，届时应把循环体升级为「一个超步（super-step）」，
并在 `loop_trace` 里记录每个并行分支的结论，而不是当前的单条结论。
