#!/usr/bin/env bash
# Install / verify everything the meeting-analysis scripts need.
#
# Idempotent: safe to run repeatedly. Every analysis script sources check_deps.sh,
# which calls this automatically when something is missing, so a fresh machine
# self-provisions instead of failing halfway through a 50-minute ASR run.
#
# Platform note: the original workflow was written on a SageMaker space where
# ffmpeg/tesseract came from apt and the Python env lived at $EFS/tools/mtg-venv.
# On a Windows host (Git Bash) there is no apt and no $EFS, so binaries come from
# winget and the venv lives under ~/.cache. Both paths are handled below.
set -uo pipefail

VENV_DIR="${MEETING_ANALYSIS_VENV:-$HOME/.cache/meeting-analysis-venv}"
STAMP="$VENV_DIR/.deps-ok"
# The Python package list deliberately lives in ONE place only: the import->package
# mapping in the heredoc below. A parallel shell array cannot express that mapping
# (faster-whisper imports as faster_whisper, pillow as PIL) and would drift from it.

log()  { printf '  %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- binaries ----
# Resolve a winget-installed exe even when its shim is not on PATH: winget adds
# the alias to a per-user dir that an already-running shell has not picked up.
find_winget_exe() {
    local name=$1
    local base="$LOCALAPPDATA/Microsoft/WinGet/Packages"
    [ -d "$base" ] || base="$HOME/AppData/Local/Microsoft/WinGet/Packages"
    [ -d "$base" ] || return 1
    find "$base" -iname "$name" -type f 2>/dev/null | head -1
}

ensure_binary() {  # $1=command  $2=winget id  $3=apt package
    local cmd=$1 wid=$2 apt=$3
    if have "$cmd"; then log "$cmd: present"; return 0; fi

    local found
    found=$(find_winget_exe "$cmd.exe" 2>/dev/null)
    if [ -n "$found" ]; then
        log "$cmd: installed but not on PATH -> $(dirname "$found")"
        PATH="$PATH:$(dirname "$found")"; export PATH
        return 0
    fi

    log "$cmd: MISSING -- installing"
    if have winget; then
        winget install --id "$wid" --accept-source-agreements \
            --accept-package-agreements --disable-interactivity >/dev/null 2>&1
    elif have apt-get; then
        sudo apt-get install -y "$apt" >/dev/null 2>&1
    else
        log "$cmd: no winget or apt available -- install manually"
        return 1
    fi

    found=$(find_winget_exe "$cmd.exe" 2>/dev/null)
    [ -n "$found" ] && { PATH="$PATH:$(dirname "$found")"; export PATH; }
    have "$cmd" || [ -n "$found" ] || { log "$cmd: install FAILED"; return 1; }
    log "$cmd: installed"
}

# ffmpeg is required (audio extraction + frame grids). tesseract is optional --
# the workflow reads frames natively because OCR missed most domain terms; it is
# kept only as a cross-check for long literal identifiers.
ensure_binary ffmpeg   Gyan.FFmpeg          ffmpeg   || FFMPEG_FAILED=1
ensure_binary pdftotext Xpdf.XpdfReader     poppler-utils || log "pdftotext: optional-ish, needed for the notes PDF"

# ------------------------------------------------------------------ python ----
PYTHON_BIN=""
for c in "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python"; do
    [ -x "$c" ] && { PYTHON_BIN=$c; break; }
done

if [ -z "$PYTHON_BIN" ]; then
    log "venv: creating at $VENV_DIR"
    if have uv; then
        uv venv "$VENV_DIR" --python 3.12 >/dev/null 2>&1
    else
        # uv is the mandated package manager fleet-wide; fall back only to bootstrap it.
        python -m venv "$VENV_DIR" >/dev/null 2>&1
    fi
    for c in "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python"; do
        [ -x "$c" ] && { PYTHON_BIN=$c; break; }
    done
fi

if [ -z "$PYTHON_BIN" ]; then
    log "venv: FAILED to create at $VENV_DIR"
    exit 1
fi
log "venv: $PYTHON_BIN"

# Check imports rather than `pip list`: an import is the thing the scripts
# actually do, and a present-but-broken wheel passes a list check.
missing=$("$PYTHON_BIN" - <<'PY' 2>/dev/null
mods = {"faster_whisper": "faster-whisper", "PIL": "pillow",
        "imagehash": "imagehash", "numpy": "numpy"}
out = []
for mod, pkg in mods.items():
    try:
        __import__(mod)
    except Exception:
        out.append(pkg)
print(" ".join(out))
PY
)

if [ -n "${missing// /}" ]; then
    log "python packages missing:$missing -- installing"
    # uv ONLY (never pip install) per the repo's mandatory package-manager rule.
    if have uv; then
        uv pip install --python "$PYTHON_BIN" $missing >/dev/null 2>&1
    else
        log "uv not found -- cannot install (pip install is forbidden in this repo)"
        exit 1
    fi
else
    log "python packages: all present"
fi

# Re-verify after install; never stamp on an unverified state.
if ! "$PYTHON_BIN" -c "import faster_whisper, PIL, imagehash, numpy" >/dev/null 2>&1; then
    log "python packages: verification FAILED after install"
    exit 1
fi
log "python packages: verified"

if [ "${FFMPEG_FAILED:-0}" = "1" ] && ! have ffmpeg; then
    log ""
    log "WARNING: ffmpeg unavailable. ASR (step 4) and frame work (steps 5-6) cannot run;"
    log "         transcript + notes-PDF analysis still can."
fi

printf '%s\n' "deps verified" > "$STAMP"
log ""
log "Done. Activate with:  export PATH=\"\$PATH:$(dirname "${PYTHON_BIN}")\""
log "Or let each script auto-resolve it via check_deps.sh."
