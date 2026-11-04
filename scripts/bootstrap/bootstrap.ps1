#Requires -Version 5.1
<#
.SYNOPSIS
    dev-env-bootstrap: Cross-platform dev environment setup tool
    PowerShell entry point for Windows-native use

.DESCRIPTION
    This PowerShell script delegates most logic to bootstrap.sh (if git bash is present)
    or implements native Windows logic where needed. It ensures proper error handling
    and provides a consistent interface for PowerShell users.

.PARAMETER Subcommand
    The action to perform: check, install, keys, clone, all

.PARAMETER Config
    Path to config file (default: ./bootstrap.config.json)

.PARAMETER DryRun
    Print intended actions without making changes

.PARAMETER Yes
    Skip confirmation prompts

.PARAMETER CloneRoot
    Override clone root directory

.PARAMETER Only
    Only process specified account

.EXAMPLE
    .\bootstrap.ps1 check

.EXAMPLE
    .\bootstrap.ps1 all -DryRun -Verbose
#>

[CmdletBinding()]
param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet('check', 'install', 'keys', 'clone', 'all')]
    [string]$Subcommand,

    [string]$Config = "",
    [switch]$DryRun,
    [switch]$Yes,
    [string]$CloneRoot = "",
    [string]$Only = ""
)

$ErrorActionPreference = 'Stop'
$Version = "1.0.0"

# ANSI colors for Windows
$script:ColorReset = "`e[0m"
$script:ColorRed = "`e[31m"
$script:ColorGreen = "`e[32m"
$script:ColorYellow = "`e[33m"
$script:ColorBlue = "`e[34m"

function Write-Info {
    param([string]$Message)
    Write-Host "${script:ColorBlue}[INFO]${script:ColorReset} $Message"
}

function Write-Success {
    param([string]$Message)
    Write-Host "${script:ColorGreen}[OK]${script:ColorReset} $Message"
}

function Write-Warning {
    param([string]$Message)
    Write-Host "${script:ColorYellow}[WARN]${script:ColorReset} $Message"
}

function Write-Error {
    param([string]$Message)
    Write-Host "${script:ColorRed}[ERROR]${script:ColorReset} $Message" -ForegroundColor Red
}

function Write-VerboseCustom {
    param([string]$Message)
    if ($VerbosePreference -eq 'Continue') {
        Write-Host "${script:ColorBlue}[VERBOSE]${script:ColorReset} $Message"
    }
}

# Check if git bash is available
function Test-GitBash {
    $gitBashPaths = @(
        "C:\Program Files\Git\bin\bash.exe",
        "C:\Program Files\Git\usr\bin\bash.exe"
    )

    foreach ($path in $gitBashPaths) {
        if (Test-Path $path) {
            return $path
        }
    }

    return $null
}

# Delegate to bash implementation if available
function Invoke-BashBootstrap {
    param(
        [string]$BashPath,
        [string]$Subcommand,
        [hashtable]$Options
    )

    $scriptDir = Split-Path -Parent $PSCommandPath
    $bashScript = Join-Path $scriptDir "bootstrap.sh"

    if (-not (Test-Path $bashScript)) {
        Write-Error "bootstrap.sh not found at: $bashScript"
        exit 1
    }

    # Build arguments
    $args = @($Subcommand)

    if ($Options.Config) {
        $args += @("--config", $Options.Config)
    }
    if ($Options.DryRun) {
        $args += "--dry-run"
    }
    if ($Options.Yes) {
        $args += "--yes"
    }
    if ($Options.Verbose) {
        $args += "--verbose"
    }
    if ($Options.CloneRoot) {
        $args += @("--clone-root", $Options.CloneRoot)
    }
    if ($Options.Only) {
        $args += @("--only", $Options.Only)
    }

    Write-VerboseCustom "Delegating to bash: $BashPath $bashScript $($args -join ' ')"

    # Execute bash script
    & $BashPath $bashScript @args

    if ($LASTEXITCODE -ne 0) {
        Write-Error "bash script exited with code: $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

# Native PowerShell implementation (fallback)
function Invoke-NativeBootstrap {
    param(
        [string]$Subcommand,
        [hashtable]$Options
    )

    Write-Warning "Git bash not found. Using limited native PowerShell implementation."
    Write-Warning "For full functionality, install Git for Windows first."

    # Minimal native implementation
    switch ($Subcommand) {
        'check' {
            Write-Info "Checking dev environment..."
            Write-Info ""

            # Check git
            if (Get-Command git -ErrorAction SilentlyContinue) {
                $gitVer = (git --version) -replace 'git version ', ''
                Write-Success "git $gitVer"
            } else {
                Write-Error "git NOT INSTALLED"
            }

            # Check gh
            if (Get-Command gh -ErrorAction SilentlyContinue) {
                $ghVer = (gh --version | Select-Object -First 1) -replace 'gh version ', ''
                Write-Success "gh $ghVer"
            } else {
                Write-Error "gh NOT INSTALLED"
            }

            # Check aws
            if (Get-Command aws -ErrorAction SilentlyContinue) {
                $awsVer = (aws --version 2>&1) -replace 'aws-cli/', '' -replace ' .*', ''
                Write-Success "aws-cli $awsVer"
            } else {
                Write-Error "aws-cli NOT INSTALLED"
            }

            Write-Info ""
            Write-Warning "For full functionality including SSH keys and repo cloning, install Git bash."
        }

        'install' {
            Write-Info "Installing missing tools..."

            if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
                Write-Error "winget not found. Please install App Installer from Microsoft Store."
                exit 1
            }

            # Install git
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                Write-Info "Installing Git..."
                if (-not $DryRun) {
                    winget install --id Git.Git --silent --accept-package-agreements --accept-source-agreements
                    Write-Success "Installed Git"
                } else {
                    Write-Info "[DRY RUN] Would install Git"
                }
            }

            # Install gh
            if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
                Write-Info "Installing GitHub CLI..."
                if (-not $DryRun) {
                    winget install --id GitHub.cli --silent --accept-package-agreements --accept-source-agreements
                    Write-Success "Installed GitHub CLI"
                } else {
                    Write-Info "[DRY RUN] Would install GitHub CLI"
                }
            }

            # Install aws
            if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
                Write-Info "Installing AWS CLI..."
                if (-not $DryRun) {
                    winget install --id Amazon.AWSCLI --silent --accept-package-agreements --accept-source-agreements
                    Write-Success "Installed AWS CLI"
                } else {
                    Write-Info "[DRY RUN] Would install AWS CLI"
                }
            }

            Write-Warning "Tools installed. You may need to:"
            Write-Warning "1. Open a new terminal for PATH changes to take effect"
            Write-Warning "2. Fully restart VS Code if it's running"
        }

        default {
            Write-Error "Subcommand '$Subcommand' requires Git bash. Please install Git for Windows."
            exit 1
        }
    }
}

# Main execution
try {
    Write-VerboseCustom "dev-env-bootstrap v$Version (PowerShell)"
    Write-VerboseCustom "OS: Windows, PowerShell $($PSVersionTable.PSVersion)"

    # Build options hashtable
    $options = @{
        Config = $Config
        DryRun = $DryRun.IsPresent
        Yes = $Yes.IsPresent
        Verbose = ($VerbosePreference -eq 'Continue')
        CloneRoot = $CloneRoot
        Only = $Only
    }

    # Check for git bash and delegate if available
    $bashPath = Test-GitBash

    if ($bashPath) {
        Write-VerboseCustom "Found git bash at: $bashPath"
        Invoke-BashBootstrap -BashPath $bashPath -Subcommand $Subcommand -Options $options
    } else {
        # Fall back to native implementation (limited functionality)
        Invoke-NativeBootstrap -Subcommand $Subcommand -Options $options
    }

    Write-VerboseCustom "PowerShell wrapper completed successfully"
}
catch {
    Write-Error "PowerShell script failed: $_"
    Write-Error $_.Exception.StackTrace
    exit 1
}
