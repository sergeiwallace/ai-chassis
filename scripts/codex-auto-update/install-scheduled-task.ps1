[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$runner = Join-Path $repository "scripts\codex-auto-update\codex_auto_update.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Codex auto-update runner is missing: $runner"
}
$python = (Get-Command python.exe -ErrorAction Stop).Source
$stateDir = Join-Path $env:LOCALAPPDATA "ai-harness\codex-auto-update"
$launcher = Join-Path $stateDir "run.cmd"
$taskName = "ai-harness-codex-auto-update"
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
Set-Content -LiteralPath $launcher -Encoding ascii -NoNewline -Value (
    "@echo off`r`n`"$python`" `"$runner`" >> `"$stateDir\scheduled-task.log`" 2>&1`r`n"
)
$action = New-ScheduledTaskAction -Execute $launcher
$trigger = New-ScheduledTaskTrigger -Daily -At 4:00AM
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "Installed and enabled Scheduled Task $taskName (daily, 4:00 AM)."
