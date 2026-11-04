# Thin PowerShell bootstrap.  Mutating installation belongs to ai-harness apply.
$ErrorActionPreference = "Stop"
Write-Host "ai-harness native PowerShell support is experimental."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    try {
        irm https://astral.sh/uv/install.ps1 | iex
    } catch {
        Write-Error "uv installation failed. Install it manually with: irm https://astral.sh/uv/install.ps1 | iex"
        exit 1
    }
}
& uv tool run ai-harness apply @args
$applyExit = $LASTEXITCODE

# Native PowerShell bypasses install.sh, so provision the same per-user Codex
# updater Task Scheduler job here as well.
& "$PSScriptRoot\scripts\codex-auto-update\install-scheduled-task.ps1" -RepositoryRoot $PSScriptRoot
if ($applyExit -ne 0) { exit $applyExit }
