# Install ai-chassis from a release tag of its repository (Windows PowerShell).
#
# The command line tool is installed with uv in editable mode from a checkout, because
# several commands read the scripts, configuration and templates that sit beside the package.
# Run the script again to update: it fetches the tags and moves the checkout to the new one.
#
# This script follows install.sh step for step. It is less tested than the shell script:
# the shell script has an automated test suite, this one has not been run on Windows yet.
[CmdletBinding()]
param(
    # Tag or branch to install. Default: the newest stable release tag.
    [string]$Ref = $env:AI_CHASSIS_REF,
    # Where the checkout lives.
    [string]$Dir = $(if ($env:AI_CHASSIS_DIR) { $env:AI_CHASSIS_DIR } else { Join-Path $env:LOCALAPPDATA "ai-chassis" }),
    # Where uv places the ai-chassis command. Default: uv's own choice.
    [string]$BinDir = $env:AI_CHASSIS_BIN_DIR,
    # Configuration directory for the initial config file. Default: the tool's own.
    [string]$ConfigDir = $env:AI_CHASSIS_CONFIG_DIR,
    # Repository to clone.
    [string]$Repo = $(if ($env:AI_CHASSIS_REPO) { $env:AI_CHASSIS_REPO } else { "https://github.com/sergeiwallace/ai-chassis.git" }),
    # Copy the files in this directory into <checkout>\config when they are not there yet.
    [string]$Templates = $env:AI_CHASSIS_TEMPLATES,
    # Install uv with its official installer when it is missing.
    [switch]$InstallUv = ($env:AI_CHASSIS_INSTALL_UV -eq "1"),
    # Do not create the initial configuration file.
    [switch]$NoInit,
    # Print what would run and change nothing.
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param([string]$Command, [string[]]$Arguments)
    Write-Host ("+ " + $Command + " " + ($Arguments -join " "))
    if (-not $DryRun) {
        & $Command @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE" }
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required and was not found"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if ($InstallUv) {
        Write-Host "+ irm https://astral.sh/uv/install.ps1 | iex"
        if (-not $DryRun) {
            Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
            $env:Path = "$HOME\.local\bin;$env:Path"
        }
    } else {
        throw "uv is required and was not found. Install it from https://docs.astral.sh/uv/ or run this script again with -InstallUv"
    }
}

if (-not $Ref) {
    $listing = git ls-remote --tags --refs --sort=-v:refname $Repo "v*"
    if ($LASTEXITCODE -ne 0) { throw "could not list the tags of $Repo" }
    $Ref = $listing |
        ForEach-Object { $_ -replace '.*refs/tags/', '' } |
        Where-Object { $_ -match '^v\d+\.\d+\.\d+$' } |
        Select-Object -First 1
    if (-not $Ref) { throw "no release tag found in $Repo; pass -Ref to choose a branch or tag" }
}

Write-Host "Installing ai-chassis $Ref from $Repo into $Dir"

if (Test-Path (Join-Path $Dir ".git")) {
    # The tags were listed from $Repo, so the checkout must come from the same place.
    $existingOrigin = (git -C $Dir remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0) { $existingOrigin = "" }
    if ($existingOrigin -ne $Repo) {
        throw "$Dir is a checkout of $(if ($existingOrigin) { $existingOrigin } else { 'an unknown repository' }), not $Repo; pass another -Dir or remove it"
    }
    Invoke-Step git @("-C", $Dir, "fetch", "--tags", "--force", "--quiet", "origin")
} else {
    Invoke-Step git @("clone", "--quiet", $Repo, $Dir)
}
Invoke-Step git @("-C", $Dir, "checkout", "--quiet", $Ref)
if (-not $DryRun) {
    # A branch moves, so bring it level with its remote; a tag has nothing to pull.
    git -C $Dir symbolic-ref --quiet HEAD *> $null
    if ($LASTEXITCODE -eq 0) {
        Invoke-Step git @("-C", $Dir, "merge", "--ff-only", "--quiet", "origin/$Ref")
    }
}

if ($Templates) {
    if (-not (Test-Path $Templates -PathType Container)) { throw "templates directory not found: $Templates" }
    $configTarget = Join-Path $Dir "config"
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $configTarget | Out-Null }
    foreach ($file in Get-ChildItem -Path $Templates -File) {
        $target = Join-Path $configTarget $file.Name
        if (Test-Path $target) {
            Write-Host "keeping existing $target"
        } else {
            Write-Host "+ copy $($file.FullName) $target"
            if (-not $DryRun) { Copy-Item $file.FullName $target }
        }
    }
}

if ($BinDir) { $env:UV_TOOL_BIN_DIR = $BinDir }
Invoke-Step uv @("tool", "install", "--force", "--editable", $Dir)

if ($BinDir) {
    $toolBin = $BinDir
} elseif (-not $DryRun) {
    $toolBin = (uv tool dir --bin).Trim()
} else {
    $toolBin = "uv's tool bin directory"
}

if (-not $NoInit) {
    $configRoot = if ($ConfigDir) { $ConfigDir } else { Join-Path $env:APPDATA "ai-harness" }
    if (Test-Path (Join-Path $configRoot "ai-harness.toml")) {
        Write-Host "keeping existing configuration $(Join-Path $configRoot 'ai-harness.toml')"
    } elseif ($ConfigDir) {
        Invoke-Step (Join-Path $toolBin "ai-chassis") @("--config", $ConfigDir, "init")
    } else {
        Invoke-Step (Join-Path $toolBin "ai-chassis") @("init")
    }
}

Write-Host "Done. Make sure $toolBin is on your PATH."
