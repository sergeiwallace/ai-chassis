"""The bounded self-heal loop (design § Failure signatures, the repair loop and its state
machine; D-17, D-18).

A validation failure has a signature, ``<use_case>/<stage>/<reason>/<cc_version>``. Per
signature one state file under ``<resume_root>/self-heal/<sha>.json``, changed only under
its ``fcntl`` lock, runs the ``AC-SM-SELF-HEAL`` machine: ``armed`` -> ``bug-fix-pending``
(the ``/delegate <billing-default> /bug-fix`` prompt injected into the TARGET session)
-> ``re-armed``
(only through :meth:`SelfHeal.mark_fixed`, a guard that verifies the fix on the main tree
and then re-runs the pending run) -> ``armed`` once the retry passes, or ``escalated``
once ``max_rounds`` repairs are spent (the escalation prompt, injected once, then only a
human's ``clear``). Every transition is a ``CC-<PREFIX>-SELF-HEAL`` stage line and a
``self-heal`` event in the run record. The loop's own injections are validated and never
self-healed: their use cases carry no ``self_heal`` policy.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ai_chassis.delegation import default_delegation_target
from ai_chassis.prompt_injection.config import (
    load_config,
    now,
    resume_root,
    safe_component,
    self_heal_policy,
    validation,
)
from ai_chassis.prompt_injection.observe import (
    AttemptStore,
    append_run_event,
    run_record_path,
    write_notice,
    write_stage_line,
)
from ai_chassis.prompt_injection.payload import sha256
from ai_chassis.prompt_injection.preflight import PASS, Preflight, refuse

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

ARMED = "armed"
BUG_FIX_PENDING = "bug-fix-pending"
RE_ARMED = "re-armed"
ESCALATED = "escalated"
STATES = (ARMED, BUG_FIX_PENDING, RE_ARMED, ESCALATED)
EVENTS = ("fail", "mark-fixed", "clear", "pass")

#: Pre-submit refusals that are a validation failure (design § Failure signatures; plus
#: the plain stage verify's own mismatch, the walk's predecessor).
PRE_SUBMIT_REASONS = frozenset(
    {
        "walk-tail-unreachable",
        "walk-top-unreachable",
        "walk-top-unproved",
        "walk-stitch-mismatch",
        "walk-unverified-version",
        "stage-undo-failed",
        "stage-verify-mismatch",
    }
)
#: Attempt reasons a post-submit failure is signed with when no verdict names it.
POST_SUBMIT_REASONS = frozenset(
    {"delivered-as-text", "landing-none", "transcript-not-found"}
)
#: The loop's own injections: validated, recorded, never self-healed (D-19).
SELF_HEAL_USE_CASE = "self-heal"
ESCALATION_USE_CASE = "repair-escalation"

BUG_FIX_TEMPLATE = "injection-self-heal-bug-fix.md"
ESCALATION_TEMPLATE = "injection-repair-limit-escalation.md"
DESCRIPTION_MAX_CHARS = 400
_SHA_CHARS = 12
#: The shortest signature prefix `find` accepts.
_MIN_PREFIX_CHARS = 4
_ROW_CLIP = 120
_FIELDS = re.compile(r"^fields:\s*\[(?P<names>[^\]]*)\]\s*$", re.MULTILINE)

REJECT_NOTHING_TO_REARM = "self-heal-nothing-to-rearm"
REJECT_GUARD = "mark-fixed-guard-failed"
REJECT_UNKNOWN = "self-heal-unknown-signature"


# --- signatures -------------------------------------------------------------------------


@dataclass(frozen=True)
class Signature:
    use_case: str
    stage: str
    reason: str
    cc_version: str

    @property
    def human(self) -> str:
        return f"{self.use_case}/{self.stage}/{self.reason}/{self.cc_version}"

    @property
    def sha(self) -> str:
        return sha256(self.human)[:_SHA_CHARS]


def failure_of(record: dict[str, Any], config: dict[str, Any]) -> Signature | None:
    """The validation failure ``record`` ended in, or None when it passed or failed for
    another reason (a busy composer, a disabled use case, a held boundary)."""
    use_case = str(record.get("use_case") or "")
    version = str(record.get("cc_version") or "unknown")
    reason = record.get("reason")
    if reason in PRE_SUBMIT_REASONS and validation(config)["pre_submit"]:
        return Signature(use_case, "pre-submit", str(reason), version)
    verdict = record.get("post_submit_verdict")
    if verdict in {"post-submit-mismatch", "post-submit-missing"}:
        name = reason if reason == "delivered-as-text" else verdict
        return Signature(use_case, "post-submit", str(name), version)
    if verdict is None and reason in POST_SUBMIT_REASONS:
        return Signature(use_case, "post-submit", str(reason), version)
    return None


# --- the machine (pure) -----------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One row of ``AC-SM-SELF-HEAL``: the transition id, the target state, what it does
    (``inject-bug-fix``, ``inject-escalation``, ``refuse``, ``rearm``, ``remove``,
    ``reject`` or ``keep``) and the reason a refusal or rejection names."""

    transition: str
    target: str
    action: str
    reason: str | None = None


def step(
    state: str, event: str, *, rounds: int, max_rounds: int, guard_ok: bool = False
) -> Step:
    """The transition ``event`` takes from ``state``. ``rounds`` counts the ``/bug-fix``
    injections so far; a cap lowered below it escalates, never loops."""
    if state not in STATES or event not in EVENTS:
        raise ValueError(f"no transition for {state!r} on {event!r}")
    if state == ESCALATED:
        if event == "clear":
            return Step("T10", ARMED, "remove")
        return Step("T9", ESCALATED, "refuse", "self-heal-escalated")
    if state == BUG_FIX_PENDING:
        return _from_pending(event, guard_ok=guard_ok)
    return _from_armed(state, event, rounds=rounds, max_rounds=max_rounds)


def _from_pending(event: str, *, guard_ok: bool) -> Step:
    if event == "fail":
        return Step("T3", BUG_FIX_PENDING, "refuse", "self-heal-disarmed")
    if event == "mark-fixed":
        if guard_ok:
            return Step("T4", RE_ARMED, "rearm")
        return Step("T5", BUG_FIX_PENDING, "reject", REJECT_GUARD)
    if event == "clear":
        # D-18 (a), its first Con: a fixer that never runs mark-fixed leaves the
        # signature disarmed until a human clears it.
        return Step("T12", ARMED, "remove")
    return Step("-", BUG_FIX_PENDING, "keep")


def _from_armed(state: str, event: str, *, rounds: int, max_rounds: int) -> Step:
    """``armed`` and ``re-armed``: they differ only in their transition ids and in what a
    pass does."""
    if event == "fail":
        first, last = ("T1", "T2") if state == ARMED else ("T7", "T8")
        if rounds < max_rounds:
            return Step(first, BUG_FIX_PENDING, "inject-bug-fix")
        return Step(last, ESCALATED, "inject-escalation")
    if event in {"mark-fixed", "clear"}:
        return Step("T11", state, "reject", REJECT_NOTHING_TO_REARM)
    if state == RE_ARMED:
        return Step("T6", ARMED, "remove")
    return Step("-", ARMED, "keep")


# --- the state store --------------------------------------------------------------------


class Store:
    """``<resume_root>/self-heal/<sha>.json``, each changed only under ``<sha>.lock``."""

    def __init__(self, root: Path | None = None) -> None:
        self.directory = (root or resume_root()) / "self-heal"

    def path(self, sha: str) -> Path:
        return self.directory / f"{safe_component(sha)}.json"

    @contextlib.contextmanager
    def locked(self, sha: str) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self.directory / f"{safe_component(sha)}.lock",
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self, sha: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path(sha).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def write(self, state: dict[str, Any]) -> Path:
        path = self.path(str(state["sha"]))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(
            json.dumps(state, sort_keys=True, indent=1) + "\n", "utf-8"
        )
        os.replace(temporary, path)
        return path

    def remove(self, sha: str) -> None:
        self.path(sha).unlink(missing_ok=True)

    def all(self) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        found = (self.read(path.stem) for path in sorted(self.directory.glob("*.json")))
        return [state for state in found if state is not None]

    def find(self, ident: str) -> dict[str, Any] | None:
        """By sha, sha prefix or human signature."""
        for state in self.all():
            if ident in {state.get("sha"), state.get("signature")} or (
                len(ident) >= _MIN_PREFIX_CHARS
                and str(state.get("sha", "")).startswith(ident)
            ):
                return state
        return None


def blocking_state(use_case: str, cc_version: str | None) -> dict[str, Any] | None:
    """A ``bug-fix-pending`` or ``escalated`` signature of ``use_case`` on this Claude Code
    version: while one is open the use case's injection itself is paused (the maintainer's
    follow-up 3), so nothing types a ``/goal`` that is expected to fail the same way."""
    version = cc_version or "unknown"
    for state in Store().all():
        if (
            state.get("use_case") == use_case
            and state.get("cc_version") == version
            and state.get("state") in {BUG_FIX_PENDING, ESCALATED}
        ):
            return state
    return None


class SelfHealArmed(Preflight):
    """Refuse before any keystroke while :func:`blocking_state` finds an open repair or an
    escalation for this use case: ``self-heal-disarmed`` naming the pending round, its
    issue and PR when known, or ``self-heal-escalated``. A use case with no ``self_heal``
    policy always passes. The ``resume`` run ``mark-fixed`` starts finds ``re-armed``."""

    def __init__(self, use_case: str) -> None:
        self.use_case = use_case

    def check(self, target, _payload, config):
        if not self_heal_policy(config, self.use_case)[0]:
            return PASS
        found = blocking_state(self.use_case, target.cc_version if target else None)
        if found is None:
            return PASS
        latest = (found.get("history") or [{}])[-1]
        detail = (
            f"signature={found.get('sha')} round={found.get('rounds')} "
            f"issue={latest.get('issue')} pr={latest.get('pr')} state={found.get('state')}"
        )
        if found.get("state") == ESCALATED:
            return refuse("self-heal-escalated", detail)
        return refuse("self-heal-disarmed", detail)


# --- templates --------------------------------------------------------------------------


class TemplateFieldMissing(KeyError):
    """A field the template's front matter names was not supplied."""

    def __init__(self, template: str, names: list[str]) -> None:
        super().__init__(f"{template}: missing field(s) {', '.join(names)}")
        self.template = template
        self.names = names


def template(name: str) -> tuple[list[str], str]:
    """The template's front-matter ``fields`` and its body."""
    text = (resources.files("ai_chassis") / "defaults" / name).read_text("utf-8")
    _, head, body = text.split("---\n", 2)
    match = _FIELDS.search(head)
    names = [n.strip() for n in match.group("names").split(",")] if match else []
    return [n for n in names if n], body.rstrip("\n")


def _fit_lines(text: str, limit: int) -> str:
    """Every line under ``limit`` characters (one ``send-keys`` call per line in
    ``multiline`` mode): a longer paragraph is broken after its last sentence that
    fits, else its last space, as a soft newline. No word is changed."""
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) >= limit:
            cut = line.rfind(". ", 0, limit - 1)
            cut = cut + 1 if cut > 0 else line.rfind(" ", 0, limit - 1)
            if cut <= 0:
                break
            lines.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        lines.append(line)
    return "\n".join(lines)


def render(name: str, context: dict[str, Any], *, line_limit: int) -> str:
    """``str.format`` over the template body with exactly the fields its front matter
    names; a missing (or None) field fails naming it. The description is clipped."""
    fields, body = template(name)
    missing = [f for f in fields if context.get(f) is None]
    if missing:
        raise TemplateFieldMissing(name, missing)
    values = {f: context[f] for f in fields}
    if "description" in values:
        description = " ".join(str(values["description"]).split())
        values["description"] = description[:DESCRIPTION_MAX_CHARS]
    return _fit_lines(body.format(**values), line_limit)


def describe(record: dict[str, Any], signature: Signature) -> str:
    """The deterministic description (``--description auto``): the stage, the reason, and
    where the intended and seen text diverge, from the attempt's own evidence."""
    lead = f"{signature.stage} validation reported {signature.reason}"
    walk = record.get("walk_divergence")
    post = record.get("post_submit") or {}
    seen = post.get("divergence") or {}
    if isinstance(walk, dict):
        return (
            f"{lead}: the walk stitched row {walk.get('row')} as "
            f"{str(walk.get('row_text'))[:_ROW_CLIP]!r} where "
            f"{str(walk.get('expected'))[:_ROW_CLIP]!r} was intended, first divergent "
            f"character offset {walk.get('offset')}"
        )
    if seen.get("offset") is not None:
        return (
            f"{lead}: the transcript recorded {str(seen.get('recorded_row'))[:_ROW_CLIP]!r}"
            f" where {str(seen.get('intended_row'))[:_ROW_CLIP]!r} was intended (row "
            f"{seen.get('row')}, first divergent character offset {seen.get('offset')}, "
            f"{seen.get('recorded_chars')} of {seen.get('intended_chars')} characters)"
        )
    if seen.get("kind") == "delivered-as-text":
        return (
            f"{lead}: the command was recorded as plain user text, not as the command"
        )
    if signature.stage == "post-submit":
        counts = post.get("records_by_type") or {}
        return (
            f"{lead}: nothing after Enter resembled the submission (records read by "
            f"type: {json.dumps(counts, sort_keys=True)})"
        )
    detail = record.get("refusal_detail")
    return f"{lead}" + (f": {detail}" if detail else "")


# --- the re-arm guard -------------------------------------------------------------------


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(tree), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def library_path() -> Path:
    """The file this process imported the library from."""
    return Path(__file__).resolve()


def main_tree() -> Path | None:
    """The main working tree of the checkout the imported library lives in: the parent
    of the git common directory, which is the same for every worktree of the repo."""
    here = library_path().parent
    result = _git(here, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result is None or result.returncode != 0:
        return None
    return Path(result.stdout.strip()).parent


def _resolve_commit(tree: Path, commit: str) -> str | None:
    """The full SHA ``commit`` names in ``tree``, or ``None``. ``--end-of-options`` keeps
    an option-shaped value (``--output=<path>``) a revision, never a git flag."""
    result = _git(
        tree,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{commit}^{{commit}}",
    )
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip()


def _commit_paths(tree: Path, sha: str) -> list[str] | None:
    parent = _git(
        tree, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^1"
    )
    if parent is not None and parent.returncode == 0:
        argv = ["diff", "--name-only", "-z", "--end-of-options", parent.stdout.strip()]
    else:
        argv = ["show", "--name-only", "--format=", "-z", "--end-of-options"]
    # -z gives raw names, NUL-separated, read as bytes: without -z git C-quotes a name
    # holding a tab or newline, and text mode turns \r into \n or fails on a non-UTF-8
    # name, so the clean check would look for a file that does not exist.
    try:
        result = subprocess.run(
            ["git", "-C", str(tree), *argv, sha],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def rearm_guard(commit: str, tree: Path | None) -> tuple[list[str], str | None]:
    """Every failing predicate of the re-arm guard (D-18 (a)), and the SHA it checked.
    The failures are empty when it holds: the commit is an ancestor of the main tree's
    HEAD, the main tree has no uncommitted change under the commit's paths, and the
    library this process imported lives in that tree (so a synced main tree IS the
    operational library). ``commit`` is resolved exactly once; one that does not resolve
    to a commit fails outright, and git only ever sees the resolved SHA."""
    if tree is None:
        return ["main-tree: the imported library is not inside a git checkout"], None
    failures: list[str] = []
    sha = _resolve_commit(tree, commit)
    if sha is None:
        failures.append(f"commit: {commit!r} does not resolve to a commit in {tree}")
    else:
        ancestor = _git(
            tree, "merge-base", "--is-ancestor", "--end-of-options", sha, "HEAD"
        )
        if ancestor is None or ancestor.returncode != 0:
            failures.append(f"ancestor: {commit} is not an ancestor of {tree} HEAD")
        paths = _commit_paths(tree, sha)
        if paths is None:
            failures.append(f"paths: cannot read the paths {commit} changed in {tree}")
        elif paths:
            status = _git(
                tree,
                "-c",
                "core.quotePath=true",
                "--literal-pathspecs",
                "status",
                "--porcelain",
                "--",
                *paths,
            )
            dirty = status.stdout.strip() if status is not None else "unreadable"
            if status is None or status.returncode != 0 or dirty:
                failures.append(
                    f"clean: uncommitted change under the commit's paths: {dirty}"
                )
    imported = library_path()
    if not imported.is_relative_to(tree.resolve()):
        failures.append(
            f"operational: the imported library is {imported}, not under {tree}"
        )
    return failures, sha


# --- the loop ---------------------------------------------------------------------------

#: ``(use_case, failed attempt record, rendered text) -> InjectionOutcome``.
Deliver = Callable[[str, dict[str, Any], str], Any]


@dataclass
class Decision:
    """What one event did: the transition, the state after it, and for an injection the
    outcome of that injection. ``ready`` means a ``/bug-fix`` is due and waits for the
    caller's description (nothing changed yet)."""

    transition: str
    state: str
    signature: Signature | None
    round: int = 0
    max_rounds: int = 0
    reason: str | None = None
    ready: bool = False
    injection: Any = None
    detail: str = ""
    issue: str | None = None
    pr: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The ``self_heal=`` value the start-goal script prints."""
        if self.ready:
            return "ready"
        if self.transition in {"T1", "T7"}:
            return "injected"
        if self.state == ESCALATED:
            return "escalated"
        if self.transition == "T3":
            return "disarmed"
        return self.state

    def to_json(self) -> dict[str, Any]:
        injection = self.injection
        return {
            "self_heal": self.label,
            "transition": self.transition,
            "state": self.state,
            "signature": self.signature.sha if self.signature else None,
            "signature_human": self.signature.human if self.signature else None,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "reason": self.reason,
            "detail": self.detail,
            "issue": self.issue,
            "pr": self.pr,
            "injection": injection.to_json() if injection is not None else None,
            **self.extra,
        }


def _new_state(signature: Signature, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "signature": signature.human,
        "sha": signature.sha,
        "use_case": signature.use_case,
        "stage": signature.stage,
        "reason": signature.reason,
        "cc_version": signature.cc_version,
        "state": ARMED,
        "rounds": 0,
        "history": [],
        "created_at": now(),
        "key": record.get("key"),
    }


def _attempt_path(record: dict[str, Any]) -> str:
    stored = record.get("_path")
    if stored:
        return str(stored)
    return str(AttemptStore().path(str(record["key"]), str(record["attempt_id"])))


def _prefix(record: dict[str, Any]) -> str:
    return str(record.get("stage_prefix") or str(record.get("use_case", "")).upper())


def default_deliver(use_case: str, record: dict[str, Any], text: str) -> Any:
    """Type ``text`` into the failed attempt's own pane (D-17: the TARGET session, never
    the caller), walked and validated like any payload, under the same run id."""
    from ai_chassis.prompt_injection.injector import INJECTORS
    from ai_chassis.prompt_injection.payload import PromptPayload, SlashCommandPayload

    command = tuple(record.get("tmux_command") or ("tmux",))
    payload = (
        SlashCommandPayload(text, mode="multiline")
        if text.startswith("/")
        else PromptPayload(text, mode="multiline")
    )
    injector = INJECTORS[use_case](
        tmux_command=command,
        run_id=record.get("run_id"),
        parent_run_id=record.get("run_id"),
        parent_attempt_id=record.get("attempt_id"),
    )
    return injector.run(f"pane:{record['pane_id']}", payload)


class SelfHeal:
    """The loop's events against the store. ``deliver`` is the injection (a test passes a
    double); ``config`` is read at call time when not given."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        deliver: Deliver | None = None,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.deliver = deliver or default_deliver
        self.store = store or Store()

    def _config(self) -> dict[str, Any]:
        return self.config if self.config is not None else load_config()

    def _log(
        self,
        state: dict[str, Any],
        decision: Decision,
        run_id: str | None,
        key: str | None = None,
    ) -> None:
        detail = (
            f"state={decision.state} transition={decision.transition} "
            f"signature={state['sha']} round={decision.round} "
            f"max_rounds={decision.max_rounds} run={run_id}"
        )
        if decision.reason:
            detail += f" reason={decision.reason}"
        if decision.issue:
            detail += f" issue={decision.issue}"
        if decision.pr:
            detail += f" pr={decision.pr}"
        if decision.injection is not None:
            detail += f" injection_attempt={decision.injection.attempt_id}"
        write_stage_line(
            f"CC-{state.get('prefix') or state['use_case'].upper()}-SELF-HEAL",
            str(key or state.get("key") or "library"),
            detail,
        )
        if run_id:
            append_run_event(run_id, "self-heal", decision.to_json())

    def on_fail(
        self, record: dict[str, Any], *, description: str | None = None
    ) -> Decision | None:
        """The ``fail`` event for the attempt ``record`` ended in. None when it is no
        validation failure or its use case does not self-heal. With no ``description`` a
        due ``/bug-fix`` returns ``ready`` and changes nothing, so the calling session can
        write one; ``"auto"`` uses :func:`describe`."""
        config = self._config()
        signature = failure_of(record, config)
        if signature is None:
            return None
        enabled, max_rounds = self_heal_policy(config, signature.use_case)
        if not enabled:
            return None
        run_id = record.get("run_id")
        with self.store.locked(signature.sha):
            state = self.store.read(signature.sha) or _new_state(signature, record)
            state["prefix"] = _prefix(record)
            rounds = int(state.get("rounds", 0))
            move = step(state["state"], "fail", rounds=rounds, max_rounds=max_rounds)
            base = {
                "signature": signature,
                "max_rounds": max_rounds,
                "round": rounds,
            }
            if move.action == "inject-bug-fix" and description is None:
                return Decision(move.transition, state["state"], ready=True, **base)
            if move.action == "refuse":
                latest = (state.get("history") or [{}])[-1]
                decision = Decision(
                    move.transition,
                    state["state"],
                    reason=move.reason,
                    issue=latest.get("issue"),
                    pr=latest.get("pr"),
                    detail=f"pending round {rounds}",
                    **base,
                )
                self._log(state, decision, run_id, record.get("key"))
                return decision
            if move.action == "inject-bug-fix":
                return self._bug_fix(
                    state=state,
                    record=record,
                    signature=signature,
                    move=move,
                    max_rounds=max_rounds,
                    description=description or "auto",
                )
            return self._escalate(state, record, signature, move, max_rounds)

    def _bug_fix(  # noqa: PLR0913 - the transition's whole context
        self,
        *,
        state: dict[str, Any],
        record: dict[str, Any],
        signature: Signature,
        move: Step,
        max_rounds: int,
        description: str,
    ) -> Decision:
        round_number = int(state.get("rounds", 0)) + 1
        history = state.get("history") or []
        previous = history[-1] if history else None
        if description == "auto":
            description = describe(record, signature)
        context = {
            "delegation_target": default_delegation_target(),
            "description": description,
            "run_id": record.get("run_id"),
            "signature": signature.sha,
            "signature_human": signature.human,
            "round": round_number,
            "max_rounds": max_rounds,
            "stage": signature.stage,
            "reason": signature.reason,
            "run_record_path": f"{run_record_path(str(record.get('run_id')))}/",
            "attempt_path": _attempt_path(record),
            "key": record.get("key"),
            "previous_issue": (
                "none (first round)"
                if previous is None
                else previous.get("issue")
                or f"round {previous.get('round')} (its issue was not recorded)"
            ),
            "repo_main_tree": str(main_tree() or "the ai-harness main tree"),
            "target_session": record.get("key"),
        }
        text = render(BUG_FIX_TEMPLATE, context, line_limit=self._line_limit())
        outcome = self.deliver(SELF_HEAL_USE_CASE, record, text)
        entry = {
            "round": round_number,
            "run_id": record.get("run_id"),
            "attempt_id": record.get("attempt_id"),
            "attempt_path": context["attempt_path"],
            "injected_at": now(),
            "description": description,
            "injection_attempt_id": outcome.attempt_id,
            "injection_state": outcome.state,
            "injection_reason": outcome.reason,
            "issue": None,
            "pr": None,
        }
        # HELD at the save-state-to-compact boundary is delivered by its drain, so a repair
        # is underway either way.
        if not (outcome.submitted or outcome.state == "HELD"):
            # Nothing reached the target, so nobody is fixing: stay as we were.
            decision = Decision(
                "-",
                state["state"],
                signature,
                round=int(state.get("rounds", 0)),
                max_rounds=max_rounds,
                reason="self-heal-injection-refused",
                injection=outcome,
                detail=f"the /bug-fix injection was not submitted: {outcome.reason}",
            )
            write_notice(record, "self-heal-injection-refused", text)
            self._log(state, decision, record.get("run_id"), record.get("key"))
            return decision
        state.update(
            state=BUG_FIX_PENDING,
            rounds=round_number,
            history=[*history, entry],
            pending_run_id=record.get("run_id"),
            resume_argv=record.get("resume_argv"),
            pane_id=record.get("pane_id"),
            tmux_command=record.get("tmux_command"),
            key=record.get("key"),
            updated_at=now(),
        )
        self.store.write(state)
        decision = Decision(
            move.transition,
            BUG_FIX_PENDING,
            signature,
            round=round_number,
            max_rounds=max_rounds,
            injection=outcome,
            extra={"prompt": text},
        )
        self._log(state, decision, record.get("run_id"), record.get("key"))
        return decision

    def _escalate(
        self,
        state: dict[str, Any],
        record: dict[str, Any],
        signature: Signature,
        move: Step,
        max_rounds: int,
    ) -> Decision:
        history = state.get("history") or []
        issues = [str(h["issue"]) for h in history if h.get("issue")]
        attempts = [str(h["attempt_path"]) for h in history if h.get("attempt_path")]
        context = {
            "delegation_target": default_delegation_target(),
            "run_id": record.get("run_id"),
            "signature": signature.sha,
            "signature_human": signature.human,
            "rounds": int(state.get("rounds", 0)),
            "max_rounds": max_rounds,
            "round_issues": ", ".join(issues) or "none recorded",
            "run_record_path": f"{run_record_path(str(record.get('run_id')))}/",
            "attempt_paths": ", ".join([*attempts, _attempt_path(record)]),
            "target_session": record.get("key"),
        }
        text = render(ESCALATION_TEMPLATE, context, line_limit=self._line_limit())
        outcome = self.deliver(ESCALATION_USE_CASE, record, text)
        verified = (
            outcome.submitted and _attempt_verdict(outcome) == "post-submit-verified"
        )
        state.update(
            state=ESCALATED,
            escalated_at=now(),
            escalation_run_id=record.get("run_id"),
            escalation_attempt_id=outcome.attempt_id,
            escalation_state=outcome.state,
            escalation_validated=verified,
            updated_at=now(),
        )
        self.store.write(state)
        notice = write_notice(record, "self-heal-escalated", text)
        decision = Decision(
            move.transition,
            ESCALATED,
            signature,
            round=int(state.get("rounds", 0)),
            max_rounds=max_rounds,
            injection=outcome,
            reason=None if verified else "escalation-injection-failed",
            detail=(
                "escalation injected"
                if verified
                else f"the escalation injection did not validate ({outcome.reason}); "
                "it stays recorded in the notice and nothing more is injected"
            ),
            extra={"prompt": text, "notice": str(notice) if notice else None},
        )
        self._log(state, decision, record.get("run_id"), record.get("key"))
        return decision

    def _cap(self, state: dict[str, Any]) -> int:
        """The configured ``max_rounds`` for the state's use case, read now."""
        return self_heal_policy(self._config(), str(state["use_case"]))[1]

    def _line_limit(self) -> int:
        return int(self._config()["multiline_mode"]["chunk_chars"])

    def mark_fixed(  # noqa: PLR0913 - the CLI's flags
        self,
        ident: str,
        *,
        commit: str,
        issue: str | None,
        pr: str | None,
        tree: Path | None = None,
        resume: bool = True,
    ) -> Decision:
        """The only re-arm path: the guard performs the mutation. On ``T4`` the pending
        run is re-run (its ``resume_argv``) after the lock is released, so the retry's own
        validation can take the next transition."""
        state = self.store.find(ident)
        if state is None:
            return Decision("T11", ARMED, None, reason=REJECT_NOTHING_TO_REARM)
        sha = str(state["sha"])
        tree = tree if tree is not None else main_tree()
        with self.store.locked(sha):
            state = self.store.read(sha) or state
            # The state stores the SHA the guard checked, so a ref that moves, appears
            # or disappears mid-call cannot record a commit the guard never saw.
            failures, resolved = (
                rearm_guard(commit, tree)
                if state["state"] == BUG_FIX_PENDING
                else ([], None)
            )
            move = step(
                state["state"],
                "mark-fixed",
                rounds=int(state.get("rounds", 0)),
                max_rounds=self._cap(state),
                guard_ok=not failures,
            )
            signature = _signature(state)
            if move.action != "rearm":
                decision = Decision(
                    move.transition,
                    state["state"],
                    signature,
                    round=int(state.get("rounds", 0)),
                    max_rounds=self._cap(state),
                    reason=move.reason,
                    detail="; ".join(failures),
                )
                self._log(state, decision, state.get("pending_run_id"))
                return decision
            fixed = resolved or commit
            history = state.get("history") or []
            if history:
                history[-1].update(issue=issue, pr=pr, fixed_commit=fixed)
            state.update(
                state=RE_ARMED,
                fixed_commit=fixed,
                issue=issue,
                pr=pr,
                fixed_at=now(),
                main_tree=str(tree),
                history=history,
                updated_at=now(),
            )
            self.store.write(state)
            decision = Decision(
                "T4",
                RE_ARMED,
                signature,
                round=int(state.get("rounds", 0)),
                max_rounds=self._cap(state),
                issue=issue,
                pr=pr,
            )
            self._log(state, decision, state.get("pending_run_id"))
        if resume:
            decision.extra["resume"] = self._resume(state)
        return decision

    def _resume(self, state: dict[str, Any]) -> dict[str, Any]:
        argv = state.get("resume_argv")
        if not argv:
            return {"ran": False, "detail": "the run recorded no resume command"}
        try:
            done = subprocess.run(
                [str(part) for part in argv],
                capture_output=True,
                text=True,
                check=False,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"ran": False, "detail": f"{type(error).__name__}: {error}"}
        result = {
            "ran": True,
            "argv": argv,
            "returncode": done.returncode,
            "stdout": done.stdout[-4000:],
            "stderr": done.stderr[-2000:],
        }
        run_id = state.get("pending_run_id")
        if run_id:
            append_run_event(str(run_id), "resume", result)
        return result

    def clear(self, ident: str) -> Decision:
        """A human's reset after review: ``escalated`` (T10) or a never-marked
        ``bug-fix-pending`` (D-18's first Con) back to ``armed``."""
        state = self.store.find(ident)
        if state is None:
            return Decision("T11", ARMED, None, reason=REJECT_NOTHING_TO_REARM)
        sha = str(state["sha"])
        with self.store.locked(sha):
            state = self.store.read(sha) or state
            move = step(state["state"], "clear", rounds=0, max_rounds=self._cap(state))
            decision = Decision(
                move.transition,
                move.target,
                _signature(state),
                round=int(state.get("rounds", 0)),
                max_rounds=self._cap(state),
                reason=move.reason,
            )
            if move.action == "remove":
                self.store.remove(sha)
                decision.detail = "by=clear"
            self._log(state, decision, state.get("pending_run_id"))
            return decision

    def on_pass(self, record: dict[str, Any]) -> list[Decision]:
        """``pass``: the retried run (same run id, same use case) passed both
        validations, so every signature it re-armed returns to ``armed`` (T6)."""
        run_id = record.get("run_id")
        decisions: list[Decision] = []
        if not run_id:
            return decisions
        for found in self.store.all():
            if (
                found.get("pending_run_id") != run_id
                or found.get("use_case") != record.get("use_case")
                or found.get("state") != RE_ARMED
            ):
                continue
            sha = str(found["sha"])
            with self.store.locked(sha):
                state = self.store.read(sha)
                if state is None or state.get("state") != RE_ARMED:
                    continue
                move = step(RE_ARMED, "pass", rounds=0, max_rounds=self._cap(state))
                self.store.remove(sha)
                decision = Decision(
                    move.transition,
                    ARMED,
                    _signature(state),
                    round=int(state.get("rounds", 0)),
                    max_rounds=self._cap(state),
                    detail=f"passed attempt={record.get('attempt_id')}",
                )
                self._log(state, decision, run_id, record.get("key"))
                decisions.append(decision)
        return decisions

    def show(self, ident: str | None = None) -> list[dict[str, Any]]:
        if ident is None:
            return self.store.all()
        found = self.store.find(ident)
        return [found] if found is not None else []


def _signature(state: dict[str, Any]) -> Signature:
    return Signature(
        str(state["use_case"]),
        str(state["stage"]),
        str(state["reason"]),
        str(state["cc_version"]),
    )


def _attempt_verdict(outcome: Any) -> str | None:
    path = (getattr(outcome, "evidence", None) or {}).get("attempt")
    if path is None:
        return None
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record.get("post_submit_verdict") if isinstance(record, dict) else None
