#!/usr/bin/env bash
# Generic shell-startup measurement or prompt helper.
set -u

B=${BENCH_DIR:-$HOME/.cache/shell-startup-perf}
STRACE=/usr/bin/strace.exe
BASH=/usr/bin/bash
REPS=${REPS:-7}
OUT=${OUT:-$B/results}
mkdir -p "$OUT" "$B/arms"

# ---- Build ablation arms -------------------------------------------------
# Each arm isolates one change so its individual contribution is attributable.
build_arms() {
    local a
    rm -rf "$B/arms"; mkdir -p "$B/arms"

    # A: stock -- original dotfiles, stock heavy git prompt
    a=$B/arms/A_stock; mkdir -p "$a"
    cp ~/.shell-perf-backup/.bashrc.bak       "$a/.bashrc"
    cp ~/.shell-perf-backup/.bash_profile.bak "$a/.bash_profile"
    cp ~/.shell-perf-backup/.profile.bak      "$a/.profile"

    # B: stock prompt, but .bashrc double-source fixed
    a=$B/arms/B_nodouble; mkdir -p "$a"
    cp ~/.shell-perf-backup/.bashrc.bak       "$a/.bashrc"
    cp ~/.shell-perf-backup/.bash_profile.bak "$a/.bash_profile"
    printf '# no-op: does not source .bashrc (see research doc)\n' > "$a/.profile"

    # C: fast prompt only, original .bashrc and double-source still present
    a=$B/arms/C_fastprompt; mkdir -p "$a/.config/git"
    cp ~/.shell-perf-backup/.bashrc.bak       "$a/.bashrc"
    cp ~/.shell-perf-backup/.bash_profile.bak "$a/.bash_profile"
    cp ~/.shell-perf-backup/.profile.bak      "$a/.profile"
    cp ~/.config/git/git-prompt.sh            "$a/.config/git/git-prompt.sh"

    # D: both fixes -- the shipped configuration
    a=$B/arms/D_both; mkdir -p "$a/.config/git"
    cp ~/.bashrc       "$a/.bashrc"
    cp ~/.bash_profile "$a/.bash_profile"
    cp ~/.profile      "$a/.profile"
    cp ~/.config/git/git-prompt.sh "$a/.config/git/git-prompt.sh"
}

# ---- Measure -------------------------------------------------------------
events_for() {  # $1 = arm dir, or "floor"
    local tmp=/tmp/sc.$$.txt
    if [ "$1" = "floor" ]; then
        "$STRACE" -o "$tmp" -f "$BASH" --noprofile --norc -c exit >/dev/null 2>&1
    else
        HOME="$1" "$STRACE" -o "$tmp" -f "$BASH" --login -i -c exit >/dev/null 2>&1
    fi
    wc -l < "$tmp"
    rm -f "$tmp"
}

build_arms

: > "$OUT/syscount.csv"
echo "arm,rep,events" >> "$OUT/syscount.csv"

declare -A LABEL=(
    [floor]="floor (no config)"
    [A_stock]="A: stock (baseline)"
    [B_nodouble]="B: + single-source .bashrc"
    [C_fastprompt]="C: + fast git prompt"
    [D_both]="D: both fixes (shipped)"
)

for arm in floor A_stock B_nodouble C_fastprompt D_both; do
    for ((r = 1; r <= REPS; r++)); do
        if [ "$arm" = "floor" ]; then n=$(events_for floor)
        else n=$(events_for "$B/arms/$arm"); fi
        echo "$arm,$r,$n" >> "$OUT/syscount.csv"
    done
    printf '%-28s measured\n' "${LABEL[$arm]}"
done

echo
echo "=== startup work (strace event volume; lower is better) ==="
awk -F, -v OFS='' 'NR>1 { n[$1]++; v[$1","n[$1]]=$3; s[$1]+=$3 }
END {
    split("floor A_stock B_nodouble C_fastprompt D_both", order, " ")
    for (k=1;k<=5;k++) {
        a=order[k]; c=n[a]
        for (i=1;i<=c;i++) x[i]=v[a","i]
        for (i=1;i<c;i++) for (j=1;j<=c-i;j++) if (x[j]>x[j+1]) {t=x[j];x[j]=x[j+1];x[j+1]=t}
        m[a] = (c%2 ? x[(c+1)/2] : (x[c/2]+x[c/2+1])/2)
        spread[a] = 100.0*(x[c]-x[1])/m[a]
        printf "%-14s median %7d events   spread %.1f%%\n", a, m[a], spread[a]
    }
    print ""
    base = m["A_stock"] - m["floor"]
    printf "config work above floor:\n"
    for (k=2;k<=5;k++) {
        a=order[k]; w = m[a]-m["floor"]
        printf "  %-14s %7d events", a, w
        if (a != "A_stock" && w > 0) printf "   (%.2fx less than stock, -%.0f%%)", base/w, 100*(1-w/base)
        printf "\n"
    }
}' "$OUT/syscount.csv"
