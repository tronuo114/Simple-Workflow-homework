# DEC-013 · 可移植性：仓库内零绝对路径，环境差异只靠两个环境变量

- **状态**：已采纳
- **日期**：2026-10-25
- **范围**：项目级
- **关联**：DEC-010、README「跑一遍」、`docs/runbook.md`

## 背景

第一版为了「在我这台机器上一定能跑」，文档和脚本里散落了大量机器相关字符串：

- `D:\python\python.exe`（解释器绝对路径）
- `D:\web扫雷\工作流`（仓库绝对路径）
- `C:\Users\47431\.dsh\tools\github-push\`（上传工具绝对路径）

随后仓库目录被改名（`D:\web扫雷` → `D:\project`），并要求「在别的电脑上也能用」。
如果继续散落绝对路径，换机器等于要改十几个文件，而且每次改名都会再坏一遍。

## 可选项

1. **保留绝对路径，文档加一句「请自行替换」**：零改动，但每台机器都要手工改，且改漏就报错。
2. **仓库内零绝对路径 + 两个环境变量**：路径一律由 `__file__` / `$PSScriptRoot` 推导，
   机器差异收敛到 `PYTHON`（解释器）与 `GH_PUSH_HOME`（外部上传工具）。
3. **把 Python 解释器和上传工具都塞进仓库**：绝对自包含，但仓库体积几十~上百 MB，
   且 Windows/Linux 安装包不通用，版本管理也混乱。

## 决定

选 **方案 2**，并落实为四条硬规则：

1. **Python 侧**：所有路径从 `Path(__file__)` 推导（`PROJECT_ROOT`、`TOOL_DIR`），
   不出现任何盘符。测试、任务、配置路径全部相对仓库根。
2. **PowerShell 侧**：`$PSScriptRoot` 推导仓库根；解释器解析顺序
   `-Python 参数 → $env:PYTHON → PATH 上的 python/python3/py（逐个实测 --version）`；
   上传脚本解析顺序 `-GhPush → $env:GH_PUSH_HOME → PATH 上的 ghpush → 与仓库同级的 github-push/`。
3. **文档侧**：命令统一写 `python -m ...`、`--dir .`，
   外部工具用 `$env:GH_PUSH_HOME` 引用，不再出现任何机器路径。
4. **脚本编码**：所有 `.ps1` 存为 **UTF-8 with BOM**。Windows PowerShell 5.1 读无 BOM 的
   UTF-8 脚本会按 ANSI 解析，中文全变乱码——这个坑实测踩到了（`仓库根` 显示成 `浠撳簱鏍`）。

另加两个便利：`scripts/_env.ps1` 统一做探测，`scripts/push.ps1` 让仓库自己也能一键上传，
避免每个使用者去记外部工具的路径。

## 理由

- **换机器成本 = 配两个环境变量**：有 `python` 在 PATH 的机器一个都不用配。
- 不把解释器塞进仓库：体积、平台、版本三重负担，而且和「作业仓库」定位不符。
- 上传工具仍留在仓库外（DEC-010 的决定不变），但通过 `GH_PUSH_HOME` 解耦，
  工具搬到哪都不影响仓库。
- 探测逻辑对「Windows 应用商店的 python.exe 空壳」做了实测校验（该壳跑 `--version` 无输出），
  否则会误判成可用解释器，报错信息会非常误导。

## 代价与后果

- 首次使用要做一次性设置，`runbook.md` 里写成三步。**环境变量对已运行的进程不生效**，
  设完需要重开终端（或重启 IDE/DSH）才会被读到。
- 解释器探测多花几十毫秒（要跑 1-3 次 `--version`），可忽略。
- 脚本存 BOM 后，某些非 Windows 编辑器可能显示 `\ufeff` 前缀；`python` 读取 `.py` 不受影响，
  `.ps1` 只由 PowerShell 执行，故可接受。
- 仍然依赖 PATH 上有 `powershell`（Windows 自带），Linux 上 `.ps1` 不可用；
  但 Python 入口（`python -m workflow.cli`）是跨平台的。

## 复核条件

若要把本仓库打包分发给完全没装 Python 的人，应改用内置 venv + `uv`/`pyinstaller` 方案，
而不是继续扩展环境变量探测（那会变成半个包管理器）。
