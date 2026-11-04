"""Read-only chief process binding, shared by require-chief and SessionStart.

The registered tmux pane is the authority, not a name or a persistent PID claim.
All process parsing and start-stamp validation belong to claude_sessions, including
its portable psutil fallback. Unprovable facts never grant chief authority.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn


class ChiefBindingError(Exception):
    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _registered_pane_pid(binding: str, label: str) -> int:
    """Read exactly one registered pane, with a bounded, read-only tmux query."""
    try:
        panes = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_id}.#{pane_id}\t#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChiefBindingError(
            "chief-binding-unavailable",
            f"refused: {label}: registered tmux pane fact is unreadable",
        ) from exc
    if panes.returncode:
        raise ChiefBindingError(
            "chief-binding-unavailable",
            f"refused: {label}: registered tmux pane fact is unreadable",
        )
    pane_pids = []
    for line in panes.stdout.splitlines():
        address, _, pane_pid = line.partition("\t")
        if address == binding and pane_pid.isdecimal():
            pane_pids.append(int(pane_pid))
    if len(pane_pids) != 1:
        raise ChiefBindingError(
            "chief-binding-unavailable",
            f"refused: {label}: registered tmux pane is absent or ambiguous",
        )
    return pane_pids[0]


def require_chief_process(registration: Mapping[str, Any]) -> None:
    """Prove the caller is the unique interactive chief in the registered tmux session.

    No stored PID needs rebinding after a restart. A second matching interactive
    record makes authority ambiguous and refuses both processes until resolved.
    """
    scripts = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import claude_sessions as sessions

    root = sessions._proc_root()
    pid = sessions.own_session_pid()
    label = (
        f"process pid {pid}"
        if pid is not None
        else "calling process (Claude pid unknown)"
    )

    def refuse(reason: str, *, unavailable: bool = False) -> NoReturn:
        status = (
            "chief-binding-unavailable" if unavailable else "chief-process-mismatch"
        )
        raise ChiefBindingError(status, f"refused: {label}: {reason}")

    if pid is None:
        refuse("cannot establish the calling Claude process", unavailable=True)
    records = sessions._validated_records(root, None)
    record = records.get(pid)
    if record is None:
        refuse("no live start-stamp-validated Claude registry entry", unavailable=True)
    target = registration.get("tmux_target")
    if not isinstance(target, str) or not target:
        refuse("registration has no tmux_target", unavailable=True)
    binding = record.get("tmux")
    if (
        record.get("name") != registration["native_agent_name"]
        or record.get("kind") != "interactive"
        or not isinstance(binding, str)
        or binding.split(":", 1)[0] != target
    ):
        refuse("not the registered chief's interactive tmux process")
    pane_pid = _registered_pane_pid(binding, label)
    ancestors = sessions.caller_ancestor_pids()
    if pid not in ancestors:
        refuse("calling process ancestry is unreadable", unavailable=True)
    if pane_pid not in ancestors:
        refuse(f"not descended from registered tmux pane pid {pane_pid}")
    matching = sorted(
        other_pid
        for other_pid, other in records.items()
        if other.get("kind") == "interactive"
        and other.get("name") == registration["native_agent_name"]
        and isinstance(other.get("tmux"), str)
        and other["tmux"].split(":", 1)[0] == target
    )
    if matching != [pid]:
        refuse(f"multiple live chief processes claim this tmux session: {matching}")
