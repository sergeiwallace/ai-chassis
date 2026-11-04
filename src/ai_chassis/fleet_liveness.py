"""Cross-machine CC-session liveness: one record, every heartbeat rung publishes it.

ISSUE-REF, Phase 3 of design `docs/designs/issue-ref-buzz-primary-cross-machine-messaging.md`
(§ Liveness heartbeat), as refined by D-4 (one channel-agnostic record, every available rung
publishes), D-12 (the git-inbox heartbeat lives on a per-machine ref at a 120 s cadence with a
300 s staleness bound) and the resolved Open Question 4 (agent mail carries a heartbeat through
`Agent.last_active_ts`, which the daemon's own `fetch_inbox` renews -- never a self-addressed
message).

This module owns RECORD CONSTRUCTION and the READER. It owns no daemon and no process: the one
`agent_mail_notifier` daemon calls `build_records` once per tick and hands the result to each
publisher, so the tri-state has ONE source (`Notifier._live_slugs`, which is
`claude_sessions.live_sessions`) rather than a second liveness notion per rung. That is the
whole point of the record being channel-shaped nowhere: `BuzzChannel.publish_presence` takes
these records, and `GitInboxPublisher` writes the same ones.

`None` FROM THE PROBE IS `undetermined`, NEVER `gone`. The existing D-8 discipline, restated
here because this module is where it becomes a published fact that other machines read: a
record says what this machine could PROVE about a session, and an unreadable `/proc` is not
evidence that nobody is home. No publisher here can emit `gone`; the reader says it in exactly
one case, :func:`scope_agent_mail`'s agent-mail identity that a fresh publisher record set for
its machine omits (D-23 (e)), and never for an `undetermined` reading.

THREE RUNGS, ONE RECORD:

* **buzz** -- `kind 20001` presence through `BuzzChannel.publish_presence` (Phase 2's seam).
  The relay's lease is never trusted; the reader ages the record's own `generated_at`.
* **git-inbox** -- one plumbing-built commit on `refs/heads/fleet-heartbeat/<machine>`, pushed
  with `--force-with-lease`. The zero-dependency floor: every machine has this remote.
  Contract in prose: `coordination/session-inbox/_heartbeat/README.md`.
* **agent-mail** -- READ-ONLY here. `config/message-transports.json` declares
  `"heartbeat": false` for it because nothing is published: the board's own `last_active_ts` is
  bumped by the daemon's `fetch_inbox`, so the heartbeat already exists and the reader only has
  to ask `whois` for it. The daemon renews it for every REGISTERED identity, ended sessions
  included, so a fresh stamp is a session heartbeat only when a fresh publisher record for the
  machine lists the session (:func:`scope_agent_mail`, D-23 (e)).

WHAT THE BUZZ RUNG ANSWERS. Two things, both through :func:`read_buzz`: THIS machine's own
publishes ("the daemon published presence for them at T", from its `inbox.json`), and every
PEER's `kind 20001` event the daemon verified and persisted to
`buzz_channel.presence_file_path(buzz_key_dir())`. `fleet-liveness status` passes that file; a
reader that omitted it would answer only for this machine while looking like a fleet view --
the narrower-question failure this module's `rung` field exists to make visible.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Imported rather than redefined, so there is ONE vocabulary for the tri-state across the
# rungs. `LIVENESS_IDLE` is deliberately NOT imported: `claude_sessions.live_sessions` cannot
# distinguish idle from live, so no publisher here can mint that word, and importing a constant
# this module can never emit would read as a capability it does not have. A reader that
# RECEIVES `idle` from a future publisher still passes it through untouched -- `Reading.liveness`
# is the record's own string and is never compared against a closed set.
from ai_chassis.buzz_channel import (
    LIVENESS_LIVE,
    LIVENESS_UNDETERMINED,
    daemon_identity_name,
    identity_name,
    parse_identity_parts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The TOML table, beside `[buzz_notify]` in `projects-wide-session-config/ai-harness.toml`.
CONFIG_SECTION = "fleet_liveness"

#: Where this machine's heartbeat ref lives. D-12 (b): a per-machine ref holding ONE
#: plumbing-built commit, never a commit on `main`.
DEFAULT_HEARTBEAT_REF_PREFIX = "refs/heads/fleet-heartbeat/"
#: The publish cadence and the git-inbox rung's own staleness bound (D-12): 300 s is 2.5x the
#: cadence, so one missed push does not read stale.
DEFAULT_GIT_INBOX_INTERVAL_S = 120
DEFAULT_GIT_INBOX_TTL_S = 300
#: The bound on any one git call (ISSUE-REF). Well under the publish cadence, so a hung
#: remote costs one rung one tick instead of freezing the whole notifier.
DEFAULT_GIT_TIMEOUT_S = 45
#: The `rc` a timed-out call reports, `timeout(1)`'s own convention.
GIT_TIMEOUT_RC = 124
#: How long a timed-out git gets after SIGTERM to remove its lock files before SIGKILL.
GIT_TERM_GRACE_S = 5
#: The same grace when the CALLER is being stopped (ISSUE-REF): the notifier has to exit inside
#: launchd's 5 s ExitTimeOut, so git gets one second to remove its locks, not five.
GIT_STOP_GRACE_S = 1
#: macOS ships `git` on PATH as an `xcrun` shim, not as git. Spawned from the notifier's
#: interpreter it was measured stalling in `_dyld_start` for 0-30 s per call (ISSUE-REF), so
#: every tick hit the bound above; the binary it forwards to starts in 0.01 s.
_XCRUN_SHIM_GIT = "/usr/bin/git"

DEFAULT_CONFIG: dict[str, Any] = {
    # ON by default, unlike `[buzz_notify]`. A heartbeat needs no relay, no key and no pair:
    # the git-inbox floor is the remote every machine already pushes to, so there is no
    # measurement left to make before it can run. `enabled = false` turns BOTH publishers off.
    "enabled": True,
    "git_inbox_interval_s": DEFAULT_GIT_INBOX_INTERVAL_S,
    "git_inbox_ttl_s": DEFAULT_GIT_INBOX_TTL_S,
    "heartbeat_ref_prefix": DEFAULT_HEARTBEAT_REF_PREFIX,
}

#: The one source word every published record carries, so a reader can tell a probe-derived
#: record from any future one that is not.
SOURCE_LOCAL_PROBE = "claude_sessions.live_sessions"
#: The agent-mail rung's reading is the board's own field, not this machine's probe.
SOURCE_AGENT_MAIL = "agent_mail.whois.last_active_ts"
#: Peer presence from `kind 20001` events the Buzz daemon received (Phase 3, ISSUE-REF).
SOURCE_BUZZ_PEER = "buzz.kind_20001"

#: The optional record field carrying the publisher's server-returned agent-mail board name
#: (D-22 (a)). Absent, never empty, when the identity is not registered on the board.
AGENT_MAIL_NAME_KEY = "agent_mail_name"

#: Rung names, as `config/message-transports.json` spells them.
RUNG_BUZZ = "buzz"
RUNG_GIT_INBOX = "git-inbox"
RUNG_AGENT_MAIL = "agent-mail"
#: The rungs that PUBLISH a machine's record set, so a fresh one can corroborate or contradict
#: an agent-mail stamp (D-23 (e)).
PUBLISHER_RUNGS = frozenset({RUNG_BUZZ, RUNG_GIT_INBOX})

#: What a reading is about. An agent-mail stamp with no fresh publisher record set for its
#: machine is `machine`-scoped: the daemon is alive and the identity is registered there,
#: and whether the SESSION is alive is unknown (D-23 (e)).
SCOPE_SESSION = "session"
SCOPE_MACHINE = "machine"
#: The one `gone` the reader can produce: an agent-mail identity a fresh publisher record set
#: for its machine omits (D-23 (e)). No publisher emits it.
LIVENESS_GONE = "gone"
#: The `gone` reason's prefix; the reason itself carries the omitting record's age.
REASON_ENDED_PER_PUBLISHER = "ended-per-publisher"
#: A stale `daemon@<machine>` record whose machine has a fresh session record (ISSUE-REF).
REASON_SUPERSEDED_BY_SESSIONS = "superseded-by-session-records"

#: The directory the records live in, inside the heartbeat ref's tree. Split because the tree
#: is built bottom-up with `mktree`, which writes ONE level at a time.
HEARTBEAT_DIR_PARTS = ("coordination", "session-inbox", "_heartbeat")
HEARTBEAT_DIR = "/".join(HEARTBEAT_DIR_PARTS)

#: Where a READER parks the fetched heartbeat refs. Deliberately NOT
#: `refs/remotes/<remote>/fleet-heartbeat/*`: that spelling embeds the remote's NAME in the
#: destination, so it breaks the moment the remote is given as a URL (which is how the tests
#: read a bare repository under `tmp_path`, and how a one-off read of a second remote would
#: work). A fixed namespace outside `refs/heads/` also keeps these refs invisible to
#: `worktree_audit.py`'s branch census on the reading side, independently of the publisher-side
#: exclusion.
HEARTBEAT_READ_NAMESPACE = "refs/fleet-heartbeat/"

#: Git's own spelling for a subdirectory entry in `mktree` input.
_TREE_MODE = "040000"
_BLOB_MODE = "100644"

#: The trailer `scripts/delegation_provenance_prepush.py` and the pre-commit back-stop require.
#: The publisher is a MACHINE, not a delegate, so `none` is the truthful value -- and it is
#: why this commit never needs `--no-verify` (ISSUE-REF).
DELEGATION_TRAILER = "Delegated-to: none"

_STALE_SUFFIX_S = "s"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class PresenceEvents:
    """D-8 presence on lifecycle change, with retained publication on failure.

    This path never ages a quiet session into absence. Reconnect/start replays
    the affected lifecycle obligation through the dispatcher, without a timer.
    Legacy heartbeat publishers remain selected on disabled hosts.
    """

    def __init__(self, root, publish):
        self.root, self.publish = Path(root), publish

    def handle(self, event, session, *, start_stamp=None):
        if event.source not in {"session-start", "session-end", "process-exit"}:
            return True
        from ai_chassis.wake.events import write_record

        path = self.root / session.slug / "presence-event.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("event") == event.identity and previous.get("published"):
            return True
        state = {
            "session-start": "live",
            "session-end": "ended",
            "process-exit": "exited",
        }[event.source]
        machine = (
            session.machine if event.origin_machine == "local" else event.origin_machine
        )
        record = {
            "identity": identity_name(machine, session.slug),
            "machine": machine,
            "slug": session.slug,
            "session": session.session_id,
            "state": state,
            "since": event.observed_at,
            "pid_start_stamp": start_stamp,
            "liveness": LIVENESS_LIVE if state == "live" else LIVENESS_GONE,
            "generated_at": _iso(dt.datetime.fromtimestamp(event.observed_at, dt.UTC)),
            "source": "presence-event",
        }
        row = {"event": event.identity, "record": record, "published": False}
        write_record(path, row)
        if self.publish(record):
            write_record(path, {**row, "published": True})
            return True
        return False

    @staticmethod
    def liveness(record, connected):
        if connected is not True:
            return LIVENESS_UNDETERMINED
        return LIVENESS_LIVE if record.get("state") == "live" else LIVENESS_GONE


def _iso(moment: dt.datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> dt.datetime | None:
    """A UTC-aware datetime, or None for anything this cannot read.

    None rather than a raise: every caller is a reader, and a record with an unreadable stamp
    must become `undetermined` rather than fail the whole status call. A NAIVE stamp is read as
    UTC, which is what upstream agent mail writes (`_utcnow_naive`).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


def load_config(
    path: Path | None = None, *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """The `[fleet_liveness]` table, with the XDG overlay (D-20), merged over :data:`DEFAULT_CONFIG`.

    Read through `agent_mail_notifier.read_config_tables`, the ONE loader every notifier table
    uses, so an operator's `[fleet_liveness].enabled = false` in
    `$XDG_CONFIG_HOME/ai-harness/ai-harness.local.toml` reaches both publishers (audit N-13).
    Typed through `merge_typed_section`, the same drop-to-default discipline: this runs inside
    a supervised daemon and one bad edit must not crash-loop the process that serves every rung.

    Imported lazily so this module can be imported BY the notifier without a cycle.
    """
    from ai_chassis.agent_mail_notifier import merge_typed_section, read_config_tables

    try:
        table = read_config_tables(path, env=env)
    except RuntimeError:
        return dict(DEFAULT_CONFIG)
    section = table.get(CONFIG_SECTION)
    if not isinstance(section, dict):
        return dict(DEFAULT_CONFIG)
    return merge_typed_section(section, DEFAULT_CONFIG)


def heartbeat_rungs(transports: Mapping[str, Any]) -> frozenset[str]:
    """Every rung `config/message-transports.json` declares `"heartbeat": true`.

    A rung named in the overlay's `disabled` list is NOT a heartbeat rung, so an operator who
    switched the floor off gets a machine that stops publishing on it (D-4's con mitigation)
    rather than one that keeps writing to a rung the ladder no longer uses.
    """
    disabled = {
        name for name in transports.get("disabled", []) or [] if isinstance(name, str)
    }
    rungs = transports.get("rungs", {}) or {}
    return frozenset(
        name
        for name, spec in rungs.items()
        if isinstance(spec, Mapping)
        and spec.get("heartbeat") is True
        and name not in disabled
    )


def heartbeat_ref(machine: str, *, prefix: str = DEFAULT_HEARTBEAT_REF_PREFIX) -> str:
    """This machine's heartbeat ref. One ref per machine, so no two publishers race."""
    return f"{prefix}{machine}"


def record_path(identity: str) -> str:
    """Where one record lives inside the heartbeat ref's tree."""
    return f"{HEARTBEAT_DIR}/{identity}.json"


def record_filename(identity: str) -> str:
    return f"{identity}.json"


# ---------------------------------------------------------------------------------------------
# T-3.1 The record
# ---------------------------------------------------------------------------------------------


def build_records(  # noqa: PLR0913  # keyword-only optional fields of the one record
    machine: str,
    live: set[str] | None,
    *,
    ttl_s: int = DEFAULT_GIT_INBOX_TTL_S,
    now: dt.datetime | None = None,
    source: str = SOURCE_LOCAL_PROBE,
    agent_mail_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One record per live slug, or ONE `undetermined` record when the probe is blind. T-3.1.

    `agent_mail_names` is `{fleet identity: server-returned board name}` (:func:`board_names`).
    A record whose identity is registered carries that name verbatim as `agent_mail_name`, so a
    peer that reads the record can `whois` it (D-22 (a)); an unregistered one carries no such
    key at all, never an empty or guessed name.

    `live is None` is the probe saying it could not tell, and it produces a single record under
    the machine daemon's identity rather than zero records: zero records is indistinguishable
    from "this machine has no sessions", which is the reassuring direction and the one a peer
    must never be allowed to infer. The record's `liveness` is `undetermined`; `gone` is not a
    word this function can produce for any input.

    An EMPTY set is a different answer and is published as such: the daemon's record ALONE,
    liveness `live`, because the probe positively enumerated none (audit N-18). Zero records
    would publish nothing at all, so no peer could ever read the machine's last session as
    ended; the daemon-only set is the omission source D-23 (e) reads, and the daemon record's
    liveness (`live` versus `undetermined`) is what keeps it apart from a blind probe.
    """
    generated_at = _iso(now or _utcnow())
    names = agent_mail_names or {}

    def record(identity: str, slug: str | None, liveness: str) -> dict[str, Any]:
        built: dict[str, Any] = {
            "identity": identity,
            "machine": machine,
            "slug": slug,
            "liveness": liveness,
            "generated_at": generated_at,
            "source": source,
            "ttl_s": int(ttl_s),
        }
        name = names.get(identity)
        if isinstance(name, str) and name:
            built[AGENT_MAIL_NAME_KEY] = name
        return built

    if live is None:
        return [record(daemon_identity_name(machine), None, LIVENESS_UNDETERMINED)]
    if not live:
        return [record(daemon_identity_name(machine), None, LIVENESS_LIVE)]
    return [
        record(identity_name(machine, slug), slug, LIVENESS_LIVE)
        for slug in sorted(live)
    ]


# ---------------------------------------------------------------------------------------------
# T-3.2 The publishers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    """One git invocation's outcome. `out` is stripped; `err` is kept for a refusal reason."""

    rc: int
    out: str = ""
    err: str = ""


GitRunner = Callable[[list[str], Path, str | None], GitResult]


@functools.lru_cache(maxsize=8)
def _resolve_git(search_path: str) -> str:
    found = shutil.which("git", path=search_path)
    if found is None:
        return "git"
    if sys.platform != "darwin" or os.path.realpath(found) != _XCRUN_SHIM_GIT:
        return found
    try:
        located = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/bin/xcrun", "--find", "git"],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    real = located.stdout.strip()
    if located.returncode == 0 and real and os.access(real, os.X_OK):
        return real
    return found


def git_executable() -> str:
    """The git binary `run_git` execs: the one on PATH, past the macOS `xcrun` shim.

    Resolved once per PATH value, so a test that puts a fake `git` first still gets it.
    """
    return _resolve_git(os.environ.get("PATH", os.defpath))


def _signal_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal `proc`'s process group (POSIX) or the process alone (Windows)."""
    if os.name == "nt":
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def run_git(
    args: list[str],
    repo: Path,
    stdin: str | None = None,
    *,
    timeout_s: float | None = None,
) -> GitResult:
    """Run one git command in `repo`. Never raises; a missing git is an `rc` like any other.

    The default :data:`GitRunner`. Injected in tests only so a refusal can be driven without a
    network: the git-inbox tests run REAL git against a bare repository under `tmp_path`.

    Every call is bounded (ISSUE-REF). This runs inside the notifier's tick, so one remote
    that never answers used to freeze the board heartbeat and every mark-read for as long as
    it hung (19 minutes measured on example-client-host-mac). A timeout is an `rc` like any other, and
    `GIT_TERMINAL_PROMPT=0` keeps git from waiting on a credential prompt nobody can answer.

    The one exception it lets through is the caller's own stop (ISSUE-REF), and only after
    git's process group has been sent SIGTERM and reaped.
    """
    bound = DEFAULT_GIT_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [git_executable(), *args],
            cwd=str(repo),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return GitResult(rc=127, err=f"{type(exc).__name__}: {exc.strerror or exc}")
    with proc:
        try:
            out, err = proc.communicate(input=stdin, timeout=bound)
        except subprocess.TimeoutExpired:
            # SIGTERM first: git deletes its own .lock files on the way out, and a SIGKILL
            # left refs/fleet-inbox-read/main.lock behind for every later fetch to refuse on.
            # The whole group, so a helper such as git-remote-https cannot keep the pipes open.
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.communicate(timeout=GIT_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                proc.communicate()
            return GitResult(
                rc=GIT_TIMEOUT_RC,
                err=f"git {' '.join(args[:1])} timed out after {bound:g}s",
            )
        except BaseException:
            # ISSUE-REF: the caller is being stopped mid-call. `with proc` would otherwise wait
            # for git to finish on its own, which a remote that never answers makes forever.
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=GIT_STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                proc.wait()
            raise
    return GitResult(
        rc=proc.returncode,
        out=(out or "").strip(),
        err=(err or "").strip(),
    )


@dataclass(frozen=True)
class PublishOutcome:
    """What one rung's publish did this tick.

    `sent` and `unsent` are not two views of one fact: a push that failed leaves the records
    COMMITTED locally and unsent, which is a state an operator has to be able to see. T-3.2's
    `If the push fails` clause is this dataclass -- `sent` stays False and every identity is
    named in `unsent`, so no caller can render the tick as delivered.
    """

    rung: str
    sent: bool
    reason: str = ""
    ref: str = ""
    commit: str = ""
    generated_at: str = ""
    identities: tuple[str, ...] = ()
    unsent: tuple[str, ...] = ()

    def as_record(self) -> dict[str, Any]:
        return {
            "rung": self.rung,
            "sent": self.sent,
            "reason": self.reason,
            "ref": self.ref,
            "commit": self.commit,
            "generated_at": self.generated_at,
            "identities": list(self.identities),
            "unsent": list(self.unsent),
        }


def _identities(records: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(str(record.get("identity") or "") for record in records)


#: Upper bound on a refusal reason, so one refused push cannot flood the daemon log.
REFUSAL_REASON_MAX = 300
_REFUSAL_SEPARATOR = " ... "
_URL_USERINFO = re.compile(r"(://)[^/@\s]+@")


def _refusal_reason(result: GitResult) -> str:
    """A failed git call's FIRST and LAST non-empty stderr lines, bounded and one line long.

    A refusing hook prints its own reason first and git prints `error: failed to push some
    refs` last, so the last line alone names the push and loses the cause (measured at T-5.6
    activation). Built from stderr only, with any URL userinfo masked, so a credential embedded
    in a remote URL never reaches the log.
    """
    lines = [
        _URL_USERINFO.sub(r"\1<redacted>@", line.strip())
        for line in result.err.splitlines()
        if line.strip()
    ]
    if not lines:
        return str(result.rc)
    kept = [lines[0]] if len(lines) == 1 else [lines[0], lines[-1]]
    separators = len(_REFUSAL_SEPARATOR) * (len(kept) - 1)
    budget = (REFUSAL_REASON_MAX - separators) // len(kept)
    clipped = [
        line if len(line) <= budget else line[: budget - 3] + "..." for line in kept
    ]
    return _REFUSAL_SEPARATOR.join(clipped)


@dataclass
class GitInboxPublisher:
    """The git-inbox rung's heartbeat publisher: plumbing only, `--force-with-lease`. T-3.2.

    PLUMBING, NOT `git commit`. The ref carries ONE commit rebuilt from scratch every publish
    (`hash-object` -> `mktree` -> `commit-tree` -> `update-ref` -> `push`), so it has no
    history to prune and never touches the index or the working tree of whichever checkout the
    daemon happens to run in. `git commit -m` would stage into that checkout's index, and a
    daemon mutating a session's index six times an hour is a defect, not a heartbeat.

    `--no-verify` IS NEVER PASSED, and the commit message carries
    :data:`DELEGATION_TRAILER` so the provenance back-stop accepts it on its own terms. This
    publisher only ever ADVANCES its ref and never deletes one, which is what keeps it clear of
    a deployment's ref-deletion guards; the fleet-specific hook analysis behind that claim is in
    `coordination/session-inbox/_heartbeat/README.md` rather than here, because naming a
    private installer's artifacts in this package is what `tests/test_install_parity.py`'s
    IN-89 row exists to catch.
    """

    repo: Path
    machine: str
    remote: str = "origin"
    ref_prefix: str = DEFAULT_HEARTBEAT_REF_PREFIX
    runner: GitRunner = run_git
    log: Callable[[str], None] = field(default=lambda _message: None)
    #: Epoch seconds of the last ATTEMPTED publish, so a relay of failures does not spin.
    last_publish_at: float | None = None

    @property
    def ref(self) -> str:
        return heartbeat_ref(self.machine, prefix=self.ref_prefix)

    def _git(self, args: list[str], stdin: str | None = None) -> GitResult:
        return self.runner(args, self.repo, stdin)

    def due(
        self,
        *,
        interval_s: int = DEFAULT_GIT_INBOX_INTERVAL_S,
        now: float | None = None,
    ) -> bool:
        """Whether `interval_s` has elapsed since the last ATTEMPT. The tick is faster."""
        moment = now if now is not None else _utcnow().timestamp()
        if self.last_publish_at is None:
            return True
        return moment - self.last_publish_at >= interval_s

    def _unsent(
        self,
        records: list[dict[str, Any]],
        reason: str,
        *,
        ref: str = "",
        commit: str = "",
    ) -> PublishOutcome:
        names = _identities(records)
        self.log(f"fleet-liveness git-inbox heartbeat unsent: {reason}")
        return PublishOutcome(
            rung=RUNG_GIT_INBOX,
            sent=False,
            reason=reason,
            ref=ref or self.ref,
            commit=commit,
            generated_at=str(records[0].get("generated_at") or "") if records else "",
            identities=names,
            unsent=names,
        )

    def _build_tree(self, records: list[dict[str, Any]]) -> tuple[str, str]:
        """The root tree holding every record at its designed path, or ("", reason)."""
        entries: list[str] = []
        for record in records:
            identity = str(record.get("identity") or "")
            if not identity:
                return "", "record-without-identity"
            payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
            blob = self._git(["hash-object", "-w", "--stdin"], payload)
            if blob.rc != 0 or not blob.out:
                return "", f"hash-object-failed ({blob.err or blob.rc})"
            entries.append(f"{_BLOB_MODE} blob {blob.out}\t{record_filename(identity)}")
        tree = self._git(["mktree"], "\n".join(sorted(entries)) + "\n")
        if tree.rc != 0 or not tree.out:
            return "", f"mktree-failed ({tree.err or tree.rc})"
        oid = tree.out
        # Wrap the leaf directory outwards, one level per `mktree`: the record's designed path
        # is three directories deep and `mktree` writes a single level.
        for component in reversed(HEARTBEAT_DIR_PARTS):
            wrapped = self._git(["mktree"], f"{_TREE_MODE} tree {oid}\t{component}\n")
            if wrapped.rc != 0 or not wrapped.out:
                return "", f"mktree-failed ({wrapped.err or wrapped.rc})"
            oid = wrapped.out
        return oid, ""

    def _lease(self) -> str:
        """`--force-with-lease` for this ref, against the REMOTE's current value.

        Read with `ls-remote` rather than from a remote-tracking ref: nothing fetches
        `refs/heads/fleet-heartbeat/*` on a publishing machine, so the tracking ref would be
        permanently absent and the lease would be vacuous. An empty expect value is git's
        spelling for "this ref must not exist yet", which is the first publish.
        """
        listed = self._git(["ls-remote", self.remote, self.ref])
        expected = ""
        if listed.rc == 0 and listed.out:
            expected = listed.out.split()[0]
        return f"--force-with-lease={self.ref}:{expected}"

    def publish(
        self, records: list[dict[str, Any]], *, now: float | None = None
    ) -> PublishOutcome:
        """Build the commit, point the ref at it, push it. A failed push marks every record unsent.

        The ORDER is the contract: nothing is pushed that was not built and recorded locally
        first, and the outcome reports `sent=True` only after the push itself returned 0. The
        README's rule is that a heartbeat you cannot `git show` was not published, so the
        publish result is a statement about the REMOTE, never about the local ref.
        """
        self.last_publish_at = now if now is not None else _utcnow().timestamp()
        if not records:
            return PublishOutcome(
                rung=RUNG_GIT_INBOX, sent=False, reason="no-records", ref=self.ref
            )
        tree, reason = self._build_tree(records)
        if not tree:
            return self._unsent(records, reason)
        stamp = str(records[0].get("generated_at") or _iso(_utcnow()))
        commit = self._git(
            [
                "commit-tree",
                tree,
                "-m",
                f"fleet heartbeat {self.machine} {stamp}",
                "-m",
                DELEGATION_TRAILER,
            ]
        )
        if commit.rc != 0 or not commit.out:
            return self._unsent(
                records, f"commit-tree-failed ({commit.err or commit.rc})"
            )
        updated = self._git(["update-ref", self.ref, commit.out])
        if updated.rc != 0:
            return self._unsent(
                records,
                f"update-ref-failed ({updated.err or updated.rc})",
                commit=commit.out,
            )
        pushed = self._git(
            ["push", self._lease(), self.remote, f"{self.ref}:{self.ref}"]
        )
        if pushed.rc != 0:
            return self._unsent(
                records, f"push-failed ({_refusal_reason(pushed)})", commit=commit.out
            )
        names = _identities(records)
        return PublishOutcome(
            rung=RUNG_GIT_INBOX,
            sent=True,
            ref=self.ref,
            commit=commit.out,
            generated_at=stamp,
            identities=names,
        )


# ---------------------------------------------------------------------------------------------
# T-3.3 / T-3.4 The reader
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One rung's answer about one identity, with the age that decides whether it is believed.

    `rung` is as load-bearing as `liveness`: "live according to the git floor 40 s ago" and
    "live according to a rung that only knows about this machine" are different claims, and a
    status line that does not name the rung cannot be checked by the reader of it.
    """

    identity: str
    rung: str
    liveness: str
    generated_at: str
    source: str = ""
    machine: str = ""
    slug: str | None = None
    ttl_s: int = DEFAULT_GIT_INBOX_TTL_S
    age_s: int | None = None
    #: The board name the PUBLISHER put in its record (D-22 (a)), or None when it put none.
    agent_mail_name: str | None = None
    #: `session`, or `machine` for an uncorroborated agent-mail stamp (D-23 (e)).
    scope: str = SCOPE_SESSION
    #: Why the reader overrode the rung's own word, e.g. `ended-per-publisher`; else empty.
    reason: str = ""
    #: For `gone`, the age of the git-inbox record that omitted the identity; else None.
    record_age_s: int | None = None

    @property
    def status(self) -> str:
        """`live|idle|undetermined|stale(<age>)|gone`, or `live (machine)` for a machine scope.

        A record past its ttl is NEVER `live`. An UNREADABLE age is `undetermined` rather than
        stale: "I cannot age this record" is not the same finding as "this record is old", and
        only one of them is about the remote session.
        """
        if self.source == "presence-event":
            return self.liveness
        if self.age_s is None:
            return LIVENESS_UNDETERMINED
        if self.age_s > self.ttl_s:
            return f"stale({self.age_s}{_STALE_SUFFIX_S})"
        if self.scope == SCOPE_MACHINE:
            return f"{self.liveness} ({SCOPE_MACHINE})"
        return self.liveness

    @property
    def fresh(self) -> bool:
        """Aged and within this rung's own staleness bound."""
        if self.source == "presence-event":
            return self.liveness != LIVENESS_UNDETERMINED
        return self.age_s is not None and self.age_s <= self.ttl_s

    def as_record(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "rung": self.rung,
            "status": self.status,
            "liveness": self.liveness,
            "generated_at": self.generated_at,
            "source": self.source,
            "machine": self.machine,
            "slug": self.slug,
            "ttl_s": self.ttl_s,
            "age_s": self.age_s,
            AGENT_MAIL_NAME_KEY: self.agent_mail_name,
            "scope": self.scope,
            "reason": self.reason,
            "record_age_s": self.record_age_s,
        }


def _aged(
    record: Mapping[str, Any],
    *,
    rung: str,
    ttl_s: int,
    now: dt.datetime,
    stamp_key: str = "generated_at",
) -> Reading:
    generated_at = str(record.get(stamp_key) or "")
    moment = _parse_iso(generated_at)
    age_s = None if moment is None else max(0, int((now - moment).total_seconds()))
    identity = str(record.get("identity") or "")
    declared = record.get("ttl_s")
    board_name = record.get(AGENT_MAIL_NAME_KEY)
    return Reading(
        identity=identity,
        rung=rung,
        liveness=str(record.get("liveness") or LIVENESS_UNDETERMINED),
        generated_at=generated_at,
        source=str(record.get("source") or ""),
        machine=str(record.get("machine") or parse_identity_parts(identity)[1]),
        slug=record.get("slug") if isinstance(record.get("slug"), str) else None,
        ttl_s=int(declared)
        if isinstance(declared, int) and not isinstance(declared, bool)
        else ttl_s,
        age_s=age_s,
        agent_mail_name=board_name
        if isinstance(board_name, str) and board_name
        else None,
    )


def _read_one_heartbeat_ref(
    ref: str,
    *,
    git: Callable[[list[str]], GitResult],
    ttl_s: int,
    now: dt.datetime,
) -> tuple[list[Reading], str]:
    """Every record held by ONE machine's heartbeat ref, and the first reason one was skipped.

    Split out of :func:`read_git_inbox` so the loop over machines and the read of one machine's
    tree are separately readable: a record this cannot parse must be SKIPPED with a reason and
    must not take the other machines' readings down with it.
    """
    readings: list[Reading] = []
    tree = git(["ls-tree", "-r", "--name-only", ref, f"{HEARTBEAT_DIR}/"])
    if tree.rc != 0:
        return readings, f"ls-tree-failed ({tree.err or tree.rc})"
    reason = ""
    for raw in tree.out.splitlines():
        path = raw.strip()
        if not path.endswith(".json"):
            continue
        shown = git(["show", f"{ref}:{path}"])
        if shown.rc != 0:
            reason = reason or f"show-failed ({shown.err or shown.rc})"
            continue
        try:
            record = json.loads(shown.out)
        except json.JSONDecodeError:
            reason = reason or f"unreadable-record ({path})"
            continue
        if isinstance(record, dict):
            readings.append(_aged(record, rung=RUNG_GIT_INBOX, ttl_s=ttl_s, now=now))
    return readings, reason


def read_git_inbox(  # noqa: PLR0913  # one keyword per injected seam; a bundle would hide them
    repo: Path,
    *,
    remote: str = "origin",
    ref_prefix: str = DEFAULT_HEARTBEAT_REF_PREFIX,
    ttl_s: int = DEFAULT_GIT_INBOX_TTL_S,
    runner: GitRunner = run_git,
    fetch: bool = True,
    now: dt.datetime | None = None,
) -> tuple[list[Reading], str]:
    """Every machine's heartbeat records off the remote, and a reason when there are none.

    The refspec fetches `refs/heads/fleet-heartbeat/*` into
    :data:`HEARTBEAT_READ_NAMESPACE`. `remote` is injectable and may be a NAME or a URL, which
    is how the tests read a bare repository under `tmp_path`: there is no code path here that
    reaches this fleet's real origin when a remote was supplied.

    A fetch that fails is a REASON, not an empty success: the caller still reads whatever the
    last fetch left behind, and says the refresh failed. Reporting zero identities for an
    unreachable remote would read as "nobody is alive anywhere", the reassuring direction.
    """
    moment = now or _utcnow()
    reason = ""

    def git(args: list[str]) -> GitResult:
        return runner(args, repo, None)

    if fetch:
        fetched = git(
            [
                "fetch",
                "--quiet",
                "--prune",
                remote,
                f"+{ref_prefix}*:{HEARTBEAT_READ_NAMESPACE}*",
            ]
        )
        if fetched.rc != 0:
            reason = f"fetch-failed ({fetched.err or fetched.rc})"
    listed = git(["for-each-ref", "--format=%(refname)", HEARTBEAT_READ_NAMESPACE])
    if listed.rc != 0:
        return [], reason or f"for-each-ref-failed ({listed.err or listed.rc})"
    readings: list[Reading] = []
    for raw in listed.out.splitlines():
        ref = raw.strip()
        if not ref:
            continue
        found, ref_reason = _read_one_heartbeat_ref(
            ref, git=git, ttl_s=ttl_s, now=moment
        )
        readings.extend(found)
        reason = reason or ref_reason
    return readings, reason


def _records_by_identity(records: Any) -> dict[str, Mapping[str, Any]]:
    """`{identity: record}` over a published record list; anything malformed is skipped."""
    if not isinstance(records, list):
        return {}
    return {
        str(record["identity"]): record
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("identity"), str)
    }


def read_buzz(  # noqa: PLR0912, PLR0915, C901  # legacy and lifecycle presence coexist until cutover
    liveness_state: Mapping[str, Any] | None,
    *,
    presence_file: Path | None = None,
    stale_after_s: int,
    now: dt.datetime | None = None,
) -> tuple[list[Reading], str]:
    """Buzz presence: this machine's publishes AND peer presence from `kind 20001` events.

    The daemon's `inbox.json` `liveness` object is the source for this machine's own identities.
    Peer presence records (Phase 3, ISSUE-REF) are read from the presence file when provided:
    the daemon subscribes to `KIND_PRESENCE` and persists each verified peer event, keyed by
    identity and aged by `generated_at`.

    Staleness is `liveness.stale_after_s` from `config/message-transports.json` (180 s), never
    the relay's own lease -- which OQ-7 measured at 90 s against 180 s documented, and which
    the fleet does not trust either way.
    """
    moment = now or _utcnow()
    readings: list[Reading] = []
    reason = ""
    # No local daemon state is a REASON, never a return: a reader whose own daemon has not
    # ticked must still see the peers the presence file holds.
    publishes: list[Any] = []
    if not isinstance(liveness_state, Mapping):
        reason = "no-daemon-state"
    elif not isinstance(liveness_state.get("publishes"), list):
        reason = "no-publish-record"
    else:
        publishes = liveness_state["publishes"]
    # This machine's own identities, from the daemon's publish records. The tri-state is the
    # PUBLISHED record's own (audit N-14): the outcome's kept records first, else the tick's
    # `records` joined by identity, else `undetermined`. Never a `live` this reader minted.
    published: dict[str, Mapping[str, Any]] = {}
    if isinstance(liveness_state, Mapping):
        published.update(_records_by_identity(liveness_state.get("records")))
    for entry in publishes:
        if not isinstance(entry, Mapping) or entry.get("rung") != RUNG_BUZZ:
            continue
        if not entry.get("sent"):
            reason = str(entry.get("reason") or "buzz-unsent")
            continue
        kept = {**published, **_records_by_identity(entry.get("records"))}
        stamp = str(entry.get("generated_at") or "")
        for identity in entry.get("identities", []) or []:
            if not isinstance(identity, str) or not identity:
                continue
            record = kept.get(identity, {})
            readings.append(
                _aged(
                    {
                        **record,
                        "identity": identity,
                        "liveness": record.get("liveness") or LIVENESS_UNDETERMINED,
                        "generated_at": record.get("generated_at") or stamp,
                        "source": record.get("source") or SOURCE_LOCAL_PROBE,
                        # Buzz ages by the ladder's bound, not the git-inbox ttl the record
                        # was built with.
                        "ttl_s": stale_after_s,
                    },
                    rung=RUNG_BUZZ,
                    ttl_s=stale_after_s,
                    now=moment,
                )
            )
    # Peer presence records, from `kind 20001` events the daemon received and persisted.
    if presence_file is not None:
        try:
            import json

            document = json.loads(presence_file.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                for identity, entry in document.items():
                    if not isinstance(entry, dict):
                        continue
                    # The record already holds the parsed identity, machine, slug, liveness.
                    liveness = entry.get("liveness")
                    generated_at = entry.get("generated_at")
                    machine = entry.get("machine")
                    slug = entry.get("slug")
                    if not isinstance(liveness, str) or not isinstance(
                        generated_at, int
                    ):
                        continue
                    if not isinstance(machine, str):
                        continue
                    # `slug` may be None (for `daemon@<machine>`), which is valid.
                    if slug is not None and not isinstance(slug, str):
                        continue
                    # Convert the integer timestamp to an ISO string for _aged.
                    stamp = dt.datetime.fromtimestamp(
                        generated_at, tz=dt.UTC
                    ).isoformat()
                    change = entry.get("presence_event")
                    if isinstance(change, dict):
                        connections = (liveness_state or {}).get("host_connections", {})
                        readings.append(
                            _aged(
                                {
                                    "identity": identity,
                                    "machine": machine,
                                    "slug": slug,
                                    "source": "presence-event",
                                    "generated_at": stamp,
                                    "liveness": PresenceEvents.liveness(
                                        change, connections.get(machine)
                                    ),
                                },
                                rung=RUNG_BUZZ,
                                ttl_s=stale_after_s,
                                now=moment,
                            )
                        )
                        continue
                    readings.append(
                        _aged(
                            {
                                "identity": identity,
                                "liveness": liveness,
                                "generated_at": stamp,
                                "source": SOURCE_BUZZ_PEER,
                                "machine": machine,
                                "slug": slug,
                                # D-22 (a): the name the peer published, so it can be whois-ed.
                                AGENT_MAIL_NAME_KEY: entry.get(AGENT_MAIL_NAME_KEY),
                            },
                            rung=RUNG_BUZZ,
                            ttl_s=stale_after_s,
                            now=moment,
                        )
                    )
        except (OSError, ValueError):
            # An unreadable presence file means no peer records, not an error: the rung still
            # answers for this machine's own identities.
            pass
    if not readings and not reason:
        reason = "no-buzz-presence"
    return readings, reason


#: Upstream agent mail's field, and the only one T-3.4 reads. OQ-4: `Agent.last_active_ts` is
#: bumped by the daemon's own `fetch_inbox` each tick, so the heartbeat already exists.
AGENT_MAIL_LAST_ACTIVE_KEY = "last_active_ts"


def _find_key(value: Any, target: str) -> Any:
    """`target`'s value anywhere in a nested MCP response, or None.

    The shape of `whois`'s response was NOT read at the pinned commit (OQ-4 says so
    explicitly), so this walks rather than indexes: an absent field is "no heartbeat on this
    rung" and a differently-nested one is still found. Never raises on a shape it did not
    expect.
    """
    if isinstance(value, Mapping):
        if target in value:
            return value[target]
        for nested in value.values():
            found = _find_key(nested, target)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_key(item, target)
            if found is not None:
                return found
    return None


def read_agent_mail(
    identities: Mapping[str, str] | None,
    *,
    whois: Callable[[str], Any] | None,
    stale_after_s: int,
    now: dt.datetime | None = None,
) -> tuple[list[Reading], str]:
    """One reading per identity whose `whois` profile carries `last_active_ts`. T-3.4.

    `Where` the field is present it IS this rung's record, renewed implicitly by the daemon's
    own `fetch_inbox` -- so nothing is published here and NO MESSAGE IS SENT. D-4 rejected a
    periodic self-addressed message as noise, and this function's whole surface is a read:
    `whois` is the only board call it can make.

    An ABSENT field is "no heartbeat on this rung", never an error (OQ-4's explicit
    instruction, because the response shape was not read at the pin): it is the rung's reason
    only when NO identity carried one, so a peer name the board no longer lists does not mark
    a rung that answered for every other name (ISSUE-REF). A `whois` that raises is a reason,
    and the rung simply contributes nothing for that name.
    """
    moment = now or _utcnow()
    if whois is None:
        return [], "board-not-configured"
    if not identities:
        return [], "no-registered-identities"
    readings: list[Reading] = []
    reason = ""
    for identity, board_name in sorted(identities.items()):
        try:
            profile = whois(board_name or identity)
        except Exception as exc:  # noqa: BLE001 - an unreachable board is a reported state
            reason = reason or f"whois-failed ({type(exc).__name__})"
            continue
        stamp = _find_key(profile, AGENT_MAIL_LAST_ACTIVE_KEY)
        if not isinstance(stamp, str) or not stamp:
            continue
        readings.append(
            _aged(
                {
                    "identity": identity,
                    "liveness": LIVENESS_LIVE,
                    "generated_at": stamp,
                    "source": SOURCE_AGENT_MAIL,
                },
                rung=RUNG_AGENT_MAIL,
                ttl_s=stale_after_s,
                now=moment,
            )
        )
    if not readings and not reason:
        reason = "no-last-active-ts"
    return readings, reason


def scope_agent_mail(readings: Iterable[Reading]) -> list[Reading]:
    """Say what a fresh agent-mail stamp is evidence OF, from the publisher rungs. D-23 (e).

    The daemon's `fetch_inbox` renews `last_active_ts` for every identity registered on its
    machine, ended sessions included, so a fresh stamp proves the machine's daemon is alive and
    the identity is registered -- not that the session is. Against each machine's FRESH
    publisher records (git-inbox, Buzz; each by its own staleness bound; a blind probe's
    `undetermined` record enumerates nothing, while a positive empty census's daemon-only
    `live` set omits every session of its machine):

    * the machine daemon's own identity -> `live (machine)`;
    * any fresh record of any rung lists the identity -> `live`, session-scoped;
    * a fresh git-inbox set omits it -> `gone`, reason `ended-per-publisher (record age <N>s)`
      and `record_age_s` = the age of the machine's freshest git-inbox record;
    * otherwise -> `live (machine)`.

    The stamp's own age is no evidence either way: the daemon renews it for every registered
    identity on every tick, so it is nearly always newer than the set. A session that started
    after the set was published therefore reads `gone` for at most one publish cadence, and the
    record age in the reason is what lets a reader tell that transient from an ending.

    Only git-inbox can omit: it is the machine's full session list (D-12), while the Buzz
    presence store files a keyless session under `daemon@<machine>`, so a Buzz set that does
    not name an identity says nothing about it. Only a fresh `live` agent-mail reading is
    touched: a stale stamp stays `stale(<age>)`, an unreadable one stays `undetermined`, and
    every other rung's reading passes through as is.
    """
    readings = list(readings)
    listed: dict[str, set[str]] = {}
    inbox_age: dict[str, int] = {}
    for reading in readings:
        if (
            reading.rung not in PUBLISHER_RUNGS
            or not reading.fresh
            or reading.liveness == LIVENESS_UNDETERMINED
        ):
            continue
        listed.setdefault(reading.machine, set()).add(reading.identity)
        if reading.rung == RUNG_GIT_INBOX and reading.age_s is not None:
            inbox_age[reading.machine] = min(
                reading.age_s, inbox_age.get(reading.machine, reading.age_s)
            )
    scoped: list[Reading] = []
    for reading in readings:
        if reading.rung != RUNG_AGENT_MAIL or reading.status != LIVENESS_LIVE:
            scoped.append(reading)
            continue
        record_age_s = inbox_age.get(reading.machine)
        if reading.identity == daemon_identity_name(reading.machine):
            scoped.append(replace(reading, scope=SCOPE_MACHINE))
        elif reading.identity in listed.get(reading.machine, set()):
            scoped.append(reading)
        elif record_age_s is None:
            scoped.append(replace(reading, scope=SCOPE_MACHINE))
        else:
            scoped.append(
                replace(
                    reading,
                    liveness=LIVENESS_GONE,
                    reason=f"{REASON_ENDED_PER_PUBLISHER} (record age {record_age_s}s)",
                    record_age_s=record_age_s,
                )
            )
    return scoped


def merge_readings(readings: Iterable[Reading]) -> dict[str, Reading]:
    """The FRESHEST reading per identity. T-3.3.

    Freshest by measured age, and a reading whose age could not be measured loses to one that
    could: an unreadable stamp must not displace a rung that actually answered. With no
    measurable age anywhere the identity still appears, reporting `undetermined`.
    """
    best: dict[str, Reading] = {}
    for reading in readings:
        if not reading.identity:
            continue
        current = best.get(reading.identity)
        if current is None:
            best[reading.identity] = reading
            continue
        if reading.age_s is None:
            continue
        if current.age_s is None or reading.age_s < current.age_s:
            best[reading.identity] = reading
    return best


def ladder_stale_after_s(transports: Mapping[str, Any]) -> int:
    """`liveness.stale_after_s` from the transport ladder, or the shipped default."""
    from ai_chassis.message_transports import DEFAULT_CONFIG

    value = (transports.get("liveness") or {}).get("stale_after_s")
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_CONFIG["liveness"]["stale_after_s"]
    return value


def daemon_liveness_state() -> dict[str, Any] | None:
    """The `liveness` object this machine's notifier wrote on its last tick, or None."""
    from ai_chassis.agent_mail_notifier import StateDir, state_root

    try:
        record = json.loads(StateDir(state_root()).inbox.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    liveness = record.get("liveness") if isinstance(record, dict) else None
    return liveness if isinstance(liveness, dict) else None


def live_buzz_sessions(
    liveness_state: Mapping[str, Any] | None,
    *,
    presence_file: Path | None,
    stale_after_s: int,
    now: dt.datetime | None = None,
) -> dict[str, list[str]]:
    """machine -> the session identities the Buzz rung reads as `live` now, sorted.

    What `buzz send --to *@<host>` expands to (ISSUE-REF): this machine's own published records
    and every peer's persisted presence, aged by the ladder's bound exactly as `fleet-liveness
    status` ages them. The daemon record names no session and is never a recipient.
    """
    readings, _reason = read_buzz(
        liveness_state,
        presence_file=presence_file,
        stale_after_s=stale_after_s,
        now=now,
    )
    out: dict[str, list[str]] = {}
    for identity, reading in sorted(merge_readings(readings).items()):
        if reading.slug and reading.status == LIVENESS_LIVE:
            out.setdefault(reading.machine, []).append(identity)
    return out


def _mark_superseded_daemons(merged: dict[str, Reading]) -> dict[str, Reading]:
    """Say why a stale daemon record beside fresh sessions is stale. ISSUE-REF.

    `build_records` publishes `daemon@<machine>` only for an empty or blind census (audit
    N-18), so the moment a session publishes, the daemon's last record stops refreshing while
    the daemon keeps running. Its status stays `stale(...)`, because the record IS old; the
    reason says a fresh session record from the same machine replaced it. A stale daemon with
    no fresh session beside it keeps no reason: that machine really has gone quiet.
    """
    fresh_machines = {
        reading.machine
        for reading in merged.values()
        if reading.fresh and reading.identity != daemon_identity_name(reading.machine)
    }
    return {
        identity: replace(reading, reason=REASON_SUPERSEDED_BY_SESSIONS)
        if identity == daemon_identity_name(reading.machine)
        and reading.age_s is not None
        and not reading.fresh
        and reading.machine in fresh_machines
        else reading
        for identity, reading in merged.items()
    }


def status_report(
    readings: Iterable[Reading],
    *,
    machine: str | None = None,
    session: str | None = None,
    rung_reasons: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """The `fleet-liveness status` document: freshest record per identity, with its rung.

    `rung_reasons` is carried into the output rather than dropped, because "this rung said
    nothing and here is why" is the answer to the question an operator actually has when an
    identity is missing. An identity absent from every rung is reported as absent, never as
    `gone`.
    """
    merged = _mark_superseded_daemons(merge_readings(readings))
    selected = sorted(merged.values(), key=lambda reading: reading.identity)
    if machine:
        selected = [reading for reading in selected if reading.machine == machine]
    if session:
        selected = [reading for reading in selected if reading.identity == session]
    return {
        "generated_at": _iso(now or _utcnow()),
        "identities": [reading.as_record() for reading in selected],
        "rung_reasons": dict(rung_reasons or {}),
        "filters": {"machine": machine or None, "session": session or None},
    }


# ---------------------------------------------------------------------------------------------
# The daemon's one call site
# ---------------------------------------------------------------------------------------------


def board_names(stored: Mapping[str, Any], machine: str) -> dict[str, str]:
    """`{fleet identity: server-returned board name}` from an `identities.json` document.

    The file is keyed by SLUG, plus the machine's own registration under the notifier's
    `MACHINE_KEY`; every other reserved `__` key (the denial ledger) is skipped. The machine
    registration names the `daemon@<machine>` record, the identity a blind probe publishes.
    Keyed by fleet identity so it joins the records every rung carries.
    """
    from ai_chassis.agent_mail_notifier import MACHINE_KEY

    names: dict[str, str] = {}
    for key, value in stored.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            continue
        if key == MACHINE_KEY:
            names[daemon_identity_name(machine)] = value
        elif not key.startswith("__"):
            names[identity_name(machine, key)] = value
    return names


def state_identities(state_path: Path, machine: str) -> dict[str, str]:
    """:func:`board_names` of the notifier's `identities.json`, or `{}` when unreadable."""
    try:
        stored = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return board_names(stored, machine)


def peer_board_names(readings: Iterable[Reading]) -> dict[str, str]:
    """`{identity: agent_mail_name}` from every reading whose record carried one (D-22 (a))."""
    return {
        reading.identity: reading.agent_mail_name
        for reading in readings
        if reading.identity and reading.agent_mail_name
    }


def default_remote(env: Mapping[str, str] | None = None) -> str:
    """The remote the heartbeat is published to. One env override, for a test host."""
    source = env if env is not None else os.environ
    return str(source.get("AIH_FLEET_LIVENESS_REMOTE") or "origin")
