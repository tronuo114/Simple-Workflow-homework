# 打包启动面板 exe（PyInstaller，单文件）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
#      可加 -Python <解释器> 覆盖自动探测，-SkipDeps 跳过依赖安装
param([string]$Python, [switch]$SkipDeps)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_env.ps1"

$py = Resolve-Python $Python
Set-Location $script:RepoRoot
$env:PYTHONIOENCODING = "utf-8"

Write-Host "仓库根: $script:RepoRoot" -ForegroundColor DarkGray
Write-Host "解释器: $py" -ForegroundColor DarkGray

if (-not $SkipDeps) {
    Write-Host "`n== 安装打包依赖 ==" -ForegroundColor Cyan
    & $py -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
}

Write-Host "`n== PyInstaller 打包（单文件，含 tasks/tests/mock/config）==" -ForegroundColor Cyan

# 写入构建时间，面板里 /version 可以看到，用来判断是不是在跑旧 exe
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$infoPath = Join-Path $script:RepoRoot 'launcher\build_info.py'
[System.IO.File]::WriteAllText(
    $infoPath,
    "BUILD_TIME = `"$stamp`"`n",
    (New-Object System.Text.UTF8Encoding($false))
)
Write-Host "构建时间戳: $stamp" -ForegroundColor DarkGray

& $py -m PyInstaller --noconfirm --clean launcher.spec
if ($LASTEXITCODE -ne 0) { throw "打包失败，看上面的日志" }

$exe = Join-Path $script:RepoRoot 'dist\WorkflowPanel.exe'
if (-not (Test-Path $exe)) { throw "没找到产物：$exe" }

# 同时拷贝一份到仓库根：ghpush 默认忽略 dist/，根目录的 exe 才会随仓库提交
$rootExe = Join-Path $script:RepoRoot 'WorkflowPanel.exe'
Copy-Item $exe $rootExe -Force
$size = [math]::Round((Get-Item $rootExe).Length / 1MB, 1)

Write-Host "`n打包完成：$exe" -ForegroundColor Green
Write-Host "入口副本：$rootExe（$size MB，随仓库提交）" -ForegroundColor Green
Write-Host "  双击 WorkflowPanel.exe → 打开聊天面板（面板内 /version 可看构建时间）" -ForegroundColor Yellow
Write-Host "  命令行 → & '.\WorkflowPanel.exe' --version" -ForegroundColor Yellow
Write-Host "          & '.\WorkflowPanel.exe' --cli run --task ttl_cache --mock" -ForegroundColor Yellow
