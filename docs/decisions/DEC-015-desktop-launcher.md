# DEC-015 · 桌面启动面板：单文件 exe + 三个子命令

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-001、DEC-004、DEC-013

## 背景

命令行入口（`python -m workflow.cli ...`）对开发者够用，但作业/演示的「打开就能用」
体验差：要先确认解释器、装依赖、记参数。需求方要求一个 **exe 启动面板的入口**。

真正的难点不是画界面，而是**打包后的运行环境**：

1. `sys.executable` 变成面板自己，`python -m pytest` 不再可用，
   而 `core/verify.py` 每轮都要跑 pytest；
2. `Path(__file__).parent.parent` 指向 PyInstaller 的解包临时目录，
   配置/任务会找不到，`runs/` 会写进临时目录、退出即丢；
3. 单文件 exe 会多一层 bootloader 父进程，窗口在子进程上。

## 可选项

1. **bat 包装脚本**：`start python -m workflow.cli`。零成本，但仍要求装 Python。
2. **Electron/Tauri 前端**：界面漂亮，但要 Node 工具链、体积大、与 Python 编排层跨进程通信。
3. **PyInstaller + Tkinter 单文件 exe**（本方案）：依赖少，能打包解释器与 pytest。
4. **onedir 打包 + 快捷方式**：启动更快，但产物是一个目录，不满足「一个 exe 入口」。

## 决定

选 **方案 3**，并配套三个约定：

| 问题 | 决定 |
| --- | --- |
| 打包后怎么调命令行 | 一份 exe 三种模式：无参数=GUI、`--cli ...`=转发给 `workflow.cli`、`--pytest ...`=装载打包内的 pytest |
| 打包后怎么找项目根 | `core/paths.app_root()`：`WORKFLOW_HOME` → exe 所在目录/上一级（有 `tasks/` 即认为仓库根）→ 解包目录 → 源码目录 |
| 打包后怎么跑 pytest | `core/verify._pytest_command()` 检测 `sys.frozen`，改调 `exe --pytest`，退出码原样回传 |
| 面板怎么跑工作流 | 子进程 + 管道，实时回显；「停止」用 `taskkill /T` 杀进程树 |

产物：`scripts/build_exe.ps1` → `dist/WorkflowPanel.exe`（约 18 MB，含 tasks/tests/mock/config）。

## 理由

- **GUI 用 Tkinter**：标准库自带，PyInstaller 打包稳定，不需要额外运行时；面板逻辑一百多行就够。
- **子进程而不是线程内调用**：长任务不阻塞界面，「停止」能真杀干净，每次运行是干净进程，
  不会带上一次的内存状态或模块缓存。
- **`--pytest` 子命令**：避免在打包产物里假设系统装了 Python，也不必把测试改成
  「在内存里 import 被测模块」（那样会污染 pytest 的模块缓存，多轮跑结果不可信）。
- **根目录解析收敛到 `core/paths.py`**：`task.py`、`verify.py`、`llm_client.py` 各自原来的
  `__file__` 推断全部替换，打包与源码两种运行方式共用一套路径逻辑。

## 代价与后果

- exe 体积约 18 MB；单文件每次启动要解包（首次约 1-2 秒，之后走系统缓存）。
- `--pytest` 子命令为了与原命令行一致，参数按 argv 原样转发，**不再解析 `-m pytest`**，
  所以只能通过 `verify` 调用，不支持手写 `exe --pytest -k xxx`。
- 打包依赖 PyInstaller，只在构建时需要；运行时依赖仍是 `requests` + 标准库。
- 打包脚本必须在 UTF-8 BOM 下保存（Windows PowerShell 5.1 无 BOM 会按 ANSI 解析中文）。

## 复核条件

若将来引入需要编译扩展的验收器（如 mypy、ruff 的二进制），或需要给面板加图表/富文本，
再评估换成 onedir 打包或把 pytest 换成「进程内子解释器」方案；当前单文件 + 三子命令
已覆盖「双击可用、命令行可调试」两个诉求。
