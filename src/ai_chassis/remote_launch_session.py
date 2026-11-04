"""Resolve launcher-reported identities against tmux, never the requested slot name."""

from __future__ import annotations

import dataclasses
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

from ai_chassis.prompt_injection import PromptPayload
from ai_chassis.prompt_injection.injector import RemoteLaunchInjector
from ai_chassis.session_address import session_address

LAUNCH_REASON = "remote-inbox"
_IDENTITY = re.compile(r"\[launch\] session: resolved ([a-z0-9][a-z0-9-]*)\s*$", re.M)
_FORMAT = "#{session_name}\t#{session_id}\t#{pane_id}\t#{pane_dead}\t#{@ai_cli_session_generation}"
_FIELD_COUNT = 5


@dataclass(frozen=True)
class Pane:
    name: str
    session_id: str
    pane_id: str
    dead: bool
    generation: str


def panes(runner, timeout: float) -> list[Pane] | None:
    try:
        result = runner(
            ["tmux", "list-panes", "-a", "-F", _FORMAT],
            shell=False,
            check=False,
            timeout=max(0.01, min(10, timeout)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = []
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        fields = line.split("\t")
        if (
            len(fields) == _FIELD_COUNT
            and re.fullmatch(r"\$\d+", fields[1])
            and re.fullmatch(r"%\d+", fields[2])
        ):
            found.append(Pane(*fields[:3], fields[3] != "0", fields[4]))
    return found if result.returncode == 0 else None


def reported_identity(output: str) -> str | None:
    matches = set(_IDENTITY.findall(output))
    return matches.pop() if len(matches) == 1 else None


def matching(identity: str, rows: list[Pane]) -> list[Pane]:
    """Use the exact printed identity, allowing only the engine's leading tmux token."""
    return [p for p in rows if p.name.partition("-")[2] == identity]


def restart_target(target: str, history: list[dict], runner) -> tuple[str | None, str]:
    """Resolve an exact identity or a verified launch's recorded name, without deriving names."""
    from ai_chassis import remote_launch as launch

    name, _, machine = target.partition("@")
    identities = {name}
    for record in history:
        identity = record.get("launched_identity")
        if (
            record.get("verb") == launch.VERB_LAUNCH
            and record.get("name") == name
            and record.get("tmux_exists") is True
            and isinstance(identity, str)
            and launch.IDENTITY_RE.fullmatch(identity)
        ):
            slug, _, host = identity.partition("@")
            if host == machine and launch.NAME_RE.fullmatch(slug):
                identities.add(slug)
    rows = panes(runner, 10)
    if rows is None:
        return None, "restart-target-unresolved: tmux unavailable"
    candidates = {
        (identity, pane.session_id)
        for identity in identities
        for pane in matching(identity, rows)
    }
    if len(candidates) != 1:
        return None, f"restart-target-unresolved: {len(candidates)} candidates"
    return session_address(machine, next(iter(candidates))[0]), ""


def agent_pane(rows: list[Pane]) -> Pane | None:
    # The initial engine pane precedes optional wake-watch windows in the same session.
    if not rows or len({p.session_id for p in rows}) != 1:
        return None
    return min(rows, key=lambda p: int(p.pane_id[1:]))


def wait_live(identity: str, runner, deadline: float) -> Pane | None:
    while True:
        remaining = deadline - time.monotonic()
        pane = agent_pane(matching(identity, panes(runner, remaining) or []))
        if pane and not pane.dead:
            return pane
        if remaining <= 0:
            return None
        time.sleep(min(0.1, remaining))


def cleanup(identity: str, before: list[Pane] | None, runner) -> bool:
    """Return whether no new session remains; never infer permission to cancel one.

    ai-cli-utils currently prints a name but no creation-bound ownership receipt.
    Observing its generation later cannot prove which launcher created it. Leave
    an explicit cleanup refusal rather than risk cancelling a concurrent launch.
    """
    if before is None:
        return False
    after = panes(runner, 10)
    if after is None:
        return False
    old_ids = {p.session_id for p in before}
    return not any(p.session_id not in old_ids for p in matching(identity, after))


def deliver(pane: Pane, message: str, timeout: float):
    """One shared-library attempt; its composer, fingerprint and landing guards apply."""
    payload = PromptPayload(message, mode="multiline" if "\n" in message else "type")
    return RemoteLaunchInjector().run(
        f"pane:{pane.pane_id}", payload, ready_wait=max(0, timeout)
    )


def execute_launch(  # noqa: PLR0911, PLR0913 - legacy verification or event-owned binding
    decision, machine, runner, timeout_s, message, *, bind_identity=None
):
    deadline = time.monotonic() + timeout_s
    before = panes(runner, timeout_s) if bind_identity is None else None
    options = (
        {
            "env": {
                **os.environ,
                "AIH_LAUNCH_REASON": (
                    f"{LAUNCH_REASON}:{bind_identity}"
                    if bind_identity
                    else LAUNCH_REASON
                ),
            }
        }
        if message is not None or bind_identity is not None
        else {}
    )
    try:
        completed = (run_owned if runner is subprocess.run else runner)(
            list(decision.argv),
            cwd=decision.cwd,
            shell=False,
            check=False,
            timeout=max(0.01, deadline - time.monotonic()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
            **options,
        )
    except subprocess.TimeoutExpired as exc:
        # stderr is bytes even with text=True on some Python versions.
        output = exc.stderr or b""
        output = (
            output.decode("utf-8", "replace") if isinstance(output, bytes) else output
        )
        if bind_identity is not None:
            return dataclasses.replace(
                decision, decision="refused", reason="launch-timeout"
            )
        return _failed(decision, reported_identity(output), before, runner)
    except OSError as exc:
        return dataclasses.replace(
            decision, decision="refused", reason=f"launch-failed ({type(exc).__name__})"
        )
    code = int(getattr(completed, "returncode", 1))
    decision = dataclasses.replace(decision, exit=code)
    output = (
        str(getattr(completed, "stdout", "") or "")
        + "\n"
        + str(getattr(completed, "stderr", "") or "")
    )
    identity = reported_identity(output)
    if bind_identity is not None:
        return dataclasses.replace(
            decision,
            decision=decision.decision if identity else "refused",
            reason="launch-awaiting-bind" if identity else "launch-identity-unresolved",
            launched_identity=session_address(machine, identity) if identity else None,
        )
    pane = wait_live(identity, runner, deadline) if identity else None
    if pane is None:
        return _failed(decision, identity, before, runner)
    result = dataclasses.replace(
        decision,
        tmux_exists=True,
        reason="tmux-session-live",
        launched_identity=session_address(machine, identity),
    )
    if message is not None:
        return _deliver(result, pane, message, deadline, before=before, runner=runner)
    return result


def _deliver(result, pane, message, deadline, *, before, runner):  # noqa: PLR0913 - the launch's delivery and ownership context
    try:
        outcome = deliver(pane, message, deadline - time.monotonic())
    except Exception as exc:  # noqa: BLE001 - retain audit and do not retry an uncertain submission
        return dataclasses.replace(
            result,
            reason="launch-message-error",
            verdict="unverified",
            verdict_reason=type(exc).__name__,
        )
    result = dataclasses.replace(
        result,
        verdict=outcome.state,
        verdict_reason=outcome.reason,
        delivery_attempt_id=outcome.attempt_id,
        reason="launch-message-delivered"
        if outcome.state == "SENT"
        else "launch-message-unverified",
    )
    if outcome.state == "REFUSED" and not outcome.submitted:
        failed = _failed(
            result, result.launched_identity.partition("@")[0], before, runner
        )
        return failed
    return result


def run_owned(argv, *, timeout, **options):
    """Bound the launcher AND its children by the process group this invocation creates."""
    from ai_chassis.fleet_liveness import _signal_group

    options.pop("capture_output", None)
    options.pop("check", None)
    options.setdefault("start_new_session", True)
    with subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(
                argv, timeout, output=out, stderr=err
            ) from None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _failed(decision, identity, before, runner):
    cleaned = cleanup(identity, before, runner) if identity else False
    reason = "launch-session-timeout" if cleaned else "launch-cleanup-unverified"
    return dataclasses.replace(
        decision,
        decision="refused",
        tmux_exists=bool(decision.tmux_exists),
        reason=reason if identity else "launch-identity-unresolved",
    )
