# 离线跑一遍完整工作流（不需要 API Key）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo.ps1
#      可加 -Python <解释器> 覆盖自动探测
param([string]$Python)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_env.ps1"

$py = Resolve-Python $Python
Set-Location $script:RepoRoot
$env:PYTHONIOENCODING = "utf-8"

Write-Host "仓库根: $script:RepoRoot" -ForegroundColor DarkGray
Write-Host "解释器: $py" -ForegroundColor DarkGray

Write-Host "`n== 1/2 先单独验收参考实现 ==" -ForegroundColor Cyan
& $py -m workflow.cli verify --task ttl_cache
if ($LASTEXITCODE -ne 0) { Write-Host "参考实现验收失败，终止" -ForegroundColor Red; exit 1 }

Write-Host "`n== 2/2 跑三 Agent 工作流（mock provider）==" -ForegroundColor Cyan
& $py -m workflow.cli run --task ttl_cache --mock
$runExit = $LASTEXITCODE

Write-Host "`n查询方式：" -ForegroundColor Yellow
Write-Host "  $py -m workflow.cli runs"
Write-Host "  Get-Content (Get-ChildItem runs | Sort-Object LastWriteTime -Descending)[0].FullName\REPORT.md"
exit $runExit
