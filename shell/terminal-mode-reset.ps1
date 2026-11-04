# ISSUE-REF — clear leaked terminal tracking modes at shell init.
#
# PowerShell counterpart of terminal-mode-reset.sh; that file carries the full
# rationale, including why the complete mode set is correct at shell init and
# nowhere else.
function Reset-AiHarnessTerminalModes {
    # 1. No live Claude Code session. Every process Claude Code spawns inherits
    #    these markers, so their presence means this host is nested INSIDE a
    #    session whose mouse modes must not be disarmed (ISSUE-REF, ISSUE-REF).
    if ($env:CLAUDECODE -or $env:CLAUDE_CODE_SESSION_ID -or $env:CLAUDE_CODE_ENTRYPOINT) {
        return
    }

    # 2. Interactive session only. A service or scheduled-task host has no user
    #    attached, and a profile IS loaded for `powershell.exe -File script.ps1`,
    #    so this is the guard that keeps escapes out of a script's own run.
    if (-not [Environment]::UserInteractive) {
        return
    }

    # 3. A real console, and stdout not redirected — the PowerShell equivalent of
    #    `[ -t 1 ]`. ConsoleHost excludes the ISE and every embedded host that has
    #    no terminal to reset; IsOutputRedirected excludes `... > out.txt` and
    #    `... | more`, where the bytes would corrupt captured output.
    if ($Host.Name -ne 'ConsoleHost' -or $null -eq $Host.UI.RawUI) {
        return
    }
    if ([Console]::IsOutputRedirected) {
        return
    }

    # [char]27, not the backtick-e escape literal: that literal is PowerShell 7+
    # only and this profile snippet has to run under Windows PowerShell 5.1 too.
    $esc = [char]27
    [Console]::Write(
        "$esc[?1000l$esc[?1002l$esc[?1003l$esc[?1005l$esc[?1006l$esc[?1015l$esc[?1004l$esc[?2004l$esc[?25h"
    )
}

Reset-AiHarnessTerminalModes
Remove-Item -Path 'Function:\Reset-AiHarnessTerminalModes' -ErrorAction SilentlyContinue
