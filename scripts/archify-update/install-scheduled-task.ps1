[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$runner = Join-Path $repository "scripts\archify-update\check_archify_update.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "archify-update-check runner is missing: $runner"
}
$python = (Get-Command python.exe -ErrorAction Stop).Source
$stateDir = Join-Path $env:LOCALAPPDATA "ai-harness\archify-update-check"
$launcher = Join-Path $stateDir "run.cmd"
$taskName = "ai-harness-archify-update-check"
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
Set-Content -LiteralPath $launcher -Encoding ascii -NoNewline -Value (
    "@echo off`r`n`"$python`" `"$runner`" >> `"$stateDir\scheduled-task.log`" 2>&1`r`n"
)
$action = New-ScheduledTaskAction -Execute $launcher
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 4:00AM
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "Installed and enabled Scheduled Task $taskName (weekly, Sunday 4:00 AM)."
