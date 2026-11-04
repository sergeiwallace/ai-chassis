# =============================================================================
# reap-orphan-shells.ps1 - reap leaked Git Bash login shells on Windows (ISSUE-REF).
#
# THE PROBLEM
# Every Claude Code tool call, hook and statusline render spawns Git Bash. When the parent
# exits without reaping, the shell survives as an idle orphan. Measured on enterprise-windows-managed-platform
# on 2026-08-04: 293 bash processes, 281 of them orphaned with dead parents, the oldest TWENTY
# HOURS old, and 285 of them plain login shells with no -c command (so not runaway work --
# one leaked shell per dead parent).
#
# THE BIGGER HALF, found 2026-08-04 (ISSUE-REF)
# The shells are not the whole leak, or even most of it. 201 MSYS coreutils helpers were also
# stuck (jq, cksum, cat, date, dirname, mkdir, tr, cut, timeout) against 92 bash, with 201
# distinct dead parents and EXACTLY ONE child per parent -- the signature of one abandoned
# child per abandoned parent, not of runaway work. 282 of 287 had accumulated zero CPU over
# lifetimes up to 26 hours, so they were blocked rather than working, and together they held
# 572 MB.
#
# Mechanism: CC KILLS a statusline render that outlasts the interval to the next one, and the
# helper that is mid-flight at that instant is left with no living parent. Windows has no
# reparent-to-init that would reap it, so it simply stays. It is a RACE -- only the one helper
# running at the moment of the kill is stranded -- which is why it accumulates slowly enough
# to be invisible in per-render profiling and still reaches hundreds within a day.
#
# Established by varying ONLY how the render ends:
#   allowed to FINISH  ->  -4 helpers over 8 renders  (no leak)
#   SIGKILLed at 0.3s  -> +12 helpers over 20 renders (~0.6 per render)
# An earlier hypothesis blamed the child inheriting CC's unread stdout pipe; that was REFUTED
# by A/B (redirecting the render's stdout to a file leaked slightly MORE: +23 vs +19 over 25).
#
# It cannot be fixed inside the statusline: an EXIT trap does not run on SIGKILL, and killing
# the process group risks taking CC itself down (the render shares CC's group). So reaping is
# the mitigation, which is why $Names covers the helpers and not just the shells.
#
# Individually cheap at ~0.75 MB, but they hold PIDs and handles indefinitely, and they are the
# same class of process churn that exhausted the SageMaker Space and killed its CodeEditor app.
#
# WHY A REAPER IS THE RIGHT TOOL HERE, unlike on the Space
# On the SageMaker Space the problem was BIRTH RATE (50 fork+exec/sec) and a reaper would not
# have helped -- supervisord was already reaping cleanly. Here the problem is the opposite:
# accumulation of processes that never exit. That is precisely what a reaper fixes.
#
# SAFETY - three independent conditions, ALL required before killing anything:
#   1. The parent process is GONE. A live parent means the shell may still be in use.
#   2. The shell has NO CHILDREN. Children mean real work in flight.
#   3. The shell is OLDER than -MinAgeMinutes (default 30). Guards against a race with a
#      just-spawned shell whose parent has not been observed yet.
# Anything failing any condition is left alone. -WhatIf reports without killing.
#
# USAGE
#   pwsh -File reap-orphan-shells.ps1 [-WhatIf] [-MinAgeMinutes 30] [-Quiet]
#
# Idempotent and safe to run repeatedly.
# =============================================================================
[CmdletBinding()]
param(
  [switch]$WhatIf,
  [int]$MinAgeMinutes = 30,
  [switch]$Quiet,
  # Process names to consider.
  #
  # bash/sh are the leaked login shells this script was written for.
  #
  # The rest are MSYS coreutils helpers, added 2026-08-04 (ISSUE-REF) after measuring that they
  # are the LARGER half of the leak and were never being reaped: 201 live helpers against 92
  # bash, 201 distinct dead parents, exactly one child per parent, oldest 26 hours, 572 MB
  # held. Every one traced to a statusline render that Claude Code abandoned mid-flight,
  # leaving the helper blocked writing to a stdout pipe nobody will read again.
  #
  # These are safe to include precisely BECAUSE they are ephemeral: date, mkdir, dirname, tr,
  # cut, cksum and basename cannot legitimately run for the 30-minute floor, so any instance
  # passing all three conditions is definitionally stuck. cat/head/tail/grep/sed/jq CAN run
  # long on a real pipeline, but the no-children plus dead-parent plus age conditions still
  # protect them. `timeout` is included because it is the wrapper the statusline uses.
  #
  # Deliberately NOT included: git.exe, ssh.exe, python.exe, node.exe and anything else that
  # legitimately runs long and detached. This list stays limited to shells and small
  # short-lived text utilities.
  [string[]]$Names = @(
    'bash.exe', 'sh.exe',
    'date.exe', 'mkdir.exe', 'dirname.exe', 'basename.exe', 'tr.exe', 'cut.exe',
    'cksum.exe', 'timeout.exe', 'cat.exe', 'head.exe', 'tail.exe', 'wc.exe',
    'grep.exe', 'sed.exe', 'jq.exe', 'printf.exe', 'rm.exe'
  )
)

$ErrorActionPreference = 'Stop'
$logFile = "$env:USERPROFILE\.reap-orphan-shells.log"

function Write-ReapLog([string]$message) {
  $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  try { "$stamp  $message" | Add-Content -Path $logFile -ErrorAction SilentlyContinue } catch { }
  if (-not $Quiet) { Write-Output $message }
}

# Snapshot everything ONCE. Re-querying per candidate would race against normal churn and,
# on this machine, cost hundreds of WMI round-trips.
$allProcs = Get-CimInstance Win32_Process
$livePids = [System.Collections.Generic.HashSet[int]]::new()
foreach ($p in $allProcs) { [void]$livePids.Add([int]$p.ProcessId) }

# Set of PIDs that are somebody's parent, so "has children" is an O(1) lookup.
$parentPids = [System.Collections.Generic.HashSet[int]]::new()
foreach ($p in $allProcs) {
  if ($null -ne $p.ParentProcessId) { [void]$parentPids.Add([int]$p.ParentProcessId) }
}

$now = Get-Date
$candidates = @($allProcs | Where-Object { $Names -contains $_.Name })

$orphans = @()
foreach ($c in $candidates) {
  $ppid = [int]$c.ParentProcessId
  $cpid = [int]$c.ProcessId

  # Condition 1: parent must be gone.
  if ($livePids.Contains($ppid)) { continue }
  # Condition 2: must not itself be a parent.
  if ($parentPids.Contains($cpid)) { continue }
  # Condition 3: must be older than the floor. A null CreationDate is treated as too new,
  # i.e. skipped -- never guess an age in order to justify a kill.
  if ($null -eq $c.CreationDate) { continue }
  $ageMin = ($now - $c.CreationDate).TotalMinutes
  if ($ageMin -lt $MinAgeMinutes) { continue }

  $orphans += [pscustomobject]@{ Pid = $cpid; Ppid = $ppid; AgeMin = [int]$ageMin; Name = $c.Name }
}

if ($orphans.Count -eq 0) {
  Write-ReapLog "nothing to reap ($($candidates.Count) shells, none orphaned+idle+older than ${MinAgeMinutes}m)"
  exit 0
}

$oldest = ($orphans | Measure-Object AgeMin -Maximum).Maximum
Write-ReapLog ("found {0} orphan(s) of {1} shell(s); oldest {2} min" -f $orphans.Count, $candidates.Count, $oldest)

if ($WhatIf) {
  $orphans | Sort-Object AgeMin -Descending | Select-Object -First 15 |
    Format-Table -AutoSize | Out-String | ForEach-Object { Write-Output $_ }
  Write-ReapLog "WhatIf: nothing killed"
  exit 0
}

$killed = 0
$failed = 0
foreach ($o in $orphans) {
  try {
    Stop-Process -Id $o.Pid -Force -ErrorAction Stop
    $killed++
  } catch {
    # A process that exited between the snapshot and now is the expected benign case, and
    # an access denial on a protected process is not worth failing the whole run over.
    $failed++
  }
}
Write-ReapLog "reaped $killed orphan shell(s), $failed could not be killed"
exit 0
