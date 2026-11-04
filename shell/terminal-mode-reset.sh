# shellcheck shell=sh
# ISSUE-REF — clear leaked terminal tracking modes at shell init.
#
# A TUI that enables mouse reporting, focus reporting or bracketed paste and dies
# without disabling it leaves the TERMINAL enabled. The terminal then TYPES its
# reports at whatever reads stdin next, so the next program's input fills with
# garbage bytes. Claude Code does not always disable its modes on exit
# (anthropics/claude-code#1509, open). Nothing in this fleet enables them — a full
# sweep of every hook, script and generated shell surface found zero terminal
# queries and zero mode writes — so clearing a leak is the only available remedy.
#
# WHY SHELL INIT IS THE RIGHT MOMENT: at an interactive prompt no TUI owns the
# terminal, so any tracking mode still enabled is by definition leaked. Clearing
# it here cannot fight a live TUI, and a Claude Code started later in the same
# terminal asserts whatever modes it needs.
#
# WHY THE FULL SET IS SAFE HERE AND ONLY HERE: ?1000/?1006/?1004 are modes Claude
# Code itself uses, and disabling them under a LIVE session destroys
# click-to-position and copy-on-select (ISSUE-REF, ISSUE-REF). The third guard
# below is what makes emitting them correct: with no Claude Code marker in the
# environment there is no live session whose modes this could disarm. Never reuse
# this sequence anywhere that runs while Claude Code is live (a hook, the
# statusline, a session launch path) — and note that per ISSUE-REF a live session
# re-asserts a cached mouse level it never re-derives, so such a write is undone
# anyway.
_ai_harness_reset_terminal_modes() {
    # 1. Interactive shells only. In a script, a pipeline or `sh -c`, these bytes
    #    would land in the program's own output.
    case $- in
        *i*) ;;
        *) return 0 ;;
    esac

    # 2. Stdout must be the terminal itself. An interactive shell whose stdout is
    #    redirected would otherwise write escapes into a file or a CI log.
    [ -t 1 ] || return 0

    # 3. No live Claude Code session. These markers are present in the
    #    environment of every process Claude Code spawns (verified by name-only
    #    probe on a live session, 2026-08-22), so their presence means this shell
    #    is nested INSIDE a session whose mouse modes must not be disarmed.
    if [ -n "${CLAUDECODE:-}" ] ||
        [ -n "${CLAUDE_CODE_SESSION_ID:-}" ] ||
        [ -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]; then
        return 0
    fi

    # Mouse tracking (?1000 normal, ?1002 button-motion, ?1003 any-motion) and its
    # three encodings (?1005 UTF-8, ?1006 SGR, ?1015 urxvt), then focus reporting
    # (?1004) and bracketed paste (?2004). ?25h re-shows the cursor, which a
    # crashed TUI leaves hidden. Octal \033 rather than \e: \e is not POSIX
    # printf, and this file is sourced by both bash and zsh.
    printf '\033[?1000l\033[?1002l\033[?1003l\033[?1005l\033[?1006l\033[?1015l\033[?1004l\033[?2004l\033[?25h'
}

_ai_harness_reset_terminal_modes
unset -f _ai_harness_reset_terminal_modes
