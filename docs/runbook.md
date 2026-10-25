# 本地运行手册（Runbook）

> “在我这台机器上要怎么跑”。**仓库内没有任何绝对路径**，换电脑只需配环境变量。
> 设计理由看 `docs/architecture.md`，决策背景看 `DECISIONS.md`。

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| Python | 3.9+（仓库代码兼容 3.9；本机实测 3.13.9） |
| 依赖 | `requests`（运行时）、`pytest`（测试） |
| 工作目录 | **仓库根目录**（本文件所在目录的上一级），在哪都行 |
| 编码 | 终端设 UTF-8，否则中文输出可能乱码 |

```powershell
cd <仓库根>
$env:PYTHONIOENCODING = "utf-8"

# 装依赖（已装可跳过）
python -m pip install -r requirements.txt

# 自检
python -m pytest        # 期望 43 passed（25 缓存 + 5 循环 + 7 面板 + 3 聊天模式 + 3 并行审查）
```

### 解释器探测顺序（脚本会自动找，不用你写路径）

```
-Python <路径>  →  $env:PYTHON  →  PATH 上的 python / python3 / py（逐个实测 --version）
```

Windows 应用商店那个 `python.exe` 是空壳（跑 `--version` 无输出），探测时会自动跳过。
如果都找不到，一次性设好即可：

```powershell
setx PYTHON "C:\Python312\python.exe"     # 永久（新终端生效）
$env:PYTHON = "C:\Python312\python.exe"   # 仅当前会话
```

### 上传工具（`GH_PUSH_HOME`）

上传走仓库外的固定工具（见 [DEC-010](decisions/DEC-010-github-tool.md)）：

```powershell
setx GH_PUSH_HOME "C:\path\to\github-push"
```

解析顺序：`-GhPush <ghpush.py>` → `$env:GH_PUSH_HOME` → PATH 上的 `ghpush` → 与仓库同级的 `github-push\`。
**注意：环境变量对已运行的进程不生效，设完要重开终端。**

## 1. 三种用法，先搞清楚区别

| 用法 | 起 Agent 吗 | 需要 Key | 用途 | 耗时 |
| --- | --- | --- | --- | --- |
| `verify` | ❌ | ❌ | 只验收代码：AST 检查 + pytest | ~1 秒 |
| `run --mock` | ✅（脚本化输出） | ❌ | 演示/自测整条编排链路 | ~2 秒 |
| `run --live` | ✅（真模型） | ✅ | 真实产出代码 | 30 秒–几分钟 |

不想敲命令就双击面板（详见第 11 节）：

```
WorkflowPanel.exe        # 输入一句需求 → 发送，面板把「编码/验收/审查/分支/修复」逐条显示
```

```powershell
# ① 只验收
python -m workflow.cli verify --task ttl_cache

# ② 离线跑通工作流（默认就是 mock，可不写 --mock）
python -m workflow.cli run --task ttl_cache --mock

# ③ 接真模型：先建 .env 填 AGENT_API_KEY
Copy-Item .env.example .env
#   编辑 .env -> AGENT_API_KEY=sk-xxxx
python -m workflow.cli run --task ttl_cache --live --apply
```

`--apply` 只在结果为 `APPROVED` 时把产物写进 `solutions/`，并记一条 DEC。不加就只落在 `runs/` 里。

不想敲 Python 命令也行，两个包装脚本会自己找解释器并切到仓库根：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\push.ps1 -DryRun
```

## 2. 一次 run 内部发生了什么

```
INIT
 └─ 记录 DEC-001~003（拓扑 / provider / 轮次上限）
CODING
 └─ publish task（题面 + 验收项 + 验收契约）→ coder → 收到 code → artifacts/v1-coder.py
REVIEWING ◀───────────────────────────────┐  ← 循环体从这里开始
 ├─ verify：AST 静态检查 + 干净工作区跑 pytest │
 ├─ publish review_request（代码 + 契约 + 验收证据）→ reviewer → JSON 结论 │
 └─ 三条分支：                            │
    ├─ approved 且机器通过 → APPROVED（记 DEC）→ 退出循环 │
    ├─ approved 但机器红   → 强制驳回（记 DEC）→ FIXING   │
    └─ 不通过              → 有预算进 FIXING / 预算耗尽 REJECTED（记 DEC）→ 退出循环
FIXING
 └─ publish fix_request（代码 + 问题清单 + 契约 + pytest 报错）→ fixer → artifacts/vN-fixer.py ──┘
```

预算：`max_rounds=3`（`--rounds` 可覆盖）。最坏调用次数：1 次编码 + 3 次审查 + 2 次修复。
循环跑了几次、每轮分支结果，看 `state.json` 的 `loop_trace` 或 `REPORT.md` 的「循环轨迹」表。

## 3. 产物在哪、怎么看

```
runs/<run_id>/
├── state.json        状态真相源（阶段/轮次/代码/审查结论）
├── messages.jsonl    全部 Agent 消息（按 seq 有序）
├── decisions.jsonl   运行级 DEC 正文
├── DECISIONS.md      运行级 DEC 索引表
├── REPORT.md         一页式报告 ← 先看这个
├── artifacts/        v1-coder.py、v2-fixer.py、latest.py
└── workspace/        跑 pytest 的干净工作区（每轮重建，可删）
```

最常用的三条命令（在仓库根执行）：

```powershell
# 列出所有运行及状态
python -m workflow.cli runs

# 打开最近一次的完整报告
Get-Content (Get-ChildItem runs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName\REPORT.md

# 看“编码第一版”和“修复后第二版”
explorer (Get-ChildItem runs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName\artifacts
```

`runs/` 已在 `.gitignore` 里，不会上传；要留证据就拷到 `examples/`。

## 4. 日常开发循环

```powershell
# 1) 改代码（solutions/ 或 agents/ 或 workflow/）
# 2) 快速验收，确认没改坏
python -m pytest
python -m workflow.cli verify --task ttl_cache

# 3) 跑一遍完整工作流，确认编排没回归
python -m workflow.cli run --task ttl_cache --mock

# 4) 看 REPORT.md + decisions.jsonl
# 5) 上传（仓库内包装脚本，自动定位外部工具）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\push.ps1 `
    -Message "feat: 说明本次改动"
```

上传前建议先 `-DryRun` 看一眼文件清单，确认没有 `.env` 之类的东西混进去。

## 5. 配置优先级

`config/agents.json` → 项目 `.env` → 进程环境变量 → 命令行参数（后者覆盖前者）。

| 想改什么 | 改哪里 |
| --- | --- |
| 换模型 / base_url / 温度 | `config/agents.json`（或 `--model` / `--base-url`） |
| 密钥 | `.env` 的 `AGENT_API_KEY`（环境变量 `AGENT_*` 优先） |
| 轮次上限 | `config/agents.json` 的 `limits.max_rounds`，或 `--rounds` |
| 只跑静态检查 | `--no-dynamic-verify` |

`.env` 格式（一行一个 `KEY=VALUE`，`#` 注释）：

```
AGENT_API_KEY=sk-xxxx
# AGENT_BASE_URL=https://api.deepseek.com/v1
# AGENT_MODEL=deepseek-chat
```

## 6. 中断与续跑

任意阶段 Ctrl+C 或进程被杀，状态都已原子落盘。续跑：

```powershell
python -m workflow.cli run --task ttl_cache --resume --run-id <run_id>
```

- 只会从 `CODING` / `REVIEWING` / `FIXING` 继续；`APPROVED` / `REJECTED` / `FAILED` 是终态，直接出报告。
- 续跑会记一条「断点续跑」DEC，不会重号。
- 同一个 `run_id` 不能被两个进程同时续跑（无文件锁）。

## 7. 退出码（接脚本用）

| 码 | 含义 |
| --- | --- |
| `0` | `APPROVED` / 验收通过 |
| `1` | `REJECTED` / `FAILED` / 验收未通过 |
| `2` | 参数或文件问题（如 `verify --file` 指向的文件不存在） |

## 8. 常见错误对照

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `未找到可用的 Python 3 解释器` | PATH 上没有可用 python | 设 `PYTHON`，或 `-Python <路径>` |
| `未找到 ghpush.py` | 上传工具没配 | 设 `GH_PUSH_HOME`，或 `-GhPush <路径>` |
| `缺少 API Key：在 .env 里设置 AGENT_API_KEY` | 用了 `--live` 但没有 `.env` | 建 `.env`，或改回 `--mock` |
| `XXX 返回的代码里没有 class TTLCache` | 模型输出被截断/没按格式给代码块 | 调大 `max_tokens`，或换模型 |
| `无法从输出中解析 JSON` | 审查模型没吐纯 JSON | 换模型；`--live` 下默认已开 `json_mode` |
| `pytest 超时（>180s），疑似死锁` | 被测代码死锁 | 看 `runs/<id>/workspace`，单独跑测试定位 |
| `IllegalTransition: 非法状态迁移` | 手工改了 `state.json` 或跳阶段 | 别手改状态；检查 `Phase.TRANSITIONS` |
| `401 Token 无效或已过期` | GitHub Token 过期 | 更新工具目录 `.env` 的 `GITHUB_TOKEN` |
| 脚本输出中文乱码 | 终端/脚本编码 | 终端 `$env:PYTHONIOENCODING="utf-8"`；`.ps1` 已存 UTF-8 BOM |

## 9. 加一个新任务

```
tasks/<task_id>/
├── task.md           题面（喂给编码 Agent）
├── requirements.md   验收项，编号 REQ-1…REQ-n（审查 Agent 按编号核对）
└── meta.json         {"module_name","class_name","target_file","tests":["test_xxx.py"],"max_rounds":3}
```

1. 测试文件放 `tests/`，文件名写进 `meta.json` 的 `tests`；
2. `python -m workflow.cli tasks` 确认能被识别；
3. `run --task <task_id> --mock` 先验证编排不报错，再 `--live`。

同一套 `agents/` + `core/` 不用改——这也是当初把 Agent 设计成“只认消息”的目的。

## 10. 换一台电脑怎么开始

```powershell
# 1) 拿到仓库（zip 或 git clone 都行），cd 到仓库根
# 2) 配两个环境变量（有 python 在 PATH 时第 1 个可省）
setx PYTHON "C:\Python312\python.exe"
setx GH_PUSH_HOME "C:\path\to\github-push"
# 3) 重开终端
python -m pip install -r requirements.txt
python -m pytest                 # 43 passed
python -m workflow.cli run --task ttl_cache --mock
```

除这两个环境变量外，仓库里没有任何与本机相关的东西：不写盘符、不写用户名、
不写解释器路径，`.env` 也不入库。

## 11. 桌面面板与打包

### 11.1 直接用

```
双击 WorkflowPanel.exe
```

exe 里打包了 Python、requests、pytest 和 `tasks/ tests/ mock/ config/`，目标机器不需要装 Python。
面板是聊天式的：输入需求（或点「示例需求」）→ Enter 发送，工作流自己跑到审核通过；
面板固定 live（离线 mock 已禁用）；可调轮次、并行审查，可停止（杀进程树）、可打开最近报告。

斜杠指令：`/help`、`/bind <API_KEY>`（写 `.env`、回显脱敏）、`/unbind`、`/test`（链路自检）、
`/model`、`/base`、`/rounds`、`/parallel`、`/status`、`/version`、`/report`、`/run <需求>`、
`/clear`、`/stop`。面板固定 live，离线 mock 已禁用；`help` 等不加斜杠也认。

> 改完代码一定要**关掉旧面板再重新双击**：Windows 上已启动的进程不会自动换成新 exe。
> 用 `/version`（面板内）或 `WorkflowPanel.exe --version` 确认构建时间。

### 11.2 重新打包（改了代码之后）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
# 或指定解释器： -Python "C:\Python312\python.exe"
# 依赖已装好可加 -SkipDeps
```

产物：`WorkflowPanel.exe`（约 24 MB）。exe 随仓库提交（评审可直接双击），`build/` 中间目录不入库。

### 11.3 同一条命令行的三种模式

```powershell
WorkflowPanel.exe                                    # 打开面板
WorkflowPanel.exe --version                          # 构建时间（判断是否旧 exe）
WorkflowPanel.exe --selftest                         # 自检斜杠指令是否可用
WorkflowPanel.exe --cli run --task ttl_cache --mock   # 命令行（面板内部就是这么调的）
WorkflowPanel.exe --pytest -q tests                   # 打包内 pytest（供 core/verify 使用）
```

### 11.4 打包相关的坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `hook-workflow.py` 报错 | PyInstaller 社区 hook 是给 PyPI 的 `workflow` 包准备的 | 已用 `pyinstaller-hooks/hook-workflow.py` 覆盖，`launcher.spec` 的 `hookspath` 指向它 |
| 打包后 pytest 跑不起来 | `sys.executable` 变成面板自己 | `core/verify._pytest_command()` 在 `sys.frozen` 时改用 `exe --pytest` |
| 面板找不到任务 / `runs/` 写丢 | `__file__` 指向解包临时目录 | `core/paths.app_root()` 按 `WORKFLOW_HOME` → exe 目录/上一级 → 解包目录解析 |
| 双击没反应 | 单文件 exe 首次要解包，或被 SmartScreen 拦 | 等 1-2 秒；右键属性解除锁定；或用 `WorkflowPanel.cmd` |
| 打包脚本中文乱码 | PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1 | `scripts/*.ps1` 必须存为 UTF-8 **带 BOM** |

