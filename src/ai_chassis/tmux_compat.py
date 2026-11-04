"""Optional tmux observer support; old or unknown clients retain legacy delivery."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence

# Both send-keys -c (upstream 3b3f42053a5f) and the L: client loop first ship
# in 3.4. This is separate from the existing SERVER admission floor (3.7).
OBSERVER_MINIMUM_VERSION = (3, 4)
LEGACY_FINGERPRINT_FORMAT = "#{session_id}|#{@ai_cli_session_generation}|#{session_attached}|#{W/i:#{window_id}[#{P:#{pane_id}=#{pane_pid}=#{pane_dead};}]}"
# One `1` per same-session non-observer client preserves attachment changes,
# including zero. Only read-only control clients are excluded.
TMUX_FINGERPRINT_FORMAT = "#{session_id}|#{@ai_cli_session_generation}|#{L:#{?#{==:#{client_session},#{session_name}},#{?#{&&:#{client_control_mode},#{client_readonly}},,1},}}|#{W/i:#{window_id}[#{P:#{pane_id}=#{pane_pid}=#{pane_dead};}]}"


# Results by command tuple, read once per process. A probe that raises (the
# adapter's command deadline) stores nothing, so a later call probes again.
_FENCE_SUPPORT: dict[tuple[str, ...], bool] = {}


def _default_run(command, **options):
    return subprocess.run(command, timeout=5, **options)


def supports_observer_fence(command: Sequence[str], run=None) -> bool:
    """Read this command's client version once per process, including failures.

    Preserve any socket/wrapper arguments. This selects optional syntax only;
    it never replaces the separate running-server admission check. A caller on
    a deadline-bounded path passes its own bounded ``run`` so the probe honours
    that deadline; its exception propagates and nothing is cached. Without one
    the probe uses a fixed 5 s timeout and any failure reads as unsupported.
    """
    key = tuple(command)
    if key not in _FENCE_SUPPORT:
        _FENCE_SUPPORT[key] = _probe_observer_fence(key, run or _default_run)
    return _FENCE_SUPPORT[key]


def _probe_observer_fence(command: tuple[str, ...], run) -> bool:
    try:
        result = run([*command, "-V"], capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.fullmatch(r"tmux (?:next-)?(\d+)\.(\d+)[a-z]?", result.stdout.strip())
    return (
        result.returncode == 0
        and match is not None
        and (int(match[1]), int(match[2])) >= OBSERVER_MINIMUM_VERSION
    )


def fingerprint_format(command: Sequence[str], run=None) -> str:
    return (
        TMUX_FINGERPRINT_FORMAT
        if supports_observer_fence(command, run)
        else LEGACY_FINGERPRINT_FORMAT
    )


def send_keys_command(command: Sequence[str], run=None) -> tuple[str, ...]:
    return (
        ("send-keys", "-c", "")
        if supports_observer_fence(command, run)
        else ("send-keys",)
    )
