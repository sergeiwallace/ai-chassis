#!/usr/bin/env bash
# Install ai-chassis from a release tag of its repository.
#
# The command line tool is installed with uv in editable mode from a checkout, because
# several commands read the scripts, configuration and templates that sit beside the package.
# Run the script again to update: it fetches the tags and moves the checkout to the new one.
#
# Every location and choice can be set with a flag or an environment variable.
set -euo pipefail

REPO="${AI_CHASSIS_REPO:-https://github.com/sergeiwallace/ai-chassis.git}"
REF="${AI_CHASSIS_REF:-}"
CHECKOUT="${AI_CHASSIS_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ai-chassis}"
BIN_DIR="${AI_CHASSIS_BIN_DIR:-}"
CONFIG_DIR="${AI_CHASSIS_CONFIG_DIR:-}"
TEMPLATES="${AI_CHASSIS_TEMPLATES:-}"
INSTALL_UV="${AI_CHASSIS_INSTALL_UV:-0}"
RUN_INIT=1
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --ref REF         Tag or branch to install. Default: the newest stable release tag.
  --dir DIR         Where the checkout lives. Default: ~/.local/share/ai-chassis.
  --bin-dir DIR     Where uv places the ai-chassis command. Default: uv's own choice.
  --config-dir DIR  Configuration directory for the initial config file. Default: the tool's own.
  --repo URL        Repository to clone. Default: the public ai-chassis repository.
  --templates DIR   Copy the files in DIR into <checkout>/config when they are not there yet.
  --install-uv      Install uv with its official installer when it is missing.
  --no-init         Do not create the initial configuration file.
  --dry-run         Print what would run and change nothing.
  -h, --help        Show this help.

Each option has an environment variable: AI_CHASSIS_REF, AI_CHASSIS_DIR,
AI_CHASSIS_BIN_DIR, AI_CHASSIS_CONFIG_DIR, AI_CHASSIS_REPO, AI_CHASSIS_TEMPLATES and
AI_CHASSIS_INSTALL_UV=1. A flag wins over its variable.
EOF
}

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

usage_error() {
  printf 'install.sh: %s\n' "$*" >&2
  usage >&2
  exit 2
}

need_value() {
  [ "$#" -ge 2 ] || usage_error "$1 needs a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) need_value "$@"; REF="$2"; shift 2 ;;
    --dir) need_value "$@"; CHECKOUT="$2"; shift 2 ;;
    --bin-dir) need_value "$@"; BIN_DIR="$2"; shift 2 ;;
    --config-dir) need_value "$@"; CONFIG_DIR="$2"; shift 2 ;;
    --repo) need_value "$@"; REPO="$2"; shift 2 ;;
    --templates) need_value "$@"; TEMPLATES="$2"; shift 2 ;;
    --install-uv) INSTALL_UV=1; shift ;;
    --no-init) RUN_INIT=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
done

# Print the command; run it unless this is a dry run.
run() {
  printf '+ %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

command -v git >/dev/null 2>&1 || die "git is required and was not found"

if ! command -v uv >/dev/null 2>&1; then
  if [ "$INSTALL_UV" = "1" ]; then
    printf '+ curl -LsSf https://astral.sh/uv/install.sh -o <temporary file>, then sh < <temporary file>\n'
    if [ "$DRY_RUN" -eq 0 ]; then
      command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
      # Download the whole script first and run it only after the download succeeded. Piping
      # curl into sh would run a fragment if the connection dropped part way.
      uv_installer="$(mktemp "${TMPDIR:-/tmp}/ai-chassis-uv-install.XXXXXX")"
      trap 'rm -f "$uv_installer"' EXIT
      curl -LsSf https://astral.sh/uv/install.sh -o "$uv_installer" \
        || die "could not download the uv installer; nothing was run"
      sh < "$uv_installer"
      PATH="$HOME/.local/bin:$PATH"
    fi
  else
    die "uv is required and was not found. Install it from https://docs.astral.sh/uv/ or run this script again with --install-uv"
  fi
fi

# The newest stable release tag: vMAJOR.MINOR.PATCH with nothing after it. Empty when there is none.
latest_tag() {
  local listing
  listing="$(git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*')" \
    || die "could not list the tags of $REPO"
  printf '%s\n' "$listing" | sed 's#.*refs/tags/##' | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } | head -n 1
}

if [ -z "$REF" ]; then
  REF="$(latest_tag)"
  [ -n "$REF" ] || die "no release tag found in $REPO; pass --ref to choose a branch or tag"
fi

printf 'Installing ai-chassis %s from %s into %s\n' "$REF" "$REPO" "$CHECKOUT"

if [ -d "$CHECKOUT/.git" ]; then
  # The tags were listed from $REPO, so the checkout must come from the same place.
  existing_origin="$(git -C "$CHECKOUT" remote get-url origin 2>/dev/null || true)"
  [ "$existing_origin" = "$REPO" ] \
    || die "$CHECKOUT is a checkout of ${existing_origin:-an unknown repository}, not $REPO; pass another --dir or remove it"
  run git -C "$CHECKOUT" fetch --tags --force --quiet origin
else
  run git clone --quiet "$REPO" "$CHECKOUT"
fi
run git -C "$CHECKOUT" checkout --quiet "$REF"
# A branch moves, so bring it level with its remote; a tag has nothing to pull.
if [ "$DRY_RUN" -eq 0 ] && git -C "$CHECKOUT" symbolic-ref --quiet HEAD >/dev/null 2>&1; then
  run git -C "$CHECKOUT" merge --ff-only --quiet "origin/$REF"
fi

if [ -n "$TEMPLATES" ]; then
  [ -d "$TEMPLATES" ] || die "templates directory not found: $TEMPLATES"
  run mkdir -p "$CHECKOUT/config"
  for source in "$TEMPLATES"/*; do
    [ -f "$source" ] || continue
    target="$CHECKOUT/config/$(basename "$source")"
    if [ -e "$target" ]; then
      printf 'keeping existing %s\n' "$target"
    else
      run cp "$source" "$target"
    fi
  done
fi

if [ -n "$BIN_DIR" ]; then
  export UV_TOOL_BIN_DIR="$BIN_DIR"
fi
run uv tool install --force --editable "$CHECKOUT"

# Where the command landed. A dry run installed nothing, so it only names the choice.
if [ -n "$BIN_DIR" ]; then
  tool_bin="$BIN_DIR"
elif [ "$DRY_RUN" -eq 0 ]; then
  tool_bin="$(uv tool dir --bin)"
else
  tool_bin="uv's tool bin directory"
fi

if [ "$RUN_INIT" -eq 1 ]; then
  config_root="${CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/ai-harness}"
  if [ -e "$config_root/ai-harness.toml" ]; then
    printf 'keeping existing configuration %s\n' "$config_root/ai-harness.toml"
  elif [ -n "$CONFIG_DIR" ]; then
    run "$tool_bin/ai-chassis" --config "$CONFIG_DIR" init
  else
    run "$tool_bin/ai-chassis" init
  fi
fi

printf 'Done. Make sure %s is on your PATH.\n' "$tool_bin"
