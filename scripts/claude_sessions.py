#!/usr/bin/env python3
# ruff: noqa: E402
"""Discover live Claude Code sessions from process titles."""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import datetime
import json
import math
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic

# Deliberately the same grammar the naming hooks admit, so a title this module
# returns is always usable as both a path component and a git ref.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

DEFAULT_PROC_ROOT = "/proc"
PROC_ROOT_ENV = "CLAUDE_SESSION_PROC_ROOT"
REGISTRY_DIR_ENV = "CLAUDE_SESSION_REGISTRY_DIR"
TITLE_OVERRIDE_ENV = "CLAUDE_WORKTREE_SESSION_TITLE"
# pytest exports this into every process a test spawns, for the test's whole duration.
TEST_RUN_ENV = "PYTEST_CURRENT_TEST"
TASK_LIST_ID_ENV = "CLAUDE_CODE_TASK_LIST_ID"
# The registry record's `nameSource` when Claude Code picked a title other than the requested
# `--name` because that name was already taken (measured: example-client-host-mac, 2026-10-21, ISSUE-REF).
COLLISION_NAME_SOURCE = "collision"
AI_CLI_LAUNCHER_PREFIXES = (
    "cx-r-",
    "c-r-",
    "g-r-",
    "p-r-",
    "cx-",
    "c-",
    "g-",
    "p-",
)

# Program-name basenames that identify a Claude Code session process. Compared against a
# `comm`, a `psutil.Process.name()` and an `argv[0]` alike -- see IDENTITY below.
CLAUDE_PROGRAM_NAMES = frozenset({"claude"})
NAME_FLAG = "--name"

# The ancestry walk is bounded so a cycle or a pathological chain cannot hang a
# hook in a launch's critical path. Measured real depth from a tool child to the
# session process: 2.
MAX_ANCESTRY_DEPTH = 40

# Field 22 of /proc/<pid>/stat, zero-based within the tail that follows "<comm>) ".
# That tail begins at field 3, so field 22 is index 19 and ppid (field 4) is index 1.
# Field 3 itself -- the process state char -- is therefore index 0.
_STAT_PPID_INDEX = 1
_STAT_STATE_INDEX = 0
_STAT_STARTTIME_INDEX = 19
_STAT_MIN_FIELDS = 20
_SCRIPT_ARG_INDEX = 1

# `/proc/<pid>/stat` field 3 values that mean the process is not running any more. A
# zombie has been reaped by the kernel but not yet by its parent: it holds no cwd, no fds
# and no working directory, so it can own nothing. Treating it as live is the same defect
# as treating a corpse as live, and it is the longer-lived of the two (ISSUE-REF).
_DEAD_PROC_STATES = frozenset({"Z", "X", "x"})


@dataclass(frozen=True)
class Session:
    """One live Claude Code session.

    ``registered`` records whether a VALIDATED registry entry supplied the
    metadata; the fields are otherwise derived from ``/proc``. ``cwd`` and
    ``started_at_ms`` are ``None`` when neither source could supply them, which a
    caller joining on them must handle rather than assume.
    """

    pid: int
    name: str | None
    cwd: str | None
    started_at_ms: int | None
    registered: bool
    #: How this pid was established to be RUNNING, for a consumer that must name its
    #: evidence when it cites the process in a refusal (ISSUE-REF). Never ``None`` for a
    #: session the procfs probe minted; defaulted so the field is additive for callers
    #: constructing a ``Session`` positionally.
    liveness_proof: str | None = None
    #: WHICH SOURCE established this pid to be a Claude Code session -- a program name or
    #: an ``argv[0]`` basename (ISSUE-REF). A separate field from ``liveness_proof``
    #: because they answer different questions ("is anybody home" vs "is it one of ours")
    #: and a consumer vetoing a reap cites both. ``None`` only for a session the probe
    #: could not describe at all, which every consumer must read as a veto, not a pass.
    identity_proof: str | None = None
    #: The Claude Code session UUID from this pid's VALIDATED registry record, or ``None``
    #: when no validated record supplied one. What the Agent Mail notifier compares a
    #: transport record against to tell a live registration from a stale one (ISSUE-REF).
    session_id: str | None = None
    #: The ``--name`` this session was launched with, when its validated registry record says
    #: Claude Code titled it otherwise only because that name collided (``nameSource``
    #: ``collision``, typically a restart that starts before its predecessor exits). Peers keep
    #: addressing that requested name, so Agent Mail treats it as an alias (ISSUE-REF).
    requested_name: str | None = None
    #: Positively identified untitled daemon or resolved daemon attach target, with
    #: reads accepted by the identity fence.
    #: It holds process resources but cannot be a titled mail recipient. Unreadable or
    #: unstable candidates retain False; a missing name alone proves nothing.
    non_addressable: bool = False
    #: A native attach-target title places this row but does not prove a mail inbox.
    #: Kept separate from registered/session_id, which always describe this PID.
    placement_only: bool = False


def _record_session_id(record: dict | None) -> str | None:
    value = record.get("sessionId") if record is not None else None
    return value if isinstance(value, str) and value else None


def _proc_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return Path(os.environ.get(PROC_ROOT_ENV) or DEFAULT_PROC_ROOT)


def _registry_dir(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get(REGISTRY_DIR_ENV)
    if override:
        return Path(override)
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "sessions"


def _stat(root: Path, pid: int | str) -> tuple[int, int] | None:
    """``(ppid, starttime)`` for ``pid``, or None if it cannot be read.

    ``comm`` is parenthesised and may itself contain spaces and parentheses, so
    the split is anchored on the LAST ``)`` -- a ``split()`` on the whole line
    misattributes every field for a process whose name contains a space.
    """
    try:
        raw = (root / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    close = raw.rfind(b")")
    if close == -1:
        return None
    tail = raw[close + 2 :].split()
    if len(tail) < _STAT_MIN_FIELDS:
        return None
    try:
        return int(tail[_STAT_PPID_INDEX]), int(tail[_STAT_STARTTIME_INDEX])
    except ValueError:
        return None


def _read_cmdline(root: Path, pid: int | str) -> bytes | None:
    """The raw `cmdline` bytes, or `None` when the file could not be READ.

    `b""` and `None` are different answers and the difference is load-bearing (ISSUE-REF
    R-6): an empty cmdline is a kernel thread, while an unreadable one is a process this
    probe cannot describe. `_argv` flattens both to `[]`, which is right for callers asking
    "what is this process" and wrong for a caller asking "is anybody home".
    """
    try:
        return (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None


def _argv_from_cmdline(raw: bytes) -> list[str]:
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _argv(root: Path, pid: int | str) -> list[str]:
    raw = _read_cmdline(root, pid)
    return [] if raw is None else _argv_from_cmdline(raw)


def _argv0(root: Path, pid: int | str) -> tuple[str | None, bool]:
    """``(argv[0] or None, cmdline was readable)`` -- the IDENTITY read, narrowed.

    Splits on the FIRST NUL and decodes only that field, so no later token -- the ones
    that carry ``AIH_RUN_HANDLE`` on this fleet -- becomes a retained ``str`` in this
    process for the identity decision (ISSUE-REF). Before this, identity built the FULL
    decoded argv list of EVERY pid in the enumeration, ~400 on this host, to answer a
    question that needs ``argv[0]``; the full decode now happens only for a process
    already proved to be a session, and only to title it.

    ``_read_bytes`` is the shared reader rather than a bounded ``open(..., "rb")``
    deliberately: the subprocess-registry gate classifies a dynamic ``open()`` target as
    an unresolved WRITER needing a per-site human review, which this read does not merit
    and cannot self-grant. So the bytes of the whole file are read exactly as they always
    were, and the narrowing this function delivers is on the decode and the retention.

    The second element preserves the ISSUE-REF R-6 distinction the whole fail-closed
    branch below rests on: an EMPTY cmdline is a kernel thread, an UNREADABLE one is a
    process this probe cannot describe, and they must not collapse to the same answer.
    """
    raw = _read_cmdline(root, pid)
    if raw is None:
        return None, False
    first = raw.split(b"\0", 1)[0]
    return (first.decode("utf-8", "replace") or None), True


def _comm(root: Path, pid: int | str) -> str | None:
    """The process's `comm` name, or `None` when it could not be read."""
    try:
        return (root / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _live_pid_proof(root: Path, pid: int | str, state: str | None) -> str:
    """How `pid` was established to be RUNNING, from the fence's opening read (ISSUE-REF).

    WHY THIS EXISTS. `live_sessions` harvests pids from a `/proc` directory listing and
    then reads each one. Those two steps are not atomic, and on this host the gap is wide
    enough to matter: measured 2026-10-04, six resamples of the inventory seconds apart
    reported 0, 3, 1, 1, 3 and 1 nameless sessions, every pid distinct, monotonically
    increasing, and EVERY ONE of them `ESRCH` with `/proc/<pid>` already gone by the time
    it was checked. They were short-lived processes caught mid-spawn, not sessions.

    The bug that made those corpses load-bearing was reading an unreadable `cmdline` as
    "a process this probe cannot describe" when the overwhelmingly common cause is
    `ENOENT` -- it exited. `_read_cmdline` catches `OSError` without distinguishing the
    two, so a corpse and a live process behind a `hidepid` mount produced the same answer,
    and `no_live_session_claims` vetoed an irreversible reap on it.

    This asks the question the module already knew how to ask twenty lines further down,
    where an unreadable `stat` is correctly commented "Exited between the listing and the
    read; not a live session". A ZOMBIE is excluded for the same reason and it is the case
    that actually persists: `stat` stays readable for as long as the parent neglects to
    `wait`, so a state check is the only thing separating a reaped-but-unwaited process
    from a running one.

    Returns a PROOF STRING rather than a bool because the consumer must name how liveness
    was established for every holder it cites in a refusal; a bare `True` cannot be quoted
    into an operator-facing reason. Whether the pid is RUNNING is now decided by the identity
    fence (ISSUE-REF), whose opening read supplies ``state``: re-reading `stat` here would put
    one more read inside the bracket for a transient error to hide in.
    """
    if state is None:
        return (
            f"`{root}/{pid}/stat` did not confirm it running; kept until its death is "
            "confirmed"
        )
    return f"`{root}/{pid}/stat` readable at probe time, state `{state}`"


def _comm_read(root: Path, pid: int) -> tuple[str | None, bool]:
    """``(comm, errored)``: :func:`_comm`, plus whether the read failed for any reason but
    absence. A pid that turns out to be a session is doubtful if it did (ISSUE-REF)."""
    try:
        return (root / str(pid) / "comm").read_text(encoding="utf-8").strip(), False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def _fence_read(root: Path, pid: int) -> tuple[str, int | None, str | None]:
    """One end of the per-pid identity fence: ``(verdict, starttime, state)`` (ISSUE-REF).

    ``dead`` only on CONFIRMED death -- ``ENOENT``/``ESRCH`` on the entry, or the module's
    existing dead-state rule (:data:`_DEAD_PROC_STATES`) -- because these sessions feed
    reap guards, where under-reporting a live session can arm a deletion. ``live`` carries
    the start time (stat field 22) that identifies WHICH process holds the pid. Every
    other failure and a malformed line answer ``doubt``.
    """
    try:
        raw = (root / str(pid) / "stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return "dead", None, None
    except OSError:
        return "doubt", None, None
    close = raw.rfind(b")")
    tail = raw[close + 2 :].split() if close != -1 else []
    if len(tail) < _STAT_MIN_FIELDS:
        return "doubt", None, None
    state = tail[_STAT_STATE_INDEX].decode("utf-8", "replace")
    if state in _DEAD_PROC_STATES:
        return "dead", None, state
    try:
        return "live", int(tail[_STAT_STARTTIME_INDEX]), state
    except ValueError:
        return "doubt", None, None


def _fence_verdict(
    first: tuple[str, object, object], last: tuple[str, object, object]
) -> str:
    """Classify a pid's whole read sequence by the fence around it: same, dead or doubt.

    The fence is read BEFORE the pid's first data read and AFTER its last, so every read
    in between is accepted only when both ends name the same live process. Dead at the
    last read drops the pid. A different start time means a NEW live process now holds
    it: never dropped, never credited with the earlier reads. Anything else is doubt.
    """
    if last[0] == "dead":
        return "dead"
    if first[0] == "live" and last[0] == "live" and first[1] == last[1]:
        return "same"
    return "doubt"


def _ai_cli_sessions_dir() -> Path:
    """The stable launcher directory used by ai-cli-utils."""
    if sys.platform == "win32":
        env_var = "LOCALAPPDATA"
        fallback = Path.home() / "AppData" / "Local"
    else:
        env_var = "XDG_STATE_HOME"
        fallback = Path.home() / ".local" / "state"
    configured = os.environ.get(env_var)
    base = (
        Path(configured) if configured and Path(configured).is_absolute() else fallback
    )
    return base / "ai-cli-utils" / "sessions"


def _launcher_session_name(argv: list[str]) -> str | None:
    if len(argv) <= _SCRIPT_ARG_INDEX:
        return None
    script = Path(argv[_SCRIPT_ARG_INDEX])
    if not script.is_absolute():
        return None
    try:
        relative = script.resolve().relative_to(_ai_cli_sessions_dir().resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) != 1 or relative.suffix != ".sh":
        return None
    stem = relative.name.removesuffix(".sh")
    return next(
        (
            stem.removeprefix(prefix)
            for prefix in AI_CLI_LAUNCHER_PREFIXES
            if stem.startswith(prefix)
        ),
        None,
    )


def is_own_session_launcher(
    pid: int,
    *,
    caller_name: str | None = None,
    proc_root: Path | str | None = None,
) -> bool:
    """Whether ``pid`` runs this caller's exact ai-cli-utils launcher script."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    caller = caller_name or caller_session_name(proc_root=proc_root)
    return (
        caller is not None
        and _launcher_session_name(_argv(_proc_root(proc_root), pid)) == caller
    )


def belongs_to_own_session_launcher(
    pid: int,
    *,
    caller_name: str | None = None,
    proc_root: Path | str | None = None,
) -> bool:
    """Whether ``pid`` is or descends from this caller's ai-cli-utils launcher.

    This answers OWNERSHIP, and a descendant is included by design. It is therefore not
    a safe input to any decision that needs "is this process using this directory":
    ``worktree_audit`` consumed it as a liveness waiver and deleted a worktree out from
    under a 4h-old helper hanging off one of its own tool shells (ISSUE-REF). For that
    question walk the caller's own ancestry instead -- :func:`caller_ancestor_pids`.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    caller = caller_name or caller_session_name(proc_root=proc_root)
    if caller is None:
        return False
    root = _proc_root(proc_root)
    current = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        fields = _stat(root, current)
        if fields is None:
            return False
        if _launcher_session_name(_argv(root, current)) == caller:
            return True
        current = fields[0]
        if current <= 1:
            return False
    return False


def _is_claude_program(name: str | None) -> bool:
    """Whether a PROGRAM NAME identifies a Claude Code session process.

    One predicate for all three sources that can supply one -- ``/proc/<pid>/comm``,
    ``psutil.Process.name()`` and an ``argv[0]`` -- because they are basename-compared
    identically and a per-source rule is a second mechanism waiting to disagree.
    """
    return bool(name) and os.path.basename(name) in CLAUDE_PROGRAM_NAMES


def _identity_proof(program: str | None, argv0: str | None) -> str | None:
    """Which source proved this process a session, or None if neither did (ISSUE-REF).

    A DISJUNCTION, not a preference order, and the non-argv source is asked FIRST so a
    host where it answers never reads argv at all. Both are required because each alone
    is wrong on a host where the other answers -- see the module docstring's IDENTITY
    section for the example-client-host-mac measurement. Returns the proof STRING rather than a bool
    because a consumer gating an irreversible delete must be able to quote what it
    actually measured; this is the ``Session.identity_proof`` value.
    """
    if _is_claude_program(program):
        return f"program name `{program}` (comm / psutil `name()`), no argv read"
    if _is_claude_program(argv0):
        return f"argv[0] basename `{os.path.basename(argv0 or '')}`"
    return None


def _procfs_identity(root: Path, pid: int | str) -> str | None:
    """``_identity_proof`` for a procfs pid, reading ``comm`` before any argv.

    The ``argv[0]`` read is skipped entirely when ``comm`` already answers, which is the
    Linux case, so the fleet's "comm, never cmdline" rule is honoured wherever it is
    satisfiable. Callers needing the EMPTY-vs-UNREADABLE distinction must use
    :func:`_argv0` directly; this answers only "is it one of ours".
    """
    proof = _identity_proof(_comm(root, pid), None)
    if proof is not None:
        return proof
    argv0, _readable = _argv0(root, pid)
    return _identity_proof(None, argv0)


def _name_from_argv(argv: list[str]) -> str | None:
    """The slug-valid ``--name`` value on a command line, or None.

    Last occurrence wins, matching how an option parser resolves a repeated flag.
    """
    name = None
    for index, token in enumerate(argv):
        if token == NAME_FLAG and index + 1 < len(argv):
            name = argv[index + 1]
        elif token.startswith(NAME_FLAG + "="):
            name = token.split("=", 1)[1]
    if name and SLUG_RE.match(name):
        return name
    return None


def _psutil():
    """``psutil``, or None on a host that does not carry it.

    Imported lazily and guarded exactly as ``agent_session_registry`` does it:
    the module is a declared dependency, but a copy of this script running under
    a bare interpreter must degrade to the existing UNDETERMINED answer rather
    than fail to import.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - production packaging supplies psutil
        return None
    return psutil


def _psutil_probe(root: Path):
    """``psutil`` when it must answer for ``root``, else None.

    Three conditions, all required: ``root`` is the host's own procfs rather
    than a redirect, that procfs is unreadable, and ``psutil`` can describe THIS
    process. The last is the same positive control ``probe_ok`` applies to
    procfs -- a psutil that cannot see the process asking is a blind probe, and
    reporting an empty inventory from it would be the exact confusion of absent
    with empty this module exists to prevent.
    """
    if str(root) != DEFAULT_PROC_ROOT or _stat(root, "self") is not None:
        return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        ps.Process(os.getpid()).create_time()
    except (ps.Error, OSError, ValueError):
        return None
    return ps


def _lstart_forms(create_time: float) -> tuple[str, ...]:
    """The ``ps -o lstart=``-shaped spellings of ``create_time``: UTC, then local.

    Both are accepted because both occur in the wild for the same process; the
    module docstring records the measurement. ``%e`` is not portable to Windows
    ``strftime``, so the two-wide day is padded by hand.
    """
    utc = datetime.datetime.fromtimestamp(create_time, datetime.timezone.utc)
    local = datetime.datetime.fromtimestamp(create_time)
    return tuple(f"{dt:%a %b} {dt.day:2d} {dt:%H:%M:%S %Y}" for dt in (utc, local))


def _psutil_record_is_live(ps, pid: int, record: dict) -> bool | None:
    """``_record_is_live`` for a host with no procfs. See the module docstring."""
    try:
        create_time = ps.Process(pid).create_time()
    except ps.NoSuchProcess:
        return False
    except (ps.Error, OSError, ValueError):
        return None
    proc_start = record.get("procStart")
    if not isinstance(proc_start, str):
        return None
    try:
        int(proc_start)
    except ValueError:
        return proc_start in _lstart_forms(create_time)
    return None


def _psutil_identity(ps, process) -> tuple[str | None, list[str], bool]:
    """``(identity proof, argv, vanished)`` for one psutil process (ISSUE-REF).

    The non-argv source (``name()``) is asked first, so a host where it answers never
    reads a command line. ``argv`` is returned only once the process is PROVED a session
    -- it is the titling input, never the identity input beyond its own ``argv[0]``.

    The two error directions stay asymmetric, as they already were here: a process whose
    attributes this user may not read provably is not one of this user's sessions and is
    SKIPPED (measured on Darwin: ``pid 1`` raises ``AccessDenied``), while one that has
    VANISHED mid-walk makes the rest of an ancestry chain untrustworthy, so ``vanished``
    tells the caller to give up rather than skip.
    """
    try:
        program = process.name()
    except ps.NoSuchProcess:
        return None, [], True
    except (ps.Error, OSError, SystemError):
        program = None
    proof = _identity_proof(program, None)
    try:
        argv = list(process.cmdline() or [])
    except ps.NoSuchProcess:
        return proof, [], True
    except (ps.Error, OSError, SystemError):
        # SystemError: psutil 7.2.2's Darwin `proc_cmdline` (ISSUE-REF) -- an AccessDenied
        # that escaped as a bare SystemError; see `_psutil_live_sessions`.
        return proof, [], False
    return (proof or _identity_proof(None, argv[0] if argv else None)), argv, False


def _psutil_own_session(ps) -> tuple[int, list[str]] | None:
    """``(pid, argv)`` of the nearest ``claude`` forebear, for a host with no procfs.

    Bounded by ``MAX_ANCESTRY_DEPTH`` like the procfs walk, for the same reason.
    """
    try:
        ancestry = ps.Process().parents()
    except (ps.Error, OSError):
        return None
    for parent in ancestry[:MAX_ANCESTRY_DEPTH]:
        proof, argv, vanished = _psutil_identity(ps, parent)
        if vanished:
            return None
        if proof is not None:
            return _session_forebear(parent.pid, argv)
    return None


def _titled_from_registry(
    pid: int, argv: list[str], root: Path, registry_dir: Path
) -> str | None:
    """The session title for a process already PROVEN to be ``claude``.

    A validated registry record for that exact pid supplies a RENAMED title; the
    command line is the fallback. Shared by the procfs and psutil ancestry
    walks, which differ only in how they reach a process, never in how they
    title one.
    """
    for record in _records(registry_dir):
        if record.get("pid") != pid or _record_is_live(record, root) is not True:
            continue
        name = record.get("name")
        if isinstance(name, str) and SLUG_RE.match(name):
            return name
    return _name_from_argv(argv)


def _psutil_caller_ancestor_pids(ps) -> frozenset[int]:
    """``caller_ancestor_pids`` for a host with no procfs.

    ``parents()`` is already ordered nearest-first, so the procfs walk's
    termination conditions map across directly: the same ``MAX_ANCESTRY_DEPTH``
    bound, and the same exclusion of pids 0 and 1, which are the init chain
    rather than anything that could have launched a session.

    An error describing the ancestry yields the EMPTY set, which is exactly what
    the procfs walk returns when its first read fails. That is safe only because
    of how the one caller reads it: ``worktree_provenance.bind_owner``
    intersects this set with an independently obtained ancestry and REFUSES when
    the intersection is empty, so emptiness means "nothing proved" and never
    "proved nothing is an ancestor". A future caller that treats the empty set
    as a licence rather than a refusal would invert that.
    """
    try:
        ancestry = ps.Process().parents()
    except (ps.Error, OSError):
        return frozenset()
    pids: set[int] = set()
    for parent in ancestry[:MAX_ANCESTRY_DEPTH]:
        if parent.pid <= 1:
            break
        pids.add(parent.pid)
    return frozenset(pids)


def _psutil_session_name_for_pid(
    ps, pid: int, root: Path, registry_dir: Path
) -> str | None:
    """``session_name_for_pid`` for a host with no procfs.

    Walks ``pid`` ITSELF and then its forebears, nearest first, exactly as the
    procfs version does -- the pid handed in may already be the Claude process,
    and starting at its parent would miss that.

    Identity and the two asymmetric error paths are ``_psutil_identity``'s, shared with
    ``_psutil_own_session``: the walks differ only in how they reach a process,
    never in how they identify or title one.
    """
    try:
        start = ps.Process(pid)
        chain = [start, *start.parents()]
    except (ps.Error, OSError):
        return None
    for process in chain[:MAX_ANCESTRY_DEPTH]:
        if process.pid <= 1:
            return None
        proof, argv, vanished = _psutil_identity(ps, process)
        if vanished:
            return None
        if proof is not None:
            return _titled_from_registry(process.pid, argv, root, registry_dir)
    return None


def probe_ok(root: Path | str | None = None) -> bool:
    """Whether the probe can be trusted to distinguish absent from empty.

    A positive control, not a path test: a usable procfs always describes THIS
    process, so failing to read our own ``stat`` is the one observation that
    proves the probe blind. ``os.path.isdir`` would pass on any directory. On a
    host with no procfs the same control is applied to ``psutil`` instead.
    """
    resolved = _proc_root(root)
    if _stat(resolved, "self") is not None:
        return True
    return _psutil_probe(resolved) is not None


def _record_is_live(record: dict, root: Path) -> bool | None:
    """True when ``record`` describes a live process, False stale, None undecidable.

    None is kept distinct from False on purpose: an undecidable record must not be
    reported as stale on a machine where the probe cannot answer, or every entry
    on a procfs-less host would be discarded.
    """
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    fields = _stat(root, pid)
    if fields is None:
        ps = _psutil_probe(root)
        if ps is not None:
            return _psutil_record_is_live(ps, pid, record)
        # Absent because the process is gone, or because the probe is blind.
        return None if not probe_ok(root) else False
    proc_start = record.get("procStart")
    if proc_start is None or isinstance(proc_start, bool):
        return None
    try:
        return int(proc_start) == fields[1]
    except (TypeError, ValueError):
        return None


def _records(directory: Path) -> list[dict]:
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _validated_records(root: Path, registry_dir: Path | str | None) -> dict[int, dict]:
    """Registry records that PROVABLY describe their live process, keyed by pid.

    Shared by both enumeration paths so enrichment stays gated on validation
    rather than on presence, whichever probe answered.
    """
    validated: dict[int, dict] = {}
    for record in _records(_registry_dir(registry_dir)):
        pid = record.get("pid")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and _record_is_live(record, root) is True
        ):
            validated[pid] = record
    return validated


def _record_name(record: dict | None) -> str | None:
    if record is None:
        return None
    name = record.get("name")
    return name if isinstance(name, str) and SLUG_RE.match(name) else None


def logical_name(record: dict | None) -> str | None:
    """Registry name used by ownership ranking and task-panel identity checks."""
    name = record.get("name") if record else None
    if not isinstance(name, str) or not name.strip():
        return None
    if _collision_renamed(record):
        name = re.sub(r" \(\d+\)$", "", name)
    return name


def _collision_renamed(record: dict | None) -> bool:
    return record is not None and record.get("nameSource") == COLLISION_NAME_SOURCE


def _requested_name(record: dict | None, argv: list[str]) -> str | None:
    """``argv``'s ``--name`` when ``record`` shows Claude Code retitled it to dodge a collision."""
    if not _collision_renamed(record):
        return None
    requested = _name_from_argv(argv)
    return requested if requested != _record_name(record) else None


def _record_cwd(record: dict | None) -> str | None:
    if record is None:
        return None
    cwd = record.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _record_started_at_ms(record: dict | None) -> int | None:
    if record is None:
        return None
    started = record.get("startedAt")
    return (
        started if isinstance(started, int) and not isinstance(started, bool) else None
    )


def registry_title(
    session_id: str,
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> str | None:
    """The slug-valid title a VALIDATED registry entry gives ``session_id``.

    None covers every unusable case together -- no entry, a stale entry, an entry
    whose title is not slug-valid -- because all three route to the same fallback.
    Callers needing "was there an entry at all" should use ``live_sessions``.
    """
    if not session_id:
        return None
    root = _proc_root(proc_root)
    for record in _records(_registry_dir(registry_dir)):
        if record.get("sessionId") != session_id:
            continue
        if _record_is_live(record, root) is False:
            continue
        name = record.get("name")
        if isinstance(name, str) and SLUG_RE.match(name):
            return name
    return None


def _untitled_daemon(argv: list[str]) -> bool:
    """The shared positive daemon identity rule for ancestry and inventory."""
    return argv[1:3] == ["daemon", "run"] and _name_from_argv(argv) is None


def _attach_target_row(short: str) -> dict | None:
    """Resolve the exact daemon ID through Claude's read-only native session view.

    Do not use partial names, parentage, or the attach client's missing --name as
    evidence about the target. An unavailable or ambiguous view proves nothing.
    """
    try:
        from claude_agents_view import TERMINAL_JOB_STATES, call_claude_agents
    except ImportError:  # A partial standalone install cannot prove the target.
        return None

    try:
        rows, error = call_claude_agents()
    except OSError:  # An installed CLI can still be unreadable/unexecutable.
        return None
    if (
        error
        or not isinstance(rows, list)
        or any(not isinstance(r, dict) for r in rows)
    ):
        return None
    matching = [
        r
        for r in rows
        if r.get("id") == short
        or isinstance(r.get("sessionId"), str)
        and r["sessionId"][:8] == short
    ]
    if len(matching) != 1:
        return None
    row = matching[0]
    sid, pid = row.get("sessionId"), row.get("pid")
    if (
        row.get("kind") != "background"
        or row.get("id") != short
        or not isinstance(row.get("state"), str)
        or not row["state"]
        or row["state"] in TERMINAL_JOB_STATES
        or not isinstance(sid, str)
        or not SESSION_ID_RE.fullmatch(sid)
        or sid[:8] != short
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or "name" not in row
        or row["name"] is not None
        and not isinstance(row["name"], str)
    ):
        return None
    return row


def _strict_registry_snapshot(directory: Path) -> dict[str, dict] | None:
    """Unlike best-effort enrichment, proving NO registration requires every read."""
    try:
        records = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in directory.iterdir()
            if path.suffix == ".json"
        }
    except (OSError, ValueError):
        return None
    return records if all(isinstance(r, dict) for r in records.values()) else None


def _attach_executable(root: Path, pid: int, ps) -> Path | None:
    """Placement-only proof from the kernel executable and its owner's OS account.

    Unknown install layouts (including platforms without an account-home lookup)
    remain unproven. Neither HOME, program names nor argv can supply this proof.
    """
    if ps is None:
        try:
            uid = (root / str(pid)).stat().st_uid
            executable = (root / str(pid) / "exe").resolve(strict=True)
        except (OSError, RuntimeError):
            return None
    else:
        try:
            process = ps.Process(pid)
            uid = process.uids().real
            reported = process.exe()
            if not reported or not Path(reported).is_absolute():
                return None
            executable = Path(reported).resolve(strict=True)
        except (
            ps.Error,
            OSError,
            SystemError,
            ValueError,
            AttributeError,
            RuntimeError,
        ):
            return None
    try:
        import pwd

        home = Path(pwd.getpwuid(uid).pw_dir)
        if not home.is_absolute():
            return None
        versions = home.resolve(strict=True) / ".local/share/claude/versions"
        return (
            executable
            if (
                executable.parent == versions
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", executable.name)
                and executable.is_file()
            )
            else None
        )
    except (ImportError, KeyError, OSError, ValueError, RuntimeError):
        return None


def _attach_target_identity(
    root: Path, pid: int, ps
) -> tuple[object, list[str], Path] | None:
    """Open a target fence; binary proof grants placement, not session identity."""
    if ps is None:
        first = _fence_read(root, pid)
        executable = _attach_executable(root, pid, ps)
        if first[0] != "live" or executable is None:
            return None
        argv, doubtful = _read_title_argv(root, pid)
        if doubtful:
            return None
    else:
        try:
            process = ps.Process(pid)
            first = process.create_time()
            executable = _attach_executable(root, pid, ps)
            if executable is None:
                return None
            argv, doubtful = _psutil_cmdline(ps, process)
            if doubtful or not argv:
                return None
        except (ps.Error, OSError, SystemError, ValueError):
            return None
    return first, argv, executable


@dataclass(frozen=True)
class _AttachPlacement:
    target: dict
    client_start: object
    target_start: object
    client_executable: Path
    target_argv: list[str]
    target_executable: Path
    registry: dict
    proved_at: float


_PendingAttachPlacement = tuple[tuple, _AttachPlacement] | None
_ATTACH_PLACEMENTS: dict[tuple, _AttachPlacement] = {}
ATTACH_CACHE_INTERVAL_S = 120


def _attach_cache_interval() -> float:
    """Read the shared TOML/overlay at CALL time; a broken edit forces fresh proof."""
    try:
        from ai_chassis.config_tables import DEFAULT_CONFIG_PATH, read_layered_tables

        section = read_layered_tables(DEFAULT_CONFIG_PATH).get("agent_mail_notify", {})
        value = section.get("attach_cache_interval_s", ATTACH_CACHE_INTERVAL_S)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return 0
        return float(value)
    except (ImportError, RuntimeError, AttributeError, ValueError):
        return 0


def _attach_process_start(root: Path, pid: int, ps):
    if ps is None:
        verdict, start, _ = _fence_read(root, pid)
        return start if verdict == "live" else None
    try:
        process = ps.Process(pid)
        return process.create_time() if _psutil_running_proof(ps, process) else None
    except (ps.Error, OSError, SystemError, ValueError):
        return None


def _resolved_attach(  # noqa: C901, PLR0911, PLR0915 -- each failed evidence check retains uncertainty
    session: Session,
    argv: list[str],
    root: Path,
    registry_dir: Path | str | None,
    ps=None,
) -> tuple[Session, _PendingAttachPlacement]:
    """Place a client under a stable, uniquely resolved target's title.

    This runs INSIDE the client's existing process fence on both inventory paths.
    The target has its own start-time fence, including when it is the client PID.
    A missing title field or unreadable registration is uncertainty, not absence.
    Cache publication waits for the caller's final inventory fence and doubt checks.
    """
    if (
        session.registered
        or len(argv) != 3  # noqa: PLR2004 -- exact `claude attach <short-id>` arity
        or os.path.basename(argv[0]) != "claude"
        or argv[1] != "attach"
        or re.fullmatch(r"[0-9a-f]{8}", argv[2]) is None
    ):
        return session, None
    client_executable = _attach_executable(root, session.pid, ps)
    if client_executable is None:
        return session, None
    directory = _registry_dir(registry_dir)
    client_start = _attach_process_start(root, session.pid, ps)
    if client_start is None:
        return session, None
    cache_key = (root, directory, session.pid, tuple(argv), ps is not None)
    cached = _ATTACH_PLACEMENTS.pop(cache_key, None)
    interval = _attach_cache_interval()
    now = monotonic()
    target, opened = None, None
    reused = False
    if (
        cached is not None
        and 0 <= now - cached.proved_at < interval
        and cached.client_start == client_start
        and cached.client_executable == client_executable
        and _strict_registry_snapshot(directory) == cached.registry
    ):
        opened = _attach_target_identity(root, cached.target["pid"], ps)
        if opened is not None and (
            (opened[0][1] if ps is None else opened[0]) == cached.target_start
            and opened[1] == cached.target_argv
            and opened[2] == cached.target_executable
        ):
            target = cached.target
            reused = True
    if target is None:
        target = _attach_target_row(argv[2])
        opened = None
    if target is None:
        return session, None
    title = target["name"]
    if title not in (None, "") and not SLUG_RE.fullmatch(title):
        return session, None
    pid = target["pid"]
    opened = opened or _attach_target_identity(root, pid, ps)
    if opened is None:
        return session, None
    first, target_argv, target_executable = opened
    # The native view can retain stale job PIDs. A live Claude at that PID is not
    # enough for an untitled target: bind its argv to this job. A titled daemon
    # instead binds through two agreeing native observations and both PID fences;
    # `claude daemon run` does not carry the session UUID in argv.
    if not (
        title
        and _untitled_daemon(target_argv)
        or target_argv == argv
        or any(
            target_argv[index : index + 2] == ["--session-id", target["sessionId"]]
            for index in range(1, len(target_argv) - 1)
        )
    ):
        return session, None
    records = _strict_registry_snapshot(directory)
    if records is None or reused and records != cached.registry:
        return session, None
    matching = [
        r
        for r in records.values()
        if r.get("pid") == pid or r.get("sessionId") == target["sessionId"]
    ]
    # Stale/conflicting records cannot establish either identity or its absence.
    if len(matching) > 1:
        return session, None
    record = matching[0] if matching else None
    if record is not None:
        if record.get("pid") != pid or record.get("sessionId") != target["sessionId"]:
            return session, None
        if ps is None:
            valid = _record_start(record) == first[1]
        else:
            valid = _record_for_start(record, first) is not None
        if not valid:
            return session, None
    recipient_name = _record_name(record) or _name_from_argv(target_argv)
    if title and recipient_name not in (None, title):
        return session, None
    name = recipient_name or title or None
    if (
        not reused and _attach_target_row(argv[2]) != target
    ) or _strict_registry_snapshot(directory) != records:
        return session, None
    if ps is None:
        # The general argv reader drops empty fields. Here an extra empty argument
        # must still fail the exact three-argument rule.
        raw = _read_cmdline(root, session.pid)
        client_doubt = raw is None
        client_argv = (
            [
                part.decode("utf-8", "replace")
                for part in raw.removesuffix(b"\0").split(b"\0")
            ]
            if raw is not None
            else []
        )
        final_argv, target_doubt = _read_title_argv(root, pid)
        final_client_executable = _attach_executable(root, session.pid, ps)
        final_target_executable = _attach_executable(root, pid, ps)
        same = _fence_verdict(first, _fence_read(root, pid)) == "same"
    else:
        try:
            client_argv, client_doubt = _psutil_cmdline(ps, ps.Process(session.pid))
            final_argv, target_doubt = _psutil_cmdline(ps, ps.Process(pid))
        except (ps.Error, OSError, SystemError):
            return session, None
        final_client_executable = _attach_executable(root, session.pid, ps)
        final_target_executable = _attach_executable(root, pid, ps)
        same = _psutil_fence_verdict(ps, pid, first) == "same"
    if (
        not same
        or client_doubt
        or target_doubt
        or client_argv != argv
        or final_argv != target_argv
        or final_client_executable != client_executable
        or final_target_executable != target_executable
        or _attach_process_start(root, session.pid, ps) != client_start
    ):
        return session, None
    placement = None
    if not recipient_name:
        # Only gone/unknown placement may outlive native observation. A target
        # whose registry/argv could make the client a wake recipient must retain
        # fresh native agreement, including rejection of a conflicting title.
        placement = (
            cache_key,
            _AttachPlacement(
                target=dict(target),
                client_start=client_start,
                target_start=first[1] if ps is None else first,
                client_executable=client_executable,
                target_argv=target_argv,
                target_executable=target_executable,
                registry=records,
                proved_at=cached.proved_at if reused else now,
            ),
        )
    return replace(
        session,
        name=name,
        # Keep the client's own registration/UUID provenance. A resolved target
        # supplies placement, never a registry entry or sender identity for this PID.
        placement_only=bool(name and not recipient_name),
        non_addressable=(
            record is None and name is None and target["name"] in (None, "")
        ),
    ), placement


def _session_forebear(pid: int, argv: list[str]) -> tuple[int, list[str]] | None:
    """A daemon is an identity boundary, even with a titled creator above it."""
    if _untitled_daemon(argv):
        # Imported here, not at the top: logging pulls in about a megabyte of modules that
        # every caller of this file would otherwise read at start, for this one warning.
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning("daemon-resident, no session identity")
        return None
    return pid, argv


def _own_session(root: Path) -> tuple[int, list[str]] | None:
    """``(pid, argv)`` of this process's nearest ``claude`` forebear, or None.

    The nearest one is the right one even when Claude Code sessions nest (a
    session shelling out to a headless ``claude -p``): the innermost session is
    the one whose hook is running.
    """
    start = _stat(root, "self")
    if start is None:
        ps = _psutil_probe(root)
        return None if ps is None else _psutil_own_session(ps)
    pid = start[0]
    for _ in range(MAX_ANCESTRY_DEPTH):
        if pid <= 1:
            return None
        fields = _stat(root, pid)
        if fields is None:
            return None
        if _procfs_identity(root, pid) is not None:
            return _session_forebear(pid, _argv(root, pid))
        pid = fields[0]
    return None


def own_session_name(*, proc_root: Path | str | None = None) -> str | None:
    """This process's own session title, from its nearest ``claude`` forebear."""
    root = _proc_root(proc_root)
    own = _own_session(root)
    return (
        None
        if own is None
        else _titled_from_registry(own[0], own[1], root, _registry_dir())
    )


def own_session_pid(*, proc_root: Path | str | None = None) -> int | None:
    """The pid of this process's nearest ``claude`` forebear, or None.

    The same walk as ``own_session_name``. A hook records it so a reader can tell
    whether a registration it left still belongs to the live session (ISSUE-REF).
    An untitled ``claude daemon run`` is a boundary: no session identity is returned,
    even if its creator above it has a title.
    """
    own = _own_session(_proc_root(proc_root))
    return None if own is None else own[0]


def caller_ancestor_pids(*, proc_root: Path | str | None = None) -> frozenset[int]:
    """This process's own ancestor PIDs, walked directly from ``os.getpid()``.

    Membership is structural proof that a process belongs to the caller's launch
    chain.  It covers shells and session launchers above Claude without attributing
    siblings or other sessions whose ancestry merely looks similar.
    """
    root = _proc_root(proc_root)
    # `_psutil_probe` alone decides whether psutil may answer: it already
    # requires the host's OWN procfs, that procfs unreadable, and a psutil that
    # can see this process. Asking it directly -- rather than pre-checking a
    # `self` entry -- is what keeps a REDIRECTED root walking its synthetic tree
    # even though that tree has no `self`.
    ps = _psutil_probe(root)
    if ps is not None:
        return _psutil_caller_ancestor_pids(ps)
    current = os.getpid()
    ancestors: set[int] = set()
    for _ in range(MAX_ANCESTRY_DEPTH):
        fields = _stat(root, current)
        if fields is None:
            break
        current = fields[0]
        if current <= 1:
            break
        ancestors.add(current)
    return frozenset(ancestors)


def _bash_tool_command(argv: list[str]) -> str | None:
    """Extract the exact command from Claude Code's Bash wrapper argv.

    Claude records this command in the calling agent's pending ``tool_use`` before
    starting the wrapper. Returning only an exact parse keeps callers from matching a
    path fragment or an argv approximation to the wrong agent transcript.
    """
    wrapper_argv_length = 3
    if len(argv) < wrapper_argv_length or argv[1] != "-c":
        return None
    wrapper = argv[2]
    marker = "&& eval '"
    if "shell-snapshots/snapshot-" not in wrapper or marker not in wrapper:
        return None
    encoded = wrapper.split(marker, 1)[1]
    suffix = " && pwd -P >| "
    if suffix not in encoded:
        return None
    encoded = encoded.rsplit(suffix, 1)[0]
    if encoded.endswith(" < /dev/null"):
        encoded = encoded[: -len(" < /dev/null")]
    if not encoded.endswith("'"):
        return None
    return encoded[:-1].replace("'\"'\"'", "'")


def caller_bash_tool_command(  # noqa: PLR0911 -- each failed proof returns its exact refusal
    *, proc_root: Path | str | None = None
) -> tuple[str | None, str]:
    """The exact pending Bash command in this process's Claude wrapper ancestry.

    This is attribution evidence, not command output. The command is never included in
    the reason because tool inputs can contain secrets. A blind or unfamiliar process
    table returns no command, which every deletion caller must treat as no proof.
    """
    root = _proc_root(proc_root)
    start = _stat(root, "self")
    if start is not None:
        pid = start[0]
        for _ in range(MAX_ANCESTRY_DEPTH):
            if pid <= 1:
                break
            fields = _stat(root, pid)
            if fields is None:
                return None, "the caller's process ancestry became unreadable"
            command = _bash_tool_command(_argv(root, pid))
            if command is not None:
                return command, "the caller's Claude Code Bash wrapper was identified"
            pid = fields[0]
        return None, "no Claude Code Bash wrapper is in this process's ancestry"

    ps = _psutil_probe(root)
    if ps is None:
        return None, "the caller's process ancestry could not be read"
    try:
        ancestry = ps.Process().parents()[:MAX_ANCESTRY_DEPTH]
    except (ps.Error, OSError):
        return None, "the caller's process ancestry could not be read"
    for process in ancestry:
        try:
            command = _bash_tool_command(list(process.cmdline()))
        except (ps.Error, OSError, SystemError):
            return None, "the caller's process ancestry became unreadable"
        if command is not None:
            return command, "the caller's Claude Code Bash wrapper was identified"
    return None, "no Claude Code Bash wrapper is in this process's ancestry"


def session_name_for_pid(
    pid: int,
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> str | None:
    """The nearest Claude session title owning ``pid``, or None when unprovable.

    A tool child belongs to the Claude process in its own ancestry, not merely to a
    same-named process somewhere in the global inventory.  A validated registry record
    for that exact Claude pid supplies a renamed title; its command line is the fallback.
    This deliberately has no environment-title override: an override identifies the
    caller, never an arbitrary process being attributed to that caller.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    root = _proc_root(proc_root)
    ps = _psutil_probe(root)
    if ps is not None:
        return _psutil_session_name_for_pid(ps, pid, root, _registry_dir(registry_dir))
    current = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        fields = _stat(root, current)
        if fields is None:
            break
        if _procfs_identity(root, current) is not None:
            return _titled_from_registry(
                current, _argv(root, current), root, _registry_dir(registry_dir)
            )
        current = fields[0]
        if current <= 1:
            break
    return None


def requested_name_for_pid(
    pid: int,
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> str | None:
    """:attr:`Session.requested_name` of the live session ``pid``, or None.

    Read from the same fenced inventory the Agent Mail daemon keys sessions by, so a session's
    own commands and the daemon agree on which aliases it carries (ISSUE-REF).
    """
    found = live_sessions(registry_dir=registry_dir, proc_root=proc_root) or []
    return next((s.requested_name for s in found if s.pid == pid), None)


SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


#: Claude Code holds its own session's background-task directory open for the session's
#: life, ``<tmp>/claude-<uid>/<cwd-slug>/<session-uuid>/tasks`` (ISSUE-REF). Measured on
#: example-project-ec2 2026-10-23: each of 8 registered live sessions held exactly one, naming its
#: record's sessionId, and the one unregistered session held its transcript's uuid.
_SESSION_TASK_DIR_RE = re.compile(
    r"/claude-[^/]+/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/tasks$"
)


def _open_session_task_dirs(root: Path, pid: int) -> set[str] | None:
    """Session UUIDs of the task directories ``pid`` holds open, or None if unreadable.

    Reads only the fd LINK targets (``readlink``), never an fd's contents. A descendant
    cannot plant an fd in its ancestor, so unlike ``CLAUDE_CODE_SESSION_ID`` this cannot
    be set by the caller.
    """
    fd_dir = root / str(pid) / "fd"
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return None
    found: set[str] = set()
    for entry in entries:
        try:
            target = os.readlink(fd_dir / entry)
        except OSError:
            continue
        match = _SESSION_TASK_DIR_RE.search(target)
        if match:
            found.add(match.group(1))
    return found


def open_task_dir_session_id(
    pid: int, *, proc_root: Path | str | None = None
) -> str | None:
    """The session UUID ``pid`` is running, from the ONE session task directory it holds open.

    For a live session with no registry record (Claude Code writes one only at session
    start, so a resumed or long-running process may have none). ``None`` when the fd
    table is unreadable or names zero or several task directories. Reads fd link targets
    only, never argv (ISSUE-REF, ISSUE-REF).
    """
    found = _open_session_task_dirs(_proc_root(proc_root), pid)
    if not found or len(found) != 1:
        return None
    (session_id,) = found
    return session_id


def caller_session_id(
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> tuple[str | None, str]:
    """The session UUID of the ONE live Claude session in this process's ancestry.

    ``(None, reason)`` on anything short of a positive answer. Established, never
    asserted (ISSUE-REF): the session is the intersection of ``live_sessions`` with
    ``caller_ancestor_pids`` -- structural, so only a descendant of that ``claude``
    process gets this answer -- and its UUID comes from that pid's registry record once
    ``_record_is_live`` has matched the record's ``procStart`` to the running process,
    or, with no record, from that pid's own open fds (below). ``CLAUDE_CODE_SESSION_ID`` is deliberately not consulted: any caller can
    set it. More than one session in the ancestry (a nested ``claude -p``) refuses,
    because a pid set cannot say which one is the innermost.

    A session with no registry record (Claude Code writes one only at session start, so a
    long-running session never gets one) is identified by the one session task directory
    its own pid holds open (ISSUE-REF); zero or several such directories refuse.
    """
    sessions = live_sessions(registry_dir=registry_dir, proc_root=proc_root)
    if sessions is None:
        return None, "the live-session inventory could not be taken"
    ancestors = caller_ancestor_pids(proc_root=proc_root)
    own_pid = os.getpid()
    own = sorted(s.pid for s in sessions if s.pid == own_pid or s.pid in ancestors)
    if len(own) != 1:
        return None, (
            f"{len(own)} live Claude session(s) are in this process's ancestry "
            f"({', '.join(map(str, own)) or 'none'}), so the caller's own session is "
            "not identifiable"
        )
    root = _proc_root(proc_root)
    record = _validated_records(root, registry_dir).get(own[0])
    session_id = record.get("sessionId") if record is not None else None
    if record is None:
        task_dirs = _open_session_task_dirs(root, own[0])
        if task_dirs is not None and len(task_dirs) == 1:
            (session_id,) = task_dirs
            return session_id, (
                f"session {session_id} per the one open task directory pid {own[0]} "
                "holds, the one live Claude session in this process's ancestry "
                "(it has no registry record)"
            )
        found = "unreadable" if task_dirs is None else str(len(task_dirs))
        return None, (
            f"the caller's own session (pid {own[0]}) has no procStart-validated "
            "registry record carrying a session UUID, and its open session task "
            f"directories ({found}) do not name exactly one"
        )
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None, (
            f"the caller's own session (pid {own[0]}) has no procStart-validated "
            "registry record carrying a session UUID"
        )
    return session_id, (
        f"session {session_id} per the procStart-validated registry record of pid "
        f"{own[0]}, the one live Claude session in this process's ancestry"
    )


def caller_session_name(
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> str | None:
    """Registry-first logical identity; inherited daemon values are only fallbacks."""
    name, status = registered_session_name(
        registry_dir=registry_dir, proc_root=proc_root
    )
    if status != "absent":
        return name
    override = os.environ.get(TITLE_OVERRIDE_ENV)
    if override:
        return override if SLUG_RE.match(override) else None
    task_list_id = os.environ.get(TASK_LIST_ID_ENV)
    if task_list_id:
        return task_list_id if SLUG_RE.match(task_list_id) else None
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if session_id:
        return session_title(session_id, registry_dir=registry_dir, proc_root=proc_root)
    return own_session_name(proc_root=proc_root)


def registered_session_name(
    *,
    session_id: str | None = None,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> tuple[str | None, str]:
    """Resolve a name without borrowing the daemon creator's environment.

    The nearest live Claude PID outranks an inherited UUID. A hook may supply its
    actual payload UUID when process evidence is unavailable. Ambiguity never
    falls back to a task-list ID. This is read-only and does not inspect task rows.
    """
    root = _proc_root(proc_root)
    directory = _registry_dir(registry_dir)
    own = _own_session(root)
    records = _records(directory)
    if own is not None:
        matching = [
            r
            for r in records
            if r.get("pid") == own[0] and _record_is_live(r, root) is True
        ]
        if matching:
            if session_id and any(r.get("sessionId") != session_id for r in matching):
                return None, "unverified"
            names = {
                n for r in matching if (n := logical_name(r)) and SLUG_RE.fullmatch(n)
            }
            if len(names) == 1:
                return names.pop(), "match"
            return None, "ambiguous" if names else "unverified"
    # Without a matching live PID or an explicit hook UUID, a daemon worker's
    # inherited UUID can belong to its creator, including when ancestry is blind.
    if session_id is None and os.environ.get("CLAUDE_BG_BACKEND") == "daemon":
        return None, "unverified"
    sid = (
        session_id
        if session_id is not None
        else os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    )
    names = {
        n
        for r in records
        if sid and r.get("sessionId") == sid and _record_is_live(r, root) is True
        if (n := logical_name(r)) and SLUG_RE.fullmatch(n)
    }
    if len(names) == 1:
        return names.pop(), "match"
    return None, "ambiguous" if names else "absent"


def session_title(
    session_id: str,
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> str | None:
    """The session's title for worktree naming, or None if none can be resolved.

    Precedence: explicit environment override, then a validated registry entry,
    then this process's own ``/proc`` ancestry. None means BOTH real sources were
    consulted and neither answered, which is a genuine "no title" a caller may
    fail closed on -- unlike the pre-ISSUE-REF None, which mostly meant "the
    registry had not heard of a session that was plainly running".
    """
    override = os.environ.get(TITLE_OVERRIDE_ENV)
    if override:
        return override if SLUG_RE.match(override) else None
    if not session_id:
        return None
    root = _proc_root(proc_root)
    from_registry = registry_title(
        session_id, registry_dir=registry_dir, proc_root=root
    )
    if from_registry is not None:
        return from_registry
    return own_session_name(proc_root=root)


def _clock_ticks() -> int | None:
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return None
    return ticks if isinstance(ticks, int) and ticks > 0 else None


def _boot_time_s(root: Path) -> int | None:
    try:
        text = (root / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _undescribable_session(root: Path, pid: int, proof: str) -> tuple[Session, bool]:
    """A nameless ``Session`` for a pid neither source could describe, and whether a read doubted it.

    ISSUE-REF R-6: an unreadable ``comm`` and an unreadable ``cmdline`` are LESS
    information, not evidence of absence, and ``no_live_session_claims`` refuses on a
    session it cannot name. Whether the pid is still RUNNING is no longer asked here: the
    caller's identity fence (ISSUE-REF) brackets this read and drops a confirmed corpse,
    which is the ISSUE-REF listing/read race this function used to gate on its own.
    """
    # Read the cwd even with no name: it is the ONLY thing that can tie this pid to, or
    # exclude it from, a candidate path. A nameless session with no cwd is what forces a
    # consumer back onto a host-global answer.
    cwd, doubtful = _read_cwd(root, pid)
    return Session(
        pid=pid,
        name=None,
        cwd=cwd,
        started_at_ms=None,
        registered=False,
        liveness_proof=proof,
        identity_proof=(
            f"neither `{root}/{pid}/comm` nor its `cmdline` was readable, so this pid "
            "could not be excluded as a session"
        ),
    ), doubtful


def _read_cwd(root: Path, pid: int) -> tuple[str | None, bool]:
    """``(cwd, doubtful)``. A missing link is no value; any other error is doubt (ISSUE-REF)."""
    try:
        return os.readlink(root / str(pid) / "cwd"), False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def _read_title_argv(root: Path, pid: int) -> tuple[list[str], bool]:
    """``(argv, doubtful)`` for titling, on the same terms as :func:`_read_cwd`.

    The ONLY full command-line decode the walk takes, reached only for a pid already proved
    a session (ISSUE-REF). One read, classified where it fails: a second read to ask why
    would let a one-shot error vanish behind a read that then succeeds (ISSUE-REF).
    """
    try:
        raw = (root / str(pid) / "cmdline").read_bytes()
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True
    return _argv_from_cmdline(raw), False


def _identified_session(  # noqa: PLR0913 -- the fence and enrichment inputs, one pid
    root: Path,
    pid: int,
    first: tuple[str, int | None, str | None],
    *,
    identity: str,
    proof: str,
    validated: dict[int, dict],
    clock: tuple[float | None, int | None],
    registry_dir: Path | str | None,
) -> tuple[Session, bool, _PendingAttachPlacement]:
    """The session an identified ``claude`` pid describes, and whether a read doubted it.

    A registry record is used only when its ``procStart`` names the process the fence's
    FIRST read saw (ISSUE-REF): ``_validated_records`` checked it earlier, against a pid a
    different process may hold by now, so a reused pid must not inherit its name or cwd.
    """
    record = validated.get(pid)
    if record is not None and _record_start(record) != first[1]:
        record = None
    doubtful = False
    cwd = _record_cwd(record)
    if cwd is None:
        cwd, doubtful = _read_cwd(root, pid)
    boot, ticks = clock
    started = _record_started_at_ms(record)
    if started is None and boot is not None and ticks and first[1] is not None:
        started = int((boot + first[1] / ticks) * 1000)
    # TITLING-ONLY argv read, and the ONLY full command line this loop takes: the pid
    # is already proved a session, and a slug-valid `--name` is the sole thing
    # extracted (ISSUE-REF). Skipped entirely when a validated record already titles
    # the session, which is the healthy path.
    # A collision-renamed record is the one other reason to read argv: the requested name.
    # Its read failing costs only that alias, never the record's title (so no doubt).
    name = _record_name(record)
    requested = None
    non_addressable = False
    if name is None or _collision_renamed(record):
        argv, argv_doubtful = _read_title_argv(root, pid)
        requested = _requested_name(record, argv)
        if name is None:
            name = _name_from_argv(argv)
            doubtful = doubtful or argv_doubtful
            non_addressable = _untitled_daemon(argv)
    session = Session(
        pid=pid,
        name=name,
        cwd=cwd,
        started_at_ms=started,
        registered=record is not None,
        liveness_proof=proof,
        identity_proof=identity,
        session_id=_record_session_id(record),
        requested_name=requested,
        non_addressable=non_addressable,
    )
    placement = None
    if name is None:
        session, placement = _resolved_attach(session, argv, root, registry_dir)
    return session, doubtful, placement


def _record_start(record: dict) -> int | None:
    proc_start = record.get("procStart")
    if isinstance(proc_start, bool):
        return None
    try:
        return int(proc_start)
    except (TypeError, ValueError):
        return None


def _fenced(
    root: Path,
    first: tuple[str, int | None, str | None],
    session: Session,
    *,
    doubtful: bool,
    exclude_test_spawned: bool,
) -> Session | None:
    """Apply the identity fence's LAST read to a finished read sequence (ISSUE-REF)."""
    spawned = False
    if exclude_test_spawned:
        spawned, environ_doubtful = _procfs_spawned_by_a_test(root, session.pid)
        doubtful = doubtful or environ_doubtful
    verdict = _fence_verdict(first, _fence_read(root, session.pid))
    return _fence_outcome(verdict, session, doubtful=doubtful, spawned=spawned)


def _procfs_spawned_by_a_test(root: Path, pid: int) -> tuple[bool, bool]:
    """``(spawned, doubtful)``: :func:`spawned_by_a_test` for a READABLE procfs walk.

    A failed ``environ`` read other than absence is doubt (ISSUE-REF), where
    ``spawned_by_a_test`` answers False and keeps the populated session. Absence still
    defers to it, so its psutil and ancestry routes are unchanged.
    """
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except FileNotFoundError:
        return spawned_by_a_test(pid, proc_root=root), False
    except OSError:
        return False, True
    marker = TEST_RUN_ENV.encode() + b"="
    return any(entry.startswith(marker) for entry in raw.split(b"\0")), False


def _fence_outcome(
    verdict: str, session: Session, *, doubtful: bool, spawned: bool
) -> Session | None:
    """Confirmed death drops; doubt keeps an unplaceable candidate; ``same`` keeps the reads.

    ``spawned`` is honoured only on ``same``: a test-spawned verdict read from a process
    the fence cannot confirm is not allowed to drop a live candidate.
    """
    if verdict == "dead":
        return None
    if verdict == "doubt" or doubtful:
        return _unplaceable_session(
            session.pid, session.liveness_proof or "", session.identity_proof or ""
        )
    return None if spawned else session


def _unplaceable_session(pid: int, liveness_proof: str, identity_proof: str) -> Session:
    """A live candidate whose reads cannot be trusted to describe the process now at ``pid``.

    No name and no cwd, so every consumer reads it as a session it cannot place and fails
    closed (ISSUE-REF). Its earlier reads are discarded because a reused pid would have
    them describe a different process.
    """
    return Session(
        pid=pid,
        name=None,
        cwd=None,
        started_at_ms=None,
        registered=False,
        liveness_proof=liveness_proof,
        identity_proof=identity_proof
        + "; its identity could not be re-confirmed after reading it, so it is kept "
        "as a live candidate that cannot be placed",
    )


def spawned_by_a_test(pid: int, *, proc_root: Path | str | None = None) -> bool:
    """Whether ``pid`` carries ``PYTEST_CURRENT_TEST``, i.e. a test run spawned it (ISSUE-REF).

    Suites hand their code under test a real ``claude``-shaped process so the ancestry walk has
    a session to find, and :func:`live_sessions` cannot tell that process from a real session by
    its program name or argv. Its environment can: a real session never carries the variable.
    Only the variable's NAME is matched; no value is decoded, returned or logged, because a
    session's environment holds credentials.

    On a host with no procfs the environment usually CANNOT be read: psutil documents that on
    macOS Big Sur and later ``environ()`` answers only for the calling process, and for any
    other it returns an empty mapping or raises ``AccessDenied``. Every test-spawned process
    on example-client-host-mac therefore read as a real session and was registered on the board (ISSUE-REF).
    There the process's ANCESTRY answers instead, from command lines this user can read: a
    process with a pytest run among its ancestors was spawned by a test, and a real session
    never has one (pytest runs inside sessions, never above them).

    A process for which neither the environment nor the ancestry answers is still False. That
    keeps an unreadable process counted as a session, which is the direction every consumer
    here fails safe in.
    """
    root = _proc_root(proc_root)
    marker = TEST_RUN_ENV.encode() + b"="
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except OSError:
        if str(root) != DEFAULT_PROC_ROOT or _stat(root, "self") is not None:
            return False
        ps = _psutil()
        if ps is None:
            return False
        try:
            process = ps.Process(pid)
        except (ps.Error, OSError, SystemError):
            return False
        try:
            environment = process.environ()
        except (ps.Error, OSError, SystemError):
            environment = None
        if environment:
            return TEST_RUN_ENV in environment
        return _psutil_descends_from_pytest(ps, process)
    return any(entry.startswith(marker) for entry in raw.split(b"\0"))


_PYTEST_PROGRAMS = frozenset({"pytest", "py.test"})


def _is_pytest_argv(argv: list[str]) -> bool:
    """Whether ``argv`` is a pytest run: ``pytest ...``, ``python <path>/pytest ...`` (a
    console script reached through its shebang) or ``python -m pytest ...``.

    Positions are fixed rather than searched, so a command that merely MENTIONS pytest
    (``vim pytest.ini``, ``rg pytest``) is not one.

    A command line whose only non-empty element holds spaces is split on whitespace first.
    That is how macOS reports a process that imported ``setproctitle`` (the joined line, then
    one empty string per original argument), and a ``pytest -n`` controller imports it through
    ``xdist.remote``; read unsplit, the fixtures under every xdist run counted as sessions and
    went on the live board (ISSUE-REF).
    """
    filled = [token for token in argv if token]
    if len(filled) == 1 and any(character.isspace() for character in filled[0]):
        argv = filled[0].split()
    program, *rest = argv or [""]
    if os.path.basename(program) in _PYTEST_PROGRAMS:
        return True
    if not os.path.basename(program).startswith("python") or not rest:
        return False
    if os.path.basename(rest[0]) in _PYTEST_PROGRAMS:
        return True
    return rest[0] == "-m" and len(rest) > 1 and rest[1] in _PYTEST_PROGRAMS


def _psutil_descends_from_pytest(ps, process) -> bool:
    """Whether a pytest run is among ``process``'s ancestors, nearest first.

    An ancestor whose command line cannot be read is skipped, not trusted: the answer is True
    only on a command line actually read. Bounded by ``MAX_ANCESTRY_DEPTH``.
    """
    try:
        ancestry = process.parents()
    except (ps.Error, OSError, SystemError):
        return False
    for parent in ancestry[:MAX_ANCESTRY_DEPTH]:
        if parent.pid <= 1:
            break
        try:
            argv = list(parent.cmdline() or [])
        except (ps.Error, OSError, SystemError):
            continue
        if _is_pytest_argv(argv):
            return True
    return False


def live_sessions(
    *,
    registry_dir: Path | str | None = None,
    proc_root: Path | str | None = None,
) -> list[Session] | None:
    """Every live Claude Code session, or None when the probe cannot be trusted.

    ``started_at_ms`` comes from a validated registry entry when there is one and
    is otherwise DERIVED from boot time plus field 22, which is approximate:
    measured against three live sessions' own records it ran 8-20 s early,
    because the record is stamped when the session initialises rather than when
    the process starts. That is inside the tolerance of a nearest-in-time join
    over candidates minutes or hours apart, and far outside the tolerance of an
    equality test -- do not compare it for equality.

    A pid whose ``cmdline`` cannot be READ is kept as a session with no name rather
    than skipped (ISSUE-REF R-6). An unreadable file is not evidence of a non-Claude
    process, and a consumer gating an irreversible delete reads a skipped pid as
    nobody home; ``worktree_audit.no_live_session_claims`` refuses on a nameless
    session for exactly that reason. An EMPTY cmdline is a different answer -- a
    kernel thread -- and stays skipped when ``comm`` is not ``claude``.

    BUT THAT PID MUST STILL BE RUNNING (ISSUE-REF). "Unreadable" was conflated with
    "undescribable" and the usual cause is ``ENOENT`` -- the process exited between the
    directory listing and the read -- so the inventory minted nameless sessions out of
    corpses, each of which vetoed a reap. The identity fence (ISSUE-REF) now gates BOTH
    branches -- the nameless one and the identified one -- and every listed session carries
    the ``liveness_proof`` that cleared it. A ZOMBIE is excluded too, and it is the case that
    persists rather than races: the identified branch used to ask only whether ``stat``
    was READABLE, which a zombie satisfies for as long as its parent neglects to ``wait``,
    so a reaped-but-unwaited ``claude`` was listed as a live session and vetoed a reap.

    A pid a TEST RUN spawned is not listed to a reader outside one (ISSUE-REF). Suites hand
    their code a ``claude``-shaped process, and every host-wide reader (the chief's fleet verb,
    the reap gate) took it for a real session. :func:`spawned_by_a_test` decides, and it answers
    False for a pid it cannot read, so an unreadable environment keeps the pid listed. A reader
    that is itself a test still sees them: that is how a suite gives its code a session to find.
    """
    root = _proc_root(proc_root)
    return _all_live_sessions(
        root, registry_dir, exclude_test_spawned=TEST_RUN_ENV not in os.environ
    )


def _all_live_sessions(
    root: Path, registry_dir: Path | str | None, *, exclude_test_spawned: bool
) -> list[Session] | None:
    """:func:`live_sessions`, with the test-spawned filter applied inside each pid's fence."""
    ps = _psutil_probe(root)
    if ps is not None:
        return _psutil_live_sessions(
            ps, root, registry_dir, exclude_test_spawned=exclude_test_spawned
        )
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    pids = sorted(int(entry) for entry in entries if entry.isdigit())
    if not pids:
        # A usable procfs always lists at least this process, so an empty
        # enumeration is a blind probe rather than an idle machine.
        return None

    validated = _validated_records(root, registry_dir)
    clock = (_boot_time_s(root), _clock_ticks())

    sessions: list[Session] = []
    for pid in pids:
        # THE IDENTITY FENCE OPENS HERE, before any read of this pid (ISSUE-REF). Every
        # read below is accepted only if `_fenced`'s closing read names the same live
        # process; see `_fence_verdict`.
        first = _fence_read(root, pid)
        # IDENTITY FIRST, and `comm` before any argv (ISSUE-REF). `_argv0` reads only the
        # first NUL-delimited field, so the full command line of a non-session pid -- the
        # overwhelming majority of the enumeration -- is never decoded here at all.
        comm, comm_errored = _comm_read(root, pid)
        identity = _identity_proof(comm, None)
        cmdline_readable = True
        if identity is None:
            argv0, cmdline_readable = _argv0(root, pid)
            identity = _identity_proof(None, argv0)
        if identity is None and (cmdline_readable or comm is not None):
            # One of the two sources was READ and did not name a session. That is an
            # answer, including the empty cmdline of a kernel thread, and the fence never
            # turns a pid that is not a session into a candidate.
            continue
        proof = _live_pid_proof(root, pid, first[2] if first[0] == "live" else None)
        placement = None
        if identity is None:
            # BOTH UNREADABLE, which is not "nobody home" -- the worst available failure
            # for a liveness probe is one that could not look reporting an absence (R-6).
            # A corpse is still dropped, by the fence (ISSUE-REF).
            session, doubtful = _undescribable_session(root, pid, proof)
        else:
            session, doubtful, placement = _identified_session(
                root,
                pid,
                first,
                identity=identity,
                proof=proof,
                validated=validated,
                clock=clock,
                registry_dir=registry_dir,
            )
        checked = _fenced(
            root,
            first,
            session,
            doubtful=doubtful or comm_errored,
            exclude_test_spawned=exclude_test_spawned,
        )
        if checked is session and placement is not None:
            _ATTACH_PLACEMENTS[placement[0]] = placement[1]
        if checked is not None:
            sessions.append(checked)
    return sessions


def _psutil_live_sessions(
    ps,
    root: Path,
    registry_dir: Path | str | None,
    *,
    exclude_test_spawned: bool,
) -> list[Session]:
    """``live_sessions`` for a host with no procfs.

    Identity is decided by the SAME ``_identity_proof`` disjunction the procfs path uses
    -- ``name()`` first, ``argv[0]`` only if that does not answer -- so a headless
    ``claude -p`` child and a nameless session are classified identically on every OS.
    ``process_iter`` skips a process that exits mid-enumeration and reports an unreadable
    attribute as None, so another user's process contributes nothing rather than raising.

    ``cmdline`` IS still in the ``process_iter`` attribute set, and on Darwin it is the
    only source that answers -- ``name()`` returns the Claude Code VERSION STRING there,
    measured, see the module docstring's IDENTITY section. Dropping it would report zero
    sessions on every macOS host, which is the fail-open direction: the worktree audit
    reads an empty inventory as "nobody holds this path".

    ``name``, ``cmdline`` and ``cwd`` are read PER PROCESS rather than in the
    ``process_iter`` snapshot (``name`` because psutil's own ``name()`` calls ``cmdline()``
    for a name of 15+ characters), because psutil 7.2.2's Darwin ``proc_cmdline`` raises a bare
    ``SystemError`` -- not a ``psutil.Error`` -- for a process caught mid-exit (ISSUE-REF).
    The C helper sets ``AccessDenied`` for a ``sysctl(KERN_PROCARGS2)`` that failed with
    ``errno 0`` and then returns success, so CPython reports "returned a result with an
    exception set" (psutil #2708's fix, corrected upstream by #2854, unreleased as of
    7.2.2). ``as_dict`` converts only psutil errors, so inside the snapshot it escaped the
    iterator and aborted the whole inventory. Unlike ``AccessDenied`` -- another user's
    process, which provably is not this user's session -- a SystemError says nothing about
    ownership, so a pid that is still RUNNING after one is kept as a nameless session
    exactly as the procfs path keeps an undescribable pid (R-6); one that is gone or a
    zombie is a corpse and owns nothing (ISSUE-REF).
    """
    validated = _validated_records(root, registry_dir)
    sessions: list[Session] = []
    for process in ps.process_iter(["pid", "create_time"]):
        info = process.info
        pid = info["pid"]
        # The identity fence's FIRST read (ISSUE-REF): `process_iter`'s snapshot, taken
        # before any per-process read below. A record is used only when it names that
        # process, so a reused pid cannot inherit an earlier process's name or cwd.
        created = info.get("create_time")
        record = _record_for_start(validated.get(pid), created)
        name_errored = False
        try:
            program = process.name()
        except ps.NoSuchProcess:
            continue
        except (ps.Error, OSError, SystemError):
            program, name_errored = None, True
        argv, cmdline_doubtful = _psutil_cmdline(ps, process)
        if argv is None:
            continue
        identity = _identity_proof(program, argv[0] if argv else None)
        if identity is None and not cmdline_doubtful:
            continue
        liveness = "psutil `process_iter` reported it at probe time"
        if identity is None:
            liveness = _psutil_running_proof(ps, process)
            if liveness is None:
                continue
        doubtful = name_errored or cmdline_doubtful
        try:
            cwd = _record_cwd(record) or process.cwd()
        except ps.NoSuchProcess:
            continue
        except (ps.Error, OSError, SystemError):
            cwd, doubtful = None, True
        started = _record_started_at_ms(record)
        if started is None and created is not None:
            started = int(created * 1000)
        session = Session(
            pid=pid,
            name=_record_name(record) or _name_from_argv(argv),
            cwd=cwd,
            started_at_ms=started,
            registered=record is not None,
            liveness_proof=liveness,
            identity_proof=identity
            or (
                f"psutil `cmdline()` failed for pid {pid} and its program name did not "
                "identify it, so this pid could not be excluded as a session"
            ),
            session_id=_record_session_id(record),
            requested_name=_requested_name(record, argv),
            non_addressable=_record_name(record) is None and _untitled_daemon(argv),
        )
        # The test-spawned read is the LAST data read, so it precedes the closing fence.
        session, placement = _resolved_attach(session, argv, root, registry_dir, ps)
        spawned = exclude_test_spawned and spawned_by_a_test(pid, proc_root=root)
        checked = _fence_outcome(
            _psutil_fence_verdict(ps, pid, created),
            session,
            doubtful=doubtful,
            spawned=spawned,
        )
        if checked is session and placement is not None:
            _ATTACH_PLACEMENTS[placement[0]] = placement[1]
        if checked is not None:
            sessions.append(checked)
    return sorted(sessions, key=lambda session: session.pid)


def _psutil_cmdline(ps, process) -> tuple[list[str] | None, bool]:
    """``(argv, doubtful)`` from psutil's ``cmdline()``; ``argv`` is None for a confirmed exit.

    Every failure but ``NoSuchProcess`` is doubt (ISSUE-REF): an unread command line can
    hide both the ``claude`` identity (Darwin's ``name()`` is the version string) and the
    ``--name`` that ties a session to its owner. The one exception is ``AccessDenied`` on
    ANOTHER user's process, which provably is not this user's session and is denied for
    every such process on Darwin, so counting it would veto every reap on the host.
    """
    try:
        return list(process.cmdline() or []), False
    except ps.NoSuchProcess:
        return None, False
    except ps.AccessDenied:
        return [], _psutil_owned_by_this_user(ps, process)
    except (ps.Error, OSError, SystemError):
        return [], True


def _psutil_owned_by_this_user(ps, process) -> bool:
    """Whether ``process`` may run as this user: False only when its owner provably differs.

    Numeric real UIDs wherever psutil has ``uids()`` (ISSUE-REF): a username is not an
    account identity, and comparing our spelling with the process's can drop a live
    session. The EFFECTIVE UID must match too (ISSUE-REF): macOS runs a setuid-root
    ``/usr/bin/login`` for every terminal tab, with our real UID and root's effective one,
    and Darwin denies ``cmdline()`` on the effective UID. A ``claude`` session is never
    setuid, so a different effective UID proves the pid is not one; compared on the real
    UID alone, each login was doubt and vetoed every reap on the host. Where it has none
    (Windows), both usernames come from the SAME psutil call, so they share a spelling,
    and are compared casefolded because Windows account names are case-insensitive. Without that fallback every system process that denies
    ``cmdline()`` would be doubt, and one doubt vetoes every reap on the host. Any read
    that fails or comes back empty answers True, which keeps the pid as doubt.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        try:
            uids = process.uids()
            return uids.real == getuid() and uids.effective == getuid()
        except (AttributeError, NotImplementedError):
            pass
        except (ps.Error, OSError, SystemError):
            return True
    try:
        theirs = process.username()
        ours = ps.Process(os.getpid()).username()
    except (ps.Error, OSError, SystemError, AttributeError, NotImplementedError):
        return True
    if not theirs or not ours:
        return True
    return theirs.casefold() == ours.casefold()


def _record_for_start(record: dict | None, created: float | None) -> dict | None:
    """``record`` only when it provably describes the process created at ``created``.

    ``procStart`` is an ``lstart`` string, so it names a creation SECOND, and two processes
    created in the same second match it alike (ISSUE-REF). ``startedAt`` closes that: it is
    stamped when the session initialises, after its own process started (measured 8-20 s
    after, see :func:`live_sessions`), and an earlier holder of the pid exited, record
    written, before the current process was created. So a record whose ``startedAt``
    precedes ``created`` belongs to an earlier process, and one without it cannot be told
    apart: neither enriches.
    """
    if record is None or created is None:
        return None
    if record.get("procStart") not in _lstart_forms(created):
        return None
    started = _record_started_at_ms(record)
    if started is None or started < created * 1000:
        return None
    return record


def _psutil_fence_verdict(ps, pid: int, created: float | None) -> str:
    """The psutil fence's LAST read: :func:`_fence_verdict`'s rule on ``create_time``.

    psutil reads ``create_time`` when a ``Process`` is constructed and caches it on the
    instance, so ``create_time()`` on an object already in hand re-reads nothing: a pid
    reused after that object was built would pass with the old value (ISSUE-REF). So
    ``status()`` is read first, and the identity is observed LAST on a newly constructed
    ``Process``, after every other read of the pid.
    """
    try:
        status = ps.Process(pid).status()
        now = ps.Process(pid).create_time()
    except ps.NoSuchProcess:
        return "dead"
    except (ps.Error, OSError, SystemError, ValueError):
        return "doubt"
    if status == ps.STATUS_ZOMBIE:
        return "dead"
    return "same" if created is not None and now == created else "doubt"


def _psutil_running_proof(ps, process) -> str | None:
    """How ``process`` was re-established RUNNING after a failed read, or None if not.

    The psutil counterpart of the procfs fence's dead rule: gone and zombie are both corpses.
    """
    try:
        status = process.status()
    except ps.NoSuchProcess:
        return None
    except (ps.Error, OSError, SystemError):
        status = "unreadable"
    if status == ps.STATUS_ZOMBIE:
        return None
    return f"psutil `status()` read `{status}` after its `cmdline()` read failed"


def main(argv: list[str] | None = None) -> int:
    """Print the live inventory as JSON. Exit 1 when the probe is unusable.

    The non-zero exit is the contract for shell and skill callers: an empty
    ``sessions`` list with exit 0 means "none are running", and exit 1 means
    "could not tell", which they must not conflate.
    """
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] not in {"--json"}:
        print(f"usage: {Path(sys.argv[0]).name} [--json]", file=sys.stderr)
        return 2
    sessions = live_sessions()
    if sessions is None:
        json.dump({"probe_ok": False, "sessions": []}, sys.stdout)
        sys.stdout.write("\n")
        print(
            "claude-sessions: the /proc probe is unusable, so an empty inventory "
            "cannot be distinguished from no sessions running",
            file=sys.stderr,
        )
        return 1
    json.dump(
        {
            "probe_ok": True,
            "sessions": [
                {
                    "pid": session.pid,
                    "name": session.name,
                    "cwd": session.cwd,
                    "startedAt": session.started_at_ms,
                    "registered": session.registered,
                    "non_addressable": session.non_addressable,
                    "placement_only": session.placement_only,
                }
                for session in sessions
            ],
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
