"""Launch continuations in the existing obligation ledger; no readiness wait."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import psutil

from ai_chassis import git_inbox_watch as inbox, remote_launch, remote_restart

from . import sessions
from .events import Event, LIFECYCLE_KINDS, digest
from .targets import SessionTarget, safe_identity

WAITING = {"launch-reserved", "launch-waiting"}
BLOCKED = WAITING | {"launch-failed"}
SIGNALS = LIFECYCLE_KINDS | {
    "session-start",
    "session-evidence",
    "process-exit",
    "pane-evidence",
    "pane-unavailable",
}
MAX_PIN_ID_CHARS = 256


def start(host, watcher, file, envelope, run, targets, pending):  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0917 - reservation, unlocked spawn and receipt handoff
    """Reserve both ledgers before starting; replay can only reconcile an attempt."""
    if host is None or not (targets or pending):
        return True  # No authenticated host action capability: fail closed.
    event = Event(
        "git-inbox",
        str(envelope.id),
        file.blob,
        SessionTarget(next(iter(set(targets) | pending))),
        requires_action=True,
        sender=inbox.address_text(envelope.from_),
    )
    with host.writer_lock():
        row = host.obligations.get(event.identity)
        if row is not None and "launch_rejection" in row:
            return False  # Permanent authentication refusal; ordinary notice only.
        if row is not None and row["state"] != "launch-reserved":
            return True
        if row is not None and row.get("quota_held"):
            row = {
                **row,
                "quota_held": False,
                "launch": {
                    **row["launch"],
                    "deadline": host.clock() + remote_restart.bound_seconds(),
                },
            }
            host._save_obligation(row)
        fresh = row is None
        attempted = str(envelope.id) in run.view.launch_attempted
        message = (
            inbox.carried_message(envelope.body)
            if row is None
            else row["launch"]["message"]
        )
        if not isinstance(message, str):
            # Legacy parsing treats missing and mistyped messages as no prompt.
            # They cannot become an event-owned prompt obligation. Settle this
            # reservation durably and retain ordinary recipient notice handling.
            host._save_obligation(
                {
                    "event": event.as_record(),
                    "state": "pending",
                    "delivered": [],
                    "launch_rejection": "launch-message:not-a-string",
                }
            )
            return False
        if row is None:
            # The envelope parser intentionally retains malformed carriers for
            # the authenticating callback. Reserve before that callback, without
            # assuming the hostile object or its id has the authenticated shape.
            node = envelope.launch_event
            launch_id = node.get("id") if isinstance(node, dict) else None
            if not isinstance(launch_id, str):
                launch_id = None
            row = {
                "event": event.as_record(),
                "state": "launch-reserved",
                "delivered": [],
                "launch": {
                    "carrier_id": str(envelope.id),
                    "live_target": bool(targets) and not pending,
                    "launch_id": launch_id,
                    "recipient": event.target.value,
                    "session_id": None,
                    "message": message,
                    "message_digest": digest(message),
                    "since": host.clock(),
                    "deadline": host.clock()
                    + (
                        remote_restart.bound_seconds()
                        if targets
                        else remote_launch.LAUNCH_TIMEOUT_S
                    ),
                },
            }
            host._save_obligation(row)
        # Reconciliation uses the reservation, never a newly-live recipient.
        # Older reservations lack this restriction; replay still cannot execute
        # an already-attempted action or fall back to ordinary notice delivery.
        live_target = row["launch"].get("live_target", False)
    result = watcher._attempt_launch(
        envelope,
        str(envelope.id),
        set() if live_target else {row["launch"]["recipient"]},
        run,
        live_target=live_target,
    )
    with host.writer_lock():
        row = host.obligations[event.identity]
        if row["state"] != "launch-reserved":
            return True
        reason = getattr(result, "reason", "")
        if reason == "quota-held":
            host._save_obligation({**row, "quota_held": True, "reason": reason})
            return True
        malformed = reason == "event:not-an-object" or reason.startswith("event:bad-")
        if malformed or (
            fresh
            and not attempted
            and targets
            and reason == "event:target-or-verb-mismatch"
        ):
            # Like the legacy watcher, a permanently refused carrier continues
            # ordinary recipient notice handling. Persist this classification
            # before acknowledging receiving, including reservation recovery.
            host._save_obligation(
                {
                    "event": event.as_record(),
                    "state": "pending",
                    "delivered": [],
                    "launch_rejection": reason,
                }
            )
            return False
        identity = getattr(result, "launched_identity", None)
        launch = dict(row["launch"])
        if identity:
            recipient, _, machine = identity.partition("@")
            if machine != watcher.machine:
                identity = None
            else:
                launch["recipient"] = safe_identity(recipient)
        launch.update(
            restart=live_target
            or getattr(getattr(result, "request", None), "verb", None)
            == remote_launch.VERB_RESTART
            or getattr(result, "engine_pid", None) is not None,
            engine_pid=getattr(result, "engine_pid", None),
            engine_start_time=getattr(result, "engine_start_time", None),
        )
        request_id = getattr(result, "request_id", None)
        log_path = getattr(result, "log_path", None)
        if launch["restart"] and request_id and log_path:
            launch.update(request_id=request_id, restart_job=str(Path(log_path).parent))
        host._save_obligation(
            {
                **row,
                "launch": launch,
                "state": "launch-waiting" if identity else "launch-failed",
                "reason": getattr(result, "reason", "launch-result-unavailable"),
            }
        )
        if not host.catching_up:
            # Bind/completion can both beat the spawn response. Replay only
            # already admitted lifecycle facts so that race needs no later event.
            check_batch(host, list(host.obligations))
    return True


def _event_pin(path, pid):
    pin = json.loads(Path(path).read_text())
    if not isinstance(pin, dict):
        raise ValueError("invalid-event-pin")
    expires = pin["expires_at"]
    if (
        pin["status"] not in ("acting", "complete")
        or type(pin["old_agent_pid"]) is not int
        or pin["old_agent_pid"] <= 0
        or pin["old_agent_pid"] == pid
        or isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not sessions.time.time() < expires < remote_restart.MAX_BIRTH_TIME
        or not isinstance(pin["event_completion"], dict)
        or "launch_id" not in pin["event_completion"]
    ):
        raise ValueError("invalid-event-pin")
    launch_id = pin["event_completion"]["launch_id"]
    if launch_id is not None and (
        not isinstance(launch_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", launch_id)
    ):
        raise ValueError("invalid-pin-launch-id")
    safe_identity(pin["session_id"])
    return pin


def _replacement_birth(path, pin, pid, session_id):
    """Ai-cli writes this request-specific exec receipt before the hook can bind."""
    request_id = safe_identity(pin["request_id"])
    if len(request_id) > MAX_PIN_ID_CHARS:
        raise ValueError("invalid-restart-request-id")
    name = path.name.removesuffix(".request.json")
    receipt = json.loads(path.with_name(f"{name}.{request_id}.launched").read_text())
    if not isinstance(receipt, dict):
        raise ValueError("invalid-restart-exec-receipt")
    birth = psutil.Process(pid).create_time()
    if (
        pin["session_id"] != session_id
        or receipt["request_id"] != request_id
        or type(receipt["replacement_pid"]) is not int
        or receipt["replacement_pid"] != pid
        or receipt["replacement_start_time"] != birth
    ):
        raise ValueError("foreign-restart-replacement")
    return birth


def binding_provenance(pid, pane, *, session_id=None):
    """Capture the bound Claude session identity; restart authority is never an env."""
    if pane and pid:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            name = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("invalid-pane")
            base = (
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
                if sys.platform == "win32"
                else Path(
                    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                )
            )
            path = base / "ai-cli-utils/restart-session" / f"{name}.request.json"
            if path.exists():
                pin = _event_pin(path, pid)
                # agent_mail_session_start.py:564-571 passes session_pid() (own_session_pid,
                # :120-137) into sessions.bind (:147-167), NOT the hook subprocess PID.
                # ai-cli session_restart.py:1785-1818 execs this replacement on POSIX;
                # its actor is the separate owner_pid (:1168, :1948), not an ancestor.
                birth = _replacement_birth(path, pin, pid, session_id)
                return {
                    "launch_id": pin["event_completion"]["launch_id"],
                    "restart_session_id": pin["session_id"],
                    "engine_pid": pid,
                    "engine_start_time": birth,
                    "restart_pin": str(path),
                }
        except (OSError, ValueError, TypeError, KeyError, psutil.Error):
            pass  # No restart_pin/process evidence: binding() cannot accept a restart.
    # Ordinary new launches retain their existing forwarded launch reason.
    reason = os.environ.get("AIH_LAUNCH_REASON", "")
    return (
        {"launch_id": reason.partition(":")[2]}
        if reason.startswith("remote-inbox:")
        else {}
    )


def _run_receipt(launch):
    """Validate completion against the actor recorded by this run's supervisor."""
    job = Path(launch["restart_job"])
    if job.name != launch["request_id"] or job.parent.name != remote_restart.RUNS_DIR:
        raise ValueError("invalid-restart-run")
    spec = remote_restart.read(job / "request.json")
    actor = remote_restart.read(job / "started.json")
    completion = remote_restart.read(job / "exit.json")
    if not all(isinstance(record, dict) for record in (spec, actor, completion)):
        raise ValueError("invalid-restart-run")
    if (
        actor["request_id"] != launch["request_id"]
        or spec["record"]["request_id"] != launch["request_id"]
        or spec["bind_identity"] != launch["launch_id"]
        or completion.get("verdict") != "restarted"
        or completion.get("exit") != 0
    ):
        raise ValueError("invalid-restart-run")
    return remote_restart.validate_receipt(
        completion.get("receipt"), launch["launch_id"], actor
    )


def binding(host, row):  # noqa: PLR0911 - fail-closed launch, SID, engine and registration fences
    launch = row["launch"]
    try:
        path = (
            sessions.directory(host.root.parent, launch["recipient"]) / "binding.json"
        )
        bound = json.loads(path.read_text())
        if not isinstance(bound, dict):
            return None
        if (
            bound.get("launch_id") != launch["launch_id"]
            or digest(launch["message"]) != launch["message_digest"]
        ):
            return None
        sid = bound["session_id"]
        if launch.get("session_id") not in {None, sid}:
            return None
        if (
            launch.get("restart")
            or launch.get("engine_pid") is not None
            or "receipt" in launch
        ):
            # Read authority at check time, never from an obligation's cached receipt.
            receipt = _run_receipt(launch)
            pin = _event_pin(bound["restart_pin"], bound["engine_pid"])
            if (
                receipt["launch_id"] != launch["launch_id"]
                or receipt["launch_id"] != pin["event_completion"]["launch_id"]
                or pin["session_id"] != sid
                or sid != receipt["session_id"]
                or bound.get("restart_session_id") != receipt["restart_session_id"]
                or bound.get("engine_pid") != receipt["replacement_pid"]
                or bound.get("engine_start_time") != receipt["replacement_start_time"]
            ):
                return None
        if not sessions.registered(host.root.parent, launch["recipient"], sid):
            return None
        if not sessions.eligible(host.root.parent, bound):
            return None
        return bound
    except (OSError, ValueError, TypeError, KeyError):
        return None


def targets(host, row, directory):
    bound = binding(host, row)
    if bound is None:
        return frozenset()
    return SessionTarget(row["launch"]["recipient"], bound["session_id"]).resolve(
        directory
    )


def check_batch(host, batch):
    signals = [
        Event.from_record(host.obligations[key]["event"])
        for key in batch
        if host.obligations[key]["event"]["source"] in SIGNALS
    ]
    if signals:
        check(host, signals)


def check(host, signals=(), *, startup=False):
    """Only lifecycle intake and startup may check launch deadlines or bindings."""
    for row in list(host.obligations.values()):
        if row["state"] not in WAITING:
            continue
        if row.get("quota_held"):
            continue  # Quota waiting never spends the launch/binding timeout.
        launch = row["launch"]
        bound = binding(host, row) if row["state"] == "launch-waiting" else None
        signalled = startup or (
            bound is not None
            and any(
                event.source in {"session-start", "session-evidence"}
                and host.obligations[event.identity]["state"] == "delivered"
                and event.target
                == SessionTarget(launch["recipient"], bound["session_id"])
                for event in signals
            )
        )
        if bound is not None and signalled and bound["bound_at"] <= launch["deadline"]:
            host._save_obligation(
                {
                    **row,
                    "state": "pending",
                    "launch": {**launch, "session_id": bound["session_id"]},
                }
            )
            host._resume_batch([Event.from_record(row["event"]).identity])
        elif host.clock() >= launch["deadline"] and (
            bound is None or bound["bound_at"] > launch["deadline"]
        ):
            host._save_obligation(
                {**row, "state": "launch-failed", "reason": "bind-deadline"}
            )
    status(host)


def status(host):
    host.status["git-inbox-launches"] = [
        {
            "carrier_id": row["launch"]["carrier_id"],
            "target": row["launch"]["recipient"],
            "age": max(0, host.clock() - row["launch"]["since"]),
            "state": row["state"],
            "deadline": row["launch"]["deadline"],
            "reason": row.get("reason", ""),
        }
        for row in host.obligations.values()
        if "launch" in row and row["state"] in BLOCKED
    ]
