# 公共环境探测（被其他脚本 dot-source 引用）
#   用法: . "$PSScriptRoot\_env.ps1"
# 目标：脚本里不出现任何绝对路径，仓库搬到哪台机器、哪个盘都能直接跑。
#   - Python 解释器：优先 -Python 参数，其次 $env:PYTHON，最后在 PATH 里找 python/python3/py
#   - 仓库根目录：由本文件位置向上推，天然相对
#   - GitHub 上传工具：优先 -GhPush，其次 $env:GH_PUSH_HOME，其次 PATH 上的 ghpush

$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-Python {
    param([string]$Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        return $Explicit
    }
    if ($env:PYTHON) {
        if (Test-Path $env:PYTHON) { return (Resolve-Path $env:PYTHON).Path }
        return $env:PYTHON
    }
    foreach ($name in @('python', 'python3', 'py')) {
        # 同一台机器上可能有多个同名候选（比如应用商店空壳在前、真解释器在后），逐个实测
        foreach ($cmd in (Get-Command $name -All -ErrorAction SilentlyContinue)) {
            try { $out = & $cmd.Source --version 2>&1 | Out-String } catch { continue }
            if ($out -match 'Python 3') { return $cmd.Source }
        }
    }
    throw @"
未找到可用的 Python 3 解释器。
  1) 安装 Python 3.9+ 并确保 python 在 PATH 里；或
  2) 设置环境变量 PYTHON 指向解释器，例如：
       `$env:PYTHON = 'C:\Python312\python.exe'      # 当前会话
       setx PYTHON "C:\Python312\python.exe"          # 永久
  3) 或调用脚本时传 -Python <解释器路径>
"@
}

function Resolve-GhPush {
    param([string]$Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        throw "找不到 ghpush 脚本：$Explicit"
    }
    if ($env:GH_PUSH_HOME) {
        $candidate = Join-Path $env:GH_PUSH_HOME 'ghpush.py'
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    $onPath = Get-Command ghpush -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    # 约定：上传工具与仓库同级（<父目录>\github-push\ghpush.py）
    $sibling = Join-Path (Split-Path -Parent $script:RepoRoot) 'github-push\ghpush.py'
    if (Test-Path $sibling) { return (Resolve-Path $sibling).Path }

    throw @"
未找到 ghpush.py（GitHub 上传工具）。
  1) 设置环境变量 GH_PUSH_HOME 指向工具目录：
       `$env:GH_PUSH_HOME = 'C:\path\to\github-push'
       setx GH_PUSH_HOME "C:\path\to\github-push"     # 永久
  2) 或把 ghpush.py 所在目录加入 PATH（命令名 ghpush）
  3) 或调用脚本时传 -GhPush <ghpush.py 路径>
"@
}
