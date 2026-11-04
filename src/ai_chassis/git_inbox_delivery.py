"""The git-inbox rung's ordered delivery layers (ISSUE-REF Phase C, D-4).

``[git_inbox_notify].delivery`` is an ordered list of layers. Each layer returns one of
``delivered``, ``unavailable(<reason>)`` or ``refused(<reason>)``, and every outcome is recorded
in the delivered ledger record's ``layers`` field. A message counts as notified when at least
one layer returned ``delivered``.

* ``hook`` is the marker contract the notifier's ``_write_markers`` already performed for the
  batch. It is the floor: a ``delivery`` list without it is dropped to the default. Its outcome is
  ``written`` (D-9): the session is told when its hook claims the line, which the ledger records
  separately as ``delivered``.
* ``prompt`` is an adapter over ``ai_chassis.prompt_injection``'s public API (design
  ``issue-ref`` § Public API for consumers) and its ``InboxNudgeInjector``, the use-case injector
  the library ships for this watcher. It writes no marker and no ``notice.id``, and it never
  drives a terminal itself: the library is the fleet's one injector. What it hands the library
  is a HARNESS-AUTHORED one-line nudge plus an attempt sentinel. A shared view claim requires
  its full sanitized notice context in the prompt; that notice already carries the counts,
  recipient and lookup details, so no redundant wrapper is added. Message bodies never enter
  the payload.

The bypass-permissions hold (D-8) runs in the watcher BEFORE this module: a held target is not in
the batch's ``per_slug``, so no layer here is ever reached for it.
"""

from __future__ import annotations

import importlib
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAYER_HOOK = "hook"
LAYER_PROMPT = "prompt"
KNOWN_LAYERS = (LAYER_HOOK, LAYER_PROMPT)
DEFAULT_DELIVERY: tuple[str, ...] = (LAYER_HOOK, LAYER_PROMPT)

DELIVERED = "delivered"
#: The hook layer's outcome at the marker write (D-9): the line is on disk, and nobody has seen it.
#: The session's later claim is recorded `delivered` in its own ledger record, not here.
WRITTEN = "written"

LIBRARY_MODULE = "ai_chassis.prompt_injection"
#: Where the library keeps its use-case injectors outside the consumer contract (its package
#: docstring), ``InboxNudgeInjector`` among them.
INJECTOR_MODULE = "ai_chassis.prompt_injection.injector"
INJECTOR_NAME = "InboxNudgeInjector"
INBOX_DIRECTORY = "coordination/session-inbox/"

#: Library states that mean the nudge landed. ``QUEUED`` is re-read once through ``verify``.
_LANDED_STATES = frozenset({"SENT", "STARTED", "COMPLETED"})
_QUEUED = "QUEUED"
#: The design's § Public API for consumers names this adapter calls; nothing else is imported.
_PUBLIC_NAMES = ("resolve_target", "PromptPayload", "verify")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REASON_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]+")
_REASON_MAX = 80


def unavailable(reason: str) -> str:
    return f"unavailable({_token(reason)})"


def refused(reason: str) -> str:
    return f"refused({_token(reason)})"


def _token(text: str) -> str:
    """A ledger-safe reason token: the library's reason is recorded, never trusted as text."""
    cleaned = _REASON_UNSAFE.sub("-", str(text)).strip("-")
    return cleaned[:_REASON_MAX] or "unknown"


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------


def _delivery(value: Any) -> tuple[tuple[str, ...], str | None]:
    """The validated layer order, or the default and the reason it was dropped."""
    if value is None:
        return DEFAULT_DELIVERY, None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return DEFAULT_DELIVERY, "delivery is not a list of layer names"
    unknown = sorted(set(value) - set(KNOWN_LAYERS))
    if unknown:
        return DEFAULT_DELIVERY, f"delivery names unknown layer(s) {unknown}"
    if len(set(value)) != len(value):
        return DEFAULT_DELIVERY, "delivery lists a layer twice"
    if LAYER_HOOK not in value:
        return DEFAULT_DELIVERY, "delivery omits the hook floor"
    return tuple(value), None


def load_config(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """``[git_inbox_notify]`` with the validated ``delivery`` list added.

    Phase A's ``git_inbox.load_config`` types the scalars and leaves list-valued keys to the
    phase that consumes them; this is that phase. A ``delivery`` list that omits ``hook``, names
    an unknown layer or is not a list drops to :data:`DEFAULT_DELIVERY` and the reason is logged:
    the floor cannot be configured away.
    """
    from ai_chassis import git_inbox
    from ai_chassis.agent_mail_notifier import read_config_tables

    cfg = git_inbox.load_config(path, env=env)
    try:
        section = read_config_tables(path, env=env).get(git_inbox.CONFIG_SECTION)
    except RuntimeError:
        section = None
    raw = section.get("delivery") if isinstance(section, dict) else None
    order, reason = _delivery(raw)
    if reason is not None and log is not None:
        log(f"git-inbox: [git_inbox_notify].{reason}; using {list(DEFAULT_DELIVERY)}")
    cfg["delivery"] = order
    return cfg


# ---------------------------------------------------------------------------------------------
# The prompt adapter
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerResult:
    outcome: str
    #: The library's outcome fields, verbatim, when the library ran.
    detail: dict[str, Any] | None = None


def new_sentinel() -> str:
    """An attempt sentinel in the library's own ``aih-<8 hex>`` shape."""
    return f"aih-{secrets.token_hex(4)}"


def nudge_text(
    slug: str,
    machine: str,
    messages: Iterable[Any],
    sentinel: str,
    *,
    notice_context: str | None = None,
) -> str:
    """One line with counts/names and, for a view claim, its complete sanitized notice.

    ``messages`` is read for its LENGTH and each message's ``message_class`` -- never for a
    subject, a sender or a body. A ``direct`` anywhere in the set makes the line ``direct``, the
    same rule the status-line record uses (D-17). ``sentinel`` is how the library tells this
    line's ``prompt-record`` landing apart from any other user turn.
    ``notice_context`` comes from the notifier's coherent publication, never message bodies.
    """
    if notice_context is not None:
        # The shared claim covers this exact view. Preserve it whole and spend
        # the bounded prompt budget on context rather than repeated metadata.
        return f"git-inbox {sentinel}: {notice_context}"
    batch = list(messages)
    classes = {getattr(m, "message_class", "") for m in batch}
    message_class = "direct" if "direct" in classes else "broadcast"
    return (
        f"git-inbox {sentinel}: {len(batch)} new {message_class} message(s) for "
        f"{slug}@{machine}; the notice is in your hook context; files under {INBOX_DIRECTORY}"
    )


def _outcome_detail(outcome: Any) -> dict[str, Any]:
    return {
        "state": getattr(outcome, "state", None),
        "landing": getattr(outcome, "landing", None),
        "reason": getattr(outcome, "reason", None),
        "submitted": getattr(outcome, "submitted", None),
        "attempt_id": getattr(outcome, "attempt_id", None),
    }


def _classify(detail: dict[str, Any]) -> str:
    state = str(detail.get("state") or "")
    if state in _LANDED_STATES:
        return DELIVERED
    reason = detail.get("reason")
    return refused(
        f"{state.lower() or 'unknown'}:{reason}" if reason else state.lower()
    )


def _inject(  # noqa: PLR0913 - injection boundary plus optional delivery authority
    library: Any,
    injector_cls: Any,
    slug: str,
    text: str,
    sentinel: str,
    *,
    before_submit: Callable[[], str | None] | None = None,
) -> LayerResult:
    """Inject only at a proved idle turn boundary; verify a ``QUEUED`` outcome once."""
    from ai_chassis.context_supervisor import (
        _transcript_quiet_boundary,
        _turn_quiet_seconds,
    )
    from ai_chassis import idle_wake

    try:
        target = library.resolve_target(f"session:{slug}")
        # AC-1.2: an empty composer is also available mid-turn. Reuse the
        # supervisor's transcript-quiet proof (IDLE_PROOFS), not composer-empty.
        # Missing evidence fails closed and leaves notification to the hook.
        quiet = _turn_quiet_seconds()
        if (
            target.transcript_path is None
            or quiet is None
            or not _transcript_quiet_boundary(target.transcript_path, quiet)
        ):
            return LayerResult(unavailable("busy"))
        injector = injector_cls(
            observers=[],
            submission_gate=lambda: idle_wake.load_policy().automatic_wakes,
        )
        if before_submit and (reason := before_submit()):
            return LayerResult(unavailable(reason))
        outcome = injector.run(target, library.PromptPayload(text, sentinel=sentinel))
        detail = _outcome_detail(outcome)
        if detail["state"] == _QUEUED and detail["attempt_id"]:
            detail["verified"] = _outcome_detail(library.verify(detail["attempt_id"]))
            if detail["verified"]["state"] in _LANDED_STATES:
                return LayerResult(DELIVERED, detail)
    except Exception as exc:  # noqa: BLE001 - a recorded outcome, never a tick failure
        if type(exc).__name__ == "TargetUnresolved":
            return LayerResult(unavailable("target-unresolved"))
        return LayerResult(unavailable(f"library-error:{type(exc).__name__}"))
    return LayerResult(_classify(detail), detail)


def prompt_layer(  # noqa: C901, PLR0911, PLR0913 - explicit transport, quota and library refusals
    slug: str,
    machine: str,
    messages: list[Any],
    *,
    cfg: Mapping[str, Any],
    wake_config: Any | None = None,
    importer: Callable[[str], Any] = importlib.import_module,
    resolver: Callable[..., Any] | None = None,
    before_submit: Callable[[], str | None] | None = None,
    notice_context: str | None = None,
) -> LayerResult:
    """Ask the shared library's ``InboxNudgeInjector`` to type :func:`nudge_text` into
    ``session:<slug>``.

    The ``inbox-nudge`` use case is the library's switch for this consumer; ``idle-nudge`` is the
    goal-waker's and is never used here. Never raises: an import or attribute error is
    ``unavailable(library-absent)`` and any other library failure is
    ``unavailable(library-error:<type>)``, so the notifier's tick survives a library that moved
    under it (Risk 6). Every name is resolved before anything is called, so a library missing
    one types nothing.
    """
    from ai_chassis import wake_config as session_wake
    from ai_chassis import wake_sidecar
    from ai_chassis.quota_optional import admission

    # FROM headers and message bodies are untrusted for chief/human bypasses.
    # The watcher/marker store remains authoritative while this prompt is held.
    batch_size = len(messages)
    messages = [
        message
        for message in messages
        if admission.check("git-inbox-prompt", slug, str(message.message_id))
    ]
    if batch_size and (
        not messages or (notice_context is not None and len(messages) != batch_size)
    ):
        # A coherent notice belongs to the caller's entire immutable view. It
        # cannot be reused for a filtered subset without exposing held work.
        # The publisher must offer a view containing only admitted references.
        return LayerResult(unavailable("quota-held"))

    if wake_config is None:
        from ai_chassis.remote_launch import read_tables

        wake_config = wake_sidecar.load_wake_sidecar_config(read_tables(), machine)
    resolution = (resolver or session_wake.resolve_runtime)(slug, machine)
    permitted, reason = session_wake.sidecar_permit("wake", wake_config, resolution)
    if not permitted:
        return LayerResult(unavailable(reason))
    if not cfg.get("prompt_enabled"):
        return LayerResult(unavailable("disabled"))
    try:
        library = importer(LIBRARY_MODULE)
        for name in _PUBLIC_NAMES:
            getattr(library, name)
        injector_cls = getattr(importer(INJECTOR_MODULE), INJECTOR_NAME)
    except (ImportError, AttributeError):
        return LayerResult(unavailable("library-absent"))
    sentinel = new_sentinel()
    text = nudge_text(slug, machine, messages, sentinel, notice_context=notice_context)
    limit = int(cfg.get("prompt_max_chars") or 0)
    if not _SLUG_RE.match(slug):
        return LayerResult(refused("unaddressable-slug"))
    if limit <= 0 or len(text) > limit:
        return LayerResult(refused("nudge-over-prompt_max_chars"))

    def before_admitted_submit():
        if not all(
            admission.check("git-inbox-prompt", slug, str(message.message_id))
            for message in messages
        ):
            return "quota-held"
        return before_submit() if before_submit else None

    result = _inject(
        library,
        injector_cls,
        slug,
        text,
        sentinel,
        before_submit=before_admitted_submit,
    )
    if result.outcome == DELIVERED:
        for message in messages:
            admission.complete("git-inbox-prompt", slug, str(message.message_id))
    return result


# ---------------------------------------------------------------------------------------------
# The ordered loop
# ---------------------------------------------------------------------------------------------

#: One layer: ``(slug, messages) -> LayerResult``.
Layer = Callable[[str, list[Any]], LayerResult]


@dataclass
class DeliveryReport:
    """Per target: each layer's outcome in configured order, and the prompt library's detail."""

    outcomes: dict[str, dict[str, str]] = field(default_factory=dict)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def notified(self, slug: str) -> bool:
        return DELIVERED in self.outcomes.get(slug, {}).values()


def run_layers(
    per_slug: Mapping[str, list[Any]],
    *,
    order: Iterable[str],
    layers: Mapping[str, Layer],
) -> DeliveryReport:
    """Run every configured layer, in order, for every target the batch delivered to.

    Every layer runs whatever an earlier one returned: the layers are belt and suspenders, and
    a ``delivered`` hook does not make an idle session read its context. ``per_slug`` is the
    watcher's post-hold set, so a held target is never a key here (D-8).
    """
    report = DeliveryReport()
    sequence = tuple(order)
    for slug, messages in sorted(per_slug.items()):
        if not messages:
            continue
        outcomes: dict[str, str] = {}
        for name in sequence:
            layer = layers.get(name)
            if layer is None:
                outcomes[name] = unavailable("layer-not-wired")
                continue
            result = layer(slug, messages)
            outcomes[name] = result.outcome
            if result.detail is not None:
                report.details.setdefault(slug, {})[name] = result.detail
        report.outcomes[slug] = outcomes
    return report


def apply_to_records(records: list[dict[str, Any]], report: DeliveryReport) -> None:
    """Write each record's ``layers`` from the report, for the targets that record names.

    A record's ``targets`` are the sessions this batch delivered it to, and the prompt layer
    nudges once per session for the whole batch, so a target's outcome applies to every record
    naming it. ``layers`` keeps the design's ``{layer: outcome}`` shape when every target of the
    record shares an outcome, else ``mixed``; ``layers_by_target`` always carries the per-target
    truth. ``notified`` is true when every target had at least one ``delivered`` layer. A record
    with no target in the report (held, pending, machine-addressed) is left as the watcher wrote
    it.
    """
    for record in records:
        targets = [
            slug for slug in record.get("targets") or [] if slug in report.outcomes
        ]
        if not targets:
            continue
        by_target = {slug: dict(report.outcomes[slug]) for slug in targets}
        summary: dict[str, str] = {}
        for name in next(iter(by_target.values())):
            values = {outcomes.get(name) for outcomes in by_target.values()}
            summary[name] = values.pop() if len(values) == 1 else "mixed"
        record["layers"] = summary
        record["layers_by_target"] = by_target
        details = {
            slug: report.details[slug] for slug in targets if slug in report.details
        }
        if details:
            record["layer_details"] = details
        record["notified"] = all(report.notified(slug) for slug in targets)
