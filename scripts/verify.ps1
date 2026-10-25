# 跑测试 + 静态验收
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify.ps1
param([string]$Python)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_env.ps1"

$py = Resolve-Python $Python
Set-Location $script:RepoRoot
$env:PYTHONIOENCODING = "utf-8"

Write-Host "== pytest ==" -ForegroundColor Cyan
& $py -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n== 静态验收（AST 检查 + 复跑测试）==" -ForegroundColor Cyan
& $py -m workflow.cli verify --task ttl_cache
exit $LASTEXITCODE
