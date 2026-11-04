"""Operator-requested, guarded removal of an abandoned injected draft. Never submit."""

from __future__ import annotations

import argparse
import hashlib

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.config import (
    load_config,
    now,
    resume_root,
    safe_component,
    stage_walk,
)
from ai_chassis.prompt_injection.injector import GoalLoopNudgeInjector, _PaneLease, _Run
from ai_chassis.prompt_injection.observe import (
    AttemptStore,
    CompositeObserver,
    parity_observers,
)
from ai_chassis.prompt_injection.readiness import (
    KEYED_VIEW_TIMEOUT_SECONDS,
    undo_tail_holds,
    walk_band,
)
from ai_chassis.prompt_injection.targets import SessionTarget


def _squeezed(text: str) -> str:
    return "".join(text.split())


def _withdraw_repair_notice(record: dict) -> None:
    """Remove the key's undelivered notice only when it names this attempt's repair
    command. ``clear_notice`` withdraws a retry notice, never a transport notice, so a
    stage-undo-failed notice would otherwise keep telling the operator to repair a
    composer that is now clean; a notice about anything else is left alone."""
    command = record.get("stage_undo_repair_command")
    path = resume_root() / "notices" / f"{safe_component(str(record['key']))}.md"
    try:
        if command and command in path.read_text(encoding="utf-8"):
            path.unlink()
    except OSError:
        pass


def _repair_target(record: dict) -> SessionTarget | str:
    """Check persisted ownership and the live pane fence before recovery."""
    remaining = record.get("stage_undo_remaining_text")
    if (
        record.get("state") not in {"REFUSED", "PENDING_UNDO"}
        or record.get("reason") != "stage-undo-failed"
        or record.get("submissions")
        or not isinstance(remaining, str)
        or not record.get("pane_id")
        or not record.get("pane_fingerprint")
        or not record.get("tmux_command")
    ):
        return "attempt-not-repairable"
    digest = record.get("stage_undo_remaining_digest")
    if (digest is not None or record["state"] == "PENDING_UNDO") and (
        digest != hashlib.sha256(remaining.encode()).hexdigest()
    ):
        return "remaining-digest-mismatch"
    command = tuple(record["tmux_command"])
    target = SessionTarget(
        record["key"], record["pane_id"], record["pane_fingerprint"], None, None
    )
    if _tmux.fingerprint(command, target.pane_id) != target.fingerprint:
        return "fence-mismatch"
    if _tmux.display(command, target.pane_id, "#{pane_in_mode}") != "0":
        return "pane-in-mode"
    return target


def _repair(record: dict, store: AttemptStore) -> str | None:
    target = _repair_target(record)
    if isinstance(target, str):
        return target
    remaining = record["stage_undo_remaining_text"]
    command = tuple(record["tmux_command"])
    injector = GoalLoopNudgeInjector(tmux_command=command)
    injector.observer = CompositeObserver(parity_observers(store.ledger))
    sample = injector.readiness.sample(
        target, phase="stage-verify", staged_text=remaining
    )
    band = walk_band(sample.capture)
    if band is None or not sample.composer_recognized:
        return "composer-hidden"
    if (
        band.closed
        and band.rows == ("",)
        and injector.readiness.cursor(target)
        == (
            2,
            band.first_line,
        )
    ):
        _repaired(injector, _Run(record, target), store)
        return None
    rows = list(band.rows)
    # Keep the filler rows: the cursor is on the last blank row, not on the
    # preceding text. An extra human newline must fail ownership too.
    blank_rows = len(rows) - len(_without_trailing_blanks(rows))
    filler = len(remaining) - len(remaining.rstrip("\n"))
    visible = "".join(rows)
    # The window may show only the remaining draft's tail. Match every visible
    # character before moving the cursor, then let undo recheck after each chunk.
    # Whitespace is squeezed on both sides: a soft wrap is not drawn, so the visible
    # rows lack the space the recorded text has there (measured on Claude Code 2.1.301).
    if (
        not sample.staged_style_safe
        or not visible
        or blank_rows != filler
        or not _squeezed(remaining).endswith(_squeezed(visible))
        or not undo_tail_holds(sample.content, remaining)
        or injector.readiness.cursor(target)
        != (len(rows[-1]) + 2, band.first_line + len(rows) - 1)
    ):
        return "composer-not-fully-owned: restore the unchanged draft tail first"
    run = _Run(record, target)
    failure = injector._undo(run, target, remaining)
    # The pane repaints after the last delete: poll as the injector's own undo does, or a
    # clean composer is refused on a capture of the view before the repaint.
    empty = injector.readiness.poll(
        target,
        lambda sample: (
            sample.composer_recognized and sample.classification == "empty-composer"
        ),
        KEYED_VIEW_TIMEOUT_SECONDS,
        phase="precheck",
    )
    if failure or not (
        empty.composer_recognized and empty.classification == "empty-composer"
    ):
        store.write(run.record)
        return failure or "composer-not-empty"
    _repaired(injector, run, store)
    return None


def _without_trailing_blanks(rows: list[str]) -> list[str]:
    rows = rows.copy()
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _repaired(injector: GoalLoopNudgeInjector, run: _Run, store: AttemptStore) -> None:
    injector._save_undo_remaining(run, "")
    run.record.update(
        state="REFUSED",
        terminal_at=now(),
        staged_text_may_remain=False,
        stage_undo_completed_at=now(),
    )
    store.write(run.record)
    injector.observer.clear_notice(run.record)
    _withdraw_repair_notice(run.record)


def retry_pending_undos(
    *, dry_run: bool = False, pane_id: str | None = None, lease_held: bool = False
) -> dict[str, int]:
    """One guarded recovery per pending attempt, from a timer or a library call.

    The record is re-read under the same pane lease as injection and manual repair.
    Retry policy is read now, so a config edit bounds already-persisted attempts.
    Never stage or submit a prompt as part of recovery.
    """
    report = {"pending": 0, "cleared": 0, "escalated": 0}
    config = load_config()
    for ledger in ("attempts", "wake-attempts"):
        store = AttemptStore(ledger=ledger)
        for record in store.all():
            if record.get("state") != "PENDING_UNDO" or (
                pane_id is not None and record.get("pane_id") != pane_id
            ):
                continue
            report["pending"] += 1
            if dry_run:
                continue
            lease = _PaneLease(str(record.get("pane_id")))
            if not lease_held and not lease.acquire(0):
                continue
            try:
                current = store.find(str(record["attempt_id"]))
                if current is None or current.get("state") != "PENDING_UNDO":
                    continue
                result = _retry_undo(current, store, config)
                if result != "pending":
                    report["pending"] -= 1
                    report[result] += 1
            finally:
                if not lease_held:
                    lease.release()
    return report


def _retry_undo(record: dict, store: AttemptStore, config: dict) -> str:
    record["stage_undo_retries"] = int(record.get("stage_undo_retries") or 0) + 1
    failure = _repair(record, store) if config["enabled"] else "config-disabled"
    if failure is None:
        return "cleared"
    record["stage_undo_reason"] = failure
    record["stage_undo_last_retry_at"] = now()
    if record["stage_undo_retries"] < stage_walk(config)["undo_max_retries"]:
        store.write(record)
        return "pending"
    record.update(state="REFUSED", terminal_at=now(), stage_undo_escalated_at=now())
    record["refusal_detail"] = (
        f"{record.get('refusal_detail') or ''}; deferred undo exhausted: {failure}"
    )
    store.write(record)
    observer = CompositeObserver(parity_observers(store.ledger))
    observer.notice(record, "stage-undo-failed")
    command = tuple(record["tmux_command"])
    target = SessionTarget(
        record["key"], record["pane_id"], record["pane_fingerprint"], None, None
    )
    if _tmux.fingerprint(command, target.pane_id) == target.fingerprint:
        GoalLoopNudgeInjector(tmux_command=command).transport.warn(
            target,
            f"AIH: undo exhausted for {record['attempt_id']}; "
            f"{failure}; repair: {record['stage_undo_repair_command']}",
        )
    return "escalated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    matches = []
    # These are the ledgers the library's injectors already own.
    for ledger in ("attempts", "wake-attempts"):
        store = AttemptStore(ledger=ledger)
        record = store.find(args.attempt)
        if record is not None:
            matches.append((store, record))
    if len(matches) != 1:
        print("Repair refused: attempt-not-unique")
        return 3
    store, record = matches[0]
    lease = _PaneLease(str(record.get("pane_id")))
    if not lease.acquire(0):
        print("Repair refused: injection-in-flight")
        return 3
    try:
        record = store.find(args.attempt)
        failure = _repair(record or {}, store)
    finally:
        lease.release()
    print(
        f"Repair refused: {failure}"
        if failure
        else "Injected draft cleared; no Enter sent"
    )
    return 3 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
