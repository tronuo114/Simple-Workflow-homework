# 跑测试 + 静态验收
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = if (Test-Path "D:\python\python.exe") { "D:\python\python.exe" } else { "python" }

Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

Write-Host "== pytest ==" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n== 静态验收（AST 检查 + 再跑一遍测试）==" -ForegroundColor Cyan
& $py -m workflow.cli verify --task ttl_cache
exit $LASTEXITCODE
