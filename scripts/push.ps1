# 上传到 GitHub（包装外部固定工具 ghpush，仓库内不出现任何绝对路径）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\push.ps1 -Message "feat: xxx"
#      加 -DryRun 只预览；-Repo 不传则用工具 .env 里的 DEFAULT_REPO
param(
    [string]$Message,
    [string]$Repo,
    [string]$Dir,
    [string]$Branch,
    [switch]$DryRun,
    [switch]$DeleteMissing,
    [string]$GhPush,
    [string]$Python
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_env.ps1"

$py = Resolve-Python $Python
$gh = Resolve-GhPush $GhPush
$target = if ($Dir) { $Dir } else { $script:RepoRoot }

Write-Host "仓库根  : $script:RepoRoot" -ForegroundColor DarkGray
Write-Host "上传工具: $gh" -ForegroundColor DarkGray

$cliArgs = @($gh, "--dir", $target)
if ($Repo) { $cliArgs += @("--repo", $Repo) }
if ($Message) { $cliArgs += @("--message", $Message) }
if ($Branch) { $cliArgs += @("--branch", $Branch) }
if ($DryRun) { $cliArgs += "--dry-run" }
if ($DeleteMissing) { $cliArgs += "--delete-missing" }

& $py @cliArgs
exit $LASTEXITCODE
