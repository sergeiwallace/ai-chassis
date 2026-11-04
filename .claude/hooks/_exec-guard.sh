#!/usr/bin/env bash
# Preserve Claude Code exec-form's direct-parent topology while tolerating stale settings.
# A missing hook passes on purpose, so a half-installed tree never wedges a session.
# The real guards live in the hooks this wrapper dispatches to.
set -euo pipefail
# --setenv NAME=VALUE, repeatable, consumed before the hook path. This exists so a
# hook needing an environment variable can still use exec form. The alternative --
# a `VAR=x bash "$path"` shell string -- costs a whole extra shell process, because
# Claude Code must start a shell to interpret it and that shell then spawns bash.
# Exec form plus this flag keeps the process count minimal: on POSIX the exec below
# replaces this image rather than forking, so the hook costs ONE process.
while [[ "${1:-}" == --setenv ]]; do
  shift
  [[ $# -ge 1 ]] || exit 0
  case "$1" in
    *=*) export "${1%%=*}=${1#*=}" ;;
    *) exit 0 ;;
  esac
  shift
done

# Prefer available Command Line Tools when no developer directory is configured.
if [[ -z "${DEVELOPER_DIR:-}" && -x /Library/Developer/CommandLineTools/usr/bin/git ]]; then
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi

[[ $# -ge 1 ]] || exit 0
hook="$1"
shift
[[ -f "$hook" ]] || exit 0

# Exec bash DIRECTLY for a .sh hook, rather than exec'ing the script and letting its
# `#!/usr/bin/env bash` shebang resolve the interpreter.
#
# Measured on example-client-host-windows: MSYS has no real exec -- it emulates one with a fresh
# Windows process -- so every hop in an exec chain costs a genuine ~2s process
# creation. `exec "$hook"` therefore pays TWO hops (env, then bash) where
# `exec bash "$hook"` pays one, and on a host at ~2s per spawn that difference was
# 4955ms vs 4045ms for the same hook. Worse, the two-hop chain made exec form
# SLOWER than the shell-string form it replaced (5944ms vs 5420ms), inverting the
# whole point of the conversion. Interpreter selection is unchanged: `env bash` and a
# bare `bash` both resolve through PATH.
#
# Anything that is not .sh keeps `exec "$hook"`, because its shebang is load-bearing:
# quality-steering-receipt.py is `#!/usr/bin/env python3`, and running it under bash
# is the exact defect this guard's adoption fixed ("line 2: ... command not found").
# Normalising the .sh shebangs to `#!/bin/bash` would also have removed the env hop,
# and was rejected: `env bash` is deliberate on macOS, where /bin/bash is 3.2 and the
# usable bash is elsewhere on PATH.
case "$hook" in
  *.sh) exec bash "$hook" "$@" ;;
  *) exec "$hook" "$@" ;;
esac
