#!/usr/bin/env bash
# Controlled A/B benchmark of Git Bash startup cost.
#
# Methodology notes (see research doc for citations):
#  - hyperfine performs its own warmup, repeated runs and outlier detection.
#  - We run with --shell=none so hyperfine does not add a wrapper shell whose
#    spawn cost (very high on this machine) would dominate and be subtracted
#    imprecisely. We are measuring a shell spawn, so the shell IS the workload.
#  - The two arms differ ONLY by $HOME, which selects the dotfile set. Same
#    binary, same cwd, same environment otherwise.
#  - Arms are benchmarked in the same hyperfine invocation so they interleave
#    in time; a load spike therefore hits both arms rather than penalising one.
set -u

B=${BENCH_DIR:-$HOME/.cache/shell-startup-perf}
HF=${HYPERFINE:-$(command -v hyperfine || cat /tmp/hfpath 2>/dev/null)}
BASH_EXE=${BASH_EXE:-$(cygpath -w /usr/bin/bash 2>/dev/null || echo /usr/bin/bash)}
RUNS=${RUNS:-20}
WARMUP=${WARMUP:-3}
OUT=${OUT:-$B/results}
mkdir -p "$OUT"

# Record machine state around the measurement so a result can be judged in
# context rather than assumed to be from a quiet machine.
snapshot_load() {
    powershell.exe -NoProfile -Command "
        \$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        \$top = Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 |
            ForEach-Object { \"\$(\$_.ProcessName)=\$([int]\$_.CPU)s\" }
        [pscustomobject]@{ cpu_pct = \$cpu; top = (\$top -join ', ') } | ConvertTo-Json -Compress
    " 2>/dev/null
}

echo "=== machine load BEFORE benchmark ==="
snapshot_load | tee "$OUT/load_pre.json"
echo

# --login -i is exactly how VS Code launches the profile (per settings.json args).
echo "=== hyperfine: interleaved A/B, runs=$RUNS warmup=$WARMUP ==="
HOME_BEFORE="$B/home_before" HOME_AFTER="$B/home_after" \
"$HF" \
    --shell=none \
    --warmup "$WARMUP" \
    --runs "$RUNS" \
    --style full \
    --time-unit millisecond \
    --export-json "$OUT/startup.json" \
    --export-markdown "$OUT/startup.md" \
    --command-name "before (stock prompt, .bashrc x2)" \
    --setup "true" \
    "env HOME=$B/home_before $BASH_EXE --login -i -c exit" \
    --command-name "after (fast prompt, single source)" \
    "env HOME=$B/home_after $BASH_EXE --login -i -c exit"

echo
echo "=== machine load AFTER benchmark ==="
snapshot_load | tee "$OUT/load_post.json"

echo
echo "=== floor reference: bash with no config at all ==="
"$HF" --shell=none --warmup 2 --runs "$RUNS" --style basic --time-unit millisecond \
    --export-json "$OUT/floor.json" \
    "$BASH_EXE --noprofile --norc -c exit"
