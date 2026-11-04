#!/usr/bin/env bash
# Build a durable tmux master on persistent storage, so install.sh Step 1g has
# something to provision from on a machine that has never had one (ISSUE-REF).
#
# WHY THIS EXISTS. Step 1g copies the newest build it finds under
# `<parent-of-PROJECTS_DIR>/tools/tmux-*/bin/tmux` into ~/.local/bin. That location is
# deliberate: ~/.local/bin is discarded when a cloud IDE space rebuilds $HOME, while
# persistent storage survives it. But NOTHING created that master. It was built by hand,
# once, on one machine -- so a fresh host had no master, Step 1g's documented no-op fired
# (correct for a package-manager machine), and the host silently sat below the transport
# floor with no resumable-session workflow possible. Measured on a new EC2 host
# 2026-09-09: shipped tmux 3.2a, no master, Step 1g rc=0, auto-compact impossible.
#
# This script is the regeneration step. It writes the MASTER, not ~/.local/bin, so
# Step 1g's existing copy path stays the single installer and survives a $HOME rebuild.
#
# NO PASSWORD IS EVER READ, PROMPTED FOR, OR PASSED. Build dependencies are installed
# only when `sudo -n` already works without one. Where it does not, this script reports
# the exact command to run and exits non-zero rather than blocking on a prompt: it is
# called from install.sh, which runs non-interactively from SessionStart hooks, SSM and
# CI, and a prompt there hangs forever or reads garbage from stdin. When a human runs the
# printed command themselves, sudo does its own prompting -- which is the right place for
# it, and keeps this repo free of any secret-bearing code path.

set -euo pipefail

# The transport floor, mirrored from MINIMUM_TMUX_VERSION in
# src/ai_harness/context_supervisor.py. Mirrored rather than parsed because this script
# must run before any Python environment is guaranteed; the two are pinned equal by
# tests/test_install_durable_tmux.py, so a divergence is a red test, not a silent drift.
TMUX_TRANSPORT_FLOOR="3.7"

# The source tarball to fall back to when the newest release cannot be looked up (no
# network, rate limit, API change). This is a BUILD SOURCE, not a gate: it is the version
# measured building clean in 6 s on Ubuntu 22.04 x86_64 on 2026-09-09. The lookup below
# is what implements "always the latest tmux" (the maintainer, 2026-09-06); this is only the
# floor-satisfying answer for when the lookup fails.
TMUX_FALLBACK_VERSION="3.7c"

TMUX_BUILD_DEPS=(
  build-essential pkg-config bison libevent-dev libncurses-dev automake autoconf
)

usage() {
  cat <<'USAGE'
usage: provision_durable_tmux.sh [--if-needed] [--tools-dir DIR] [--version VER] [--help]

Builds tmux from source into DIR/tmux-<version>/ so install.sh Step 1g can provision it.

  --if-needed     exit 0 without building when a master at or above the floor exists
  --tools-dir DIR where masters live (default: <parent-of-PROJECTS_DIR>/tools)
  --version VER   build this version instead of looking up the newest release
  --help          show this message

Never prompts for a password. Build dependencies are installed only if `sudo -n` works.
USAGE
}

log()  { printf '  %s\n' "$*"; }
warn() { printf '  %s\n' "$*" >&2; }

# 0 when $1 is a strictly newer tmux version than $2, comparing (major, minor)
# NUMERICALLY and only then the patch letter lexically. Same contract as install.sh's
# _tmux_version_gt, and for the same reason: "3.10" sorts BELOW "3.7" as a string while
# being the newer build, so a lexical compare would pick an older tmux the first time a
# minor reaches two digits.
_version_gt() {
  local a="${1#next-}" b="${2#next-}"
  local a_major a_minor a_suffix b_major b_minor b_suffix
  [[ "$a" == *.* && "$b" == *.* ]] || return 1
  a_major="${a%%.*}"; a_major="${a_major%%[^0-9]*}"
  b_major="${b%%.*}"; b_major="${b_major%%[^0-9]*}"
  a_minor="${a#*.}"; a_suffix="${a_minor#"${a_minor%%[^0-9]*}"}"; a_minor="${a_minor%%[^0-9]*}"
  b_minor="${b#*.}"; b_suffix="${b_minor#"${b_minor%%[^0-9]*}"}"; b_minor="${b_minor%%[^0-9]*}"
  [[ -n "$a_major" && -n "$a_minor" && -n "$b_major" && -n "$b_minor" ]] || return 1
  ((a_major > b_major)) && return 0
  ((a_major < b_major)) && return 1
  ((a_minor > b_minor)) && return 0
  ((a_minor < b_minor)) && return 1
  [[ "$a_suffix" > "$b_suffix" ]] && return 0
  return 1
}

# 0 when $1 is at or above the transport floor. An empty version is BELOW it: absent is
# not "good enough", and treating it as passing is how a machine with no tmux at all
# would have slipped through.
_meets_floor() {
  local v="${1:-}"
  [[ -n "$v" ]] || return 1
  [[ "$v" == "$TMUX_TRANSPORT_FLOOR" ]] && return 0
  _version_gt "$v" "$TMUX_TRANSPORT_FLOOR"
}

# Echo the newest version among the masters already under $1, or nothing.
_existing_master_version() {
  local tools_dir="$1" candidate candidate_version best="" had_nullglob=0
  shopt -q nullglob && had_nullglob=1
  shopt -s nullglob
  for candidate in "$tools_dir"/tmux-*/bin/tmux; do
    [[ -x "$candidate" ]] || continue
    # Ask the BINARY for its version rather than trusting the directory name: the
    # directory is a human-typed label and the binary is the thing that has to be new.
    candidate_version="$("$candidate" -V 2>/dev/null | awk '{print $2}')" || candidate_version=""
    [[ -n "$candidate_version" ]] || continue
    if [[ -z "$best" ]] || _version_gt "$candidate_version" "$best"; then
      best="$candidate_version"
    fi
  done
  ((had_nullglob)) || shopt -u nullglob
  printf '%s' "$best"
}

# Echo the newest published tmux version, or nothing when it cannot be determined.
_latest_released_version() {
  local body tag
  command -v curl >/dev/null 2>&1 || return 0
  body="$(curl -fsSL --max-time 20 \
    https://api.github.com/repos/tmux/tmux/releases/latest 2>/dev/null)" || return 0
  # Read the tag out of the JSON without requiring a JSON parser: this runs before any
  # Python environment is guaranteed.
  tag="$(printf '%s' "$body" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
  [[ -n "$tag" ]] || return 0
  printf '%s' "${tag#v}"
}

_missing_build_tools() {
  local missing=() tool
  for tool in curl make cc tar; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  printf '%s' "${missing[*]-}"
}

main() {
  local if_needed=0 tools_dir="" version="" existing missing tmp src prefix
  while (($#)); do
    case "$1" in
      --if-needed) if_needed=1 ;;
      --tools-dir) tools_dir="${2:-}"; shift ;;
      --version)   version="${2:-}"; shift ;;
      --help|-h)   usage; return 0 ;;
      *) warn "provision_durable_tmux: unknown argument: $1"; usage >&2; return 2 ;;
    esac
    shift
  done

  if [[ -z "$tools_dir" ]]; then
    if [[ -z "${PROJECTS_DIR:-}" ]]; then
      warn "provision_durable_tmux: pass --tools-dir, or export PROJECTS_DIR so the"
      warn "                        persistent tools directory can be derived from it."
      return 2
    fi
    tools_dir="${PROJECTS_DIR%/*}/tools"
  fi

  existing="$(_existing_master_version "$tools_dir")"
  if ((if_needed)) && _meets_floor "$existing"; then
    log "[ok]  durable tmux master already present: $existing (floor $TMUX_TRANSPORT_FLOOR)"
    return 0
  fi

  missing="$(_missing_build_tools)"
  if [[ -n "$missing" ]]; then
    warn "[warn] cannot build tmux: missing $missing"
    warn "       install those, then re-run: scripts/provision_durable_tmux.sh"
    return 1
  fi

  if [[ -z "$version" ]]; then
    version="$(_latest_released_version)"
    if [[ -z "$version" ]]; then
      version="$TMUX_FALLBACK_VERSION"
      log "[info] newest release lookup failed; building the known-good $version"
    fi
  fi

  if ! _meets_floor "$version"; then
    warn "[warn] refusing to build tmux $version: below the transport floor $TMUX_TRANSPORT_FLOOR"
    return 1
  fi

  prefix="$tools_dir/tmux-$version"
  if [[ -x "$prefix/bin/tmux" ]]; then
    log "[ok]  durable tmux master already built: $prefix/bin/tmux"
    return 0
  fi

  tmp="$(mktemp -d)" || { warn "[warn] cannot create a build directory"; return 1; }
  # shellcheck disable=SC2064  # expand $tmp now: it must be removed even if it is reassigned
  trap "rm -rf '$tmp'" EXIT

  log "[..]  building tmux $version into $prefix"
  if ! curl -fsSL --max-time 120 \
      "https://github.com/tmux/tmux/releases/download/$version/tmux-$version.tar.gz" \
      -o "$tmp/tmux.tar.gz" 2>/dev/null; then
    warn "[warn] could not download tmux $version"
    warn "       check network egress, then re-run: scripts/provision_durable_tmux.sh"
    return 1
  fi
  tar -xzf "$tmp/tmux.tar.gz" -C "$tmp" || { warn "[warn] could not unpack tmux $version"; return 1; }
  src="$tmp/tmux-$version"
  [[ -d "$src" ]] || { warn "[warn] unexpected tarball layout for tmux $version"; return 1; }

  # Configure FIRST without touching the package manager: on many machines libevent and
  # ncurses headers are already present, and attempting a dependency install we do not
  # need is how a build acquires a sudo requirement it never had.
  if ! ( cd "$src" && ./configure --prefix="$prefix" >"$tmp/configure.log" 2>&1 ); then
    # `apt-get --version` under sudo, not `true`: the command this branch is about to run is
    # `sudo -n apt-get install`, and a `true` probe asks about a different program entirely.
    # Probe the intended package-manager command non-interactively.
    if ! command -v apt-get >/dev/null 2>&1 || ! sudo -n apt-get --version >/dev/null 2>&1; then
      warn "[warn] tmux $version needs build dependencies this machine is missing, and"
      warn "       passwordless sudo is not available, so they cannot be installed here."
      warn "       Run these two commands yourself (sudo will prompt you for a password):"
      warn "         sudo apt-get install -y ${TMUX_BUILD_DEPS[*]}"
      warn "         scripts/provision_durable_tmux.sh"
      warn "       Until then auto-compact stays refused on this machine: the transport"
      warn "       needs a tmux server at or above $TMUX_TRANSPORT_FLOOR."
      return 1
    fi
    log "[..]  installing build dependencies with passwordless sudo"
    # shellcheck disable=SC2024  # the redirect running unprivileged is the INTENT: the log
    # belongs to this script's own mktemp directory, not to a root-owned path, so a
    # `| sudo tee` would escalate a write that needs no privilege.
    if ! sudo -n apt-get install -y ${TMUX_BUILD_DEPS[@]+"${TMUX_BUILD_DEPS[@]}"} >"$tmp/apt.log" 2>&1; then
      warn "[warn] installing build dependencies failed; see the tail below"
      tail -5 "$tmp/apt.log" >&2 || true
      warn "       re-run by hand: sudo apt-get install -y ${TMUX_BUILD_DEPS[*]}"
      return 1
    fi
    if ! ( cd "$src" && ./configure --prefix="$prefix" >"$tmp/configure.log" 2>&1 ); then
      warn "[warn] tmux $version still does not configure after installing dependencies"
      tail -5 "$tmp/configure.log" >&2 || true
      return 1
    fi
  fi

  if ! ( cd "$src" && make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)" \
        >"$tmp/make.log" 2>&1 && make install >>"$tmp/make.log" 2>&1 ); then
    warn "[warn] building tmux $version failed; see the tail below"
    tail -5 "$tmp/make.log" >&2 || true
    return 1
  fi

  # Prove the built binary answers before calling this a success: a master that cannot
  # report its version is one Step 1g will skip, which would look like a working build.
  local built
  built="$("$prefix/bin/tmux" -V 2>/dev/null | awk '{print $2}')" || built=""
  if ! _meets_floor "$built"; then
    warn "[warn] built tmux reports '${built:-nothing}', which does not meet the floor $TMUX_TRANSPORT_FLOOR"
    return 1
  fi
  log "[fixed] tmux $built built into $prefix -- install.sh Step 1g will provision it"
  return 0
}

main "$@"
