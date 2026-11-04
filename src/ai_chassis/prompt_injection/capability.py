"""Disposable, fail-closed admission for Claude Code versions without shipped evidence.

No caller pane is passed to the probe. Only a UUID-named private tmux server can
receive probe input. The child runs the real injector; its temporary pin applies
only to that disposable target, never to a delivery or the caller's config.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_chassis.prompt_injection import _tmux
from ai_chassis.prompt_injection.config import (
    capability_settings,
    claude_root,
    defaults,
    load_config,
)
from ai_chassis.prompt_injection.observe import AttemptStore
from ai_chassis.prompt_injection.targets import _claude_version_at

try:
    import fcntl
except ImportError:  # pragma: no cover - tmux hosts use POSIX locks
    fcntl = None

PROBE_TIMEOUT_SECONDS = 180
NARROW_WIDTH = 46
TYPE_WIDTH = 84
TEARDOWN_TIMEOUT_SECONDS = 5
CTX_PCT_STAGE_SECONDS = 35
CTX_PCT_INCONCLUSIVE_LIMIT = 2
#: Checks a late frame or a slow host can fail on its own (ISSUE-REF). Measured while
#: machine-contention reads anything but ADMIT, such a failure is not the version's.
TIMING_CHECKS = (
    "walk-scroll-line-local:",
    "post-submit-landing:",
    "probe-readiness:",
    "extended-key-negotiation:",
    "probe-timeout:",
)
CONTENTION_TIMEOUT_SECONDS = 10
#: Failed measurements whose run records stay under ``runs/``, newest first.
KEPT_PROBE_RECORDS = 10
_LOCK = threading.Lock()
_DEADLINE = contextvars.ContextVar("capability_probe_deadline", default=None)


@contextlib.contextmanager
def probe_budget(seconds: float):
    """Share a caller's probe budget across lock waits and multiple measurements."""
    deadline = time.monotonic() + seconds
    previous = _DEADLINE.get()
    token = _DEADLINE.set(min(deadline, previous) if previous is not None else deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _probe_timeout(limit: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("capability-probe", limit)
    return min(limit, remaining)


@dataclass(frozen=True)
class ProbeFailure:
    check: str
    observation: str


class _TeardownFailed(Exception):
    """A successful probe cannot be admitted while its session remains alive."""


class _Inconclusive(Exception):
    """Do not memoize an unmeasured ctx-pct stage as a successful measurement."""


class _Unsettled(Exception):
    """A timing failure measured on a contended host: refuse with it, record nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure


class _Deferred(Exception):
    """The caller's budget cannot hold a full probe, so this process measures nothing."""

    def __init__(self, *, running: bool = False) -> None:
        super().__init__()
        #: Another process holds the measurement lock, so none is started here.
        self.running = running


def _budget_short() -> bool:
    """True when the caller's shared budget is under one full probe."""
    deadline = _DEADLINE.get()
    return deadline is not None and deadline - time.monotonic() < PROBE_TIMEOUT_SECONDS


def check_capability(version: str | None, *, refresh: bool = False) -> str | None:
    """Return a refusal, or None for admission, including an inconclusive ctx-pct stage.

    Resolve the executable before caching: a target on an older version must not
    inherit a newer binary's result. Configuration and executable identity are
    part of the key; failed checks never become successful through cache reuse.
    """
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        return "version-readable: failed"
    try:
        _probe_timeout(PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "probe-timeout: TimeoutExpired"
    binary = _version_binary(version)
    if binary is None:
        return "probe-executable: claude unavailable"
    check = "probe-executable"
    try:
        path = Path(binary).resolve(strict=True)
        stat = path.stat()
        root = claude_root().resolve()
        check = "probe-keybindings"
        binding = (root / "keybindings.json").read_bytes()
        check = "probe-code"
        code = hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest()
    except OSError as error:
        return f"{check}: {type(error).__name__} errno={error.errno}"
    result = None
    try:
        # Serialize misses too: lru_cache alone allows two concurrent first probes.
        with _LOCK, contextlib.suppress(_Inconclusive):
            if refresh:
                _cached_probe.cache_clear()
                result = _shared_probe(
                    version,
                    str(path),
                    root,
                    [
                        version,
                        str(path),
                        stat.st_mtime_ns,
                        stat.st_size,
                        str(root),
                        binding.hex(),
                        code,
                    ],
                    refresh=True,
                )
            else:
                result = _cached_probe(
                    version,
                    str(path),
                    stat.st_mtime_ns,
                    stat.st_size,
                    str(root),
                    binding,
                    code,
                )
    except _Unsettled as unsettled:
        result = unsettled.failure
    except _Deferred as deferred:
        result = (
            "probe-deferred: another process is measuring this version"
            if deferred.running
            else _measure_detached(version, refresh=refresh)
        )
    return result


def _measure_detached(version: str, *, refresh: bool) -> str:
    """Start the full measurement in its own process and refuse this check uncached.

    The goal waker shares 60 s across a tick, under the probe's own bound; a probe cut
    short there would be cached as the version's result (ISSUE-REF). The detached
    process has no caller budget and records the result a later check reads."""
    argv = [
        sys.executable,
        "-m",
        "ai_chassis.prompt_injection.capability",
        "--reverify" if refresh else "--measure",
        version,
    ]
    # Linux only: stopping a systemd service kills its whole cgroup, so the waker's
    # oneshot unit would take a plain child with it. A scope of its own survives.
    if (
        sys.platform.startswith("linux")
        and os.environ.get("INVOCATION_ID")
        and shutil.which("systemd-run")
    ):
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", *argv]
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
            # launchd kills a finished job's process group; a new session leaves it.
            start_new_session=True,
        )
    except OSError as error:
        return (
            f"probe-deferred: detached measurement failed to start errno={error.errno}"
        )
    return (
        f"probe-deferred: caller budget under {PROBE_TIMEOUT_SECONDS}s, "
        "measuring in a detached process"
    )


def _child_env() -> dict[str, str]:
    """Script consumers bootstrap src in sys.path without an installed package.
    Forward the actual package root only to a child; do not depend on its cwd or
    mutate the caller."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    str(Path(__file__).resolve().parents[2]),
                    os.environ.get("PYTHONPATH"),
                ),
            )
        ),
    }


def _version_binary(version: str) -> str | None:
    """The executable of ``version`` itself, else ``claude`` on PATH.

    The native installer repoints ``claude`` at each new version but keeps older
    versions' binaries under ``versions/``, where a live session on an older version
    still runs from (ISSUE-REF). Look there first: next to the PATH launcher's own
    target, then in the installer's documented roots. Elsewhere (an npm install) the
    PATH binary is the only one, and ``_run_probe`` refuses it if its version differs.
    """
    on_path = shutil.which("claude")
    roots = [
        Path.home() / ".local" / "share" / "claude" / "versions",
        Path.home() / "AppData" / "Local" / "claude" / "versions",
    ]
    if on_path is not None:
        with contextlib.suppress(OSError):
            target = Path(on_path).resolve(strict=True).parent
            if target.name == "versions":
                roots.insert(0, target)
    for root in roots:
        for name in (version, version + ".exe"):
            if (root / name).is_file():
                return str(root / name)
    return on_path


@functools.lru_cache(maxsize=32)
def _cached_probe(version, binary, _mtime, _size, root, _binding, _code=""):
    return _shared_probe(
        version,
        binary,
        Path(root),
        [version, binary, _mtime, _size, root, _binding.hex(), _code],
    )


def _shared_probe(version, binary, root: Path, identity, *, refresh=False):  # noqa: C901 - preserve strict cache validation
    """One measurement per identity, even across concurrent SessionStart/delivery CLIs."""
    if fcntl is None:
        return "probe-cache-lock: POSIX locking unavailable"
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    store = AttemptStore(root / "state", ledger="prompt-injection-capabilities")
    directory = store.directory
    path = store.path(key, "probe")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            short = _budget_short()
            busy = _lock_identity(fd, short=short)
            if busy is not None:
                return busy
            if not refresh:
                try:
                    report = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = None
                if (
                    isinstance(report, dict)
                    and set(report) == {"key", "attempt_id", "failure"}
                    and report["key"] == key
                    and report["attempt_id"] == "probe"
                    and (
                        report["failure"] is None or isinstance(report["failure"], str)
                    )
                    and not _ctx_pct_inconclusive(report["failure"])
                ):
                    return report["failure"]
            if short:
                raise _Deferred
            before = _contention()
            failure = _admit_measured(
                store, version, _measure_probe(version, binary, Path(root))
            )
            if failure is not None and failure.startswith(TIMING_CHECKS):
                # A late frame reads like a broken input path; only an uncontended
                # host's measurement may stand as the version's (ISSUE-REF).
                verdict = before if before != "ADMIT" else _contention()
                if verdict != "ADMIT":
                    raise _Unsettled(_inconclusive(store, key, failure, verdict))
            # A recorded result, pass or failure, ends any run of inconclusive ones.
            store.path(key, "inconclusive").unlink(missing_ok=True)
            store.write({"key": key, "attempt_id": "probe", "failure": failure})
            return failure
        finally:
            os.close(fd)
    except subprocess.TimeoutExpired:
        return "probe-timeout: TimeoutExpired"
    except OSError as error:
        return f"probe-cache: {type(error).__name__} errno={error.errno}"


def _lock_identity(fd: int, *, short: bool) -> str | None:
    """Take the identity's measurement lock; a refusal when it stays held."""
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS + 30
    if _DEADLINE.get() is not None:
        deadline = min(deadline, _DEADLINE.get())
    if short:
        # Outlast a reader's brief hold, never a measurement the budget could not
        # hold anyway: the next check reads its record.
        deadline = min(deadline, time.monotonic() + 1)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            if time.monotonic() >= deadline:
                if short:
                    raise _Deferred(running=True) from None
                return "probe-cache-lock: measurement still running after deadline"
            time.sleep(0.05)


def _inconclusive(store: AttemptStore, key: str, failure: str, verdict: str) -> str:
    """The refusal for a timing failure measured under contention, counting it.

    A host that never reads ADMIT would otherwise answer "inconclusive, re-run" for a
    really broken version forever, with nothing asking for a fix. After
    ``capability.inconclusive_escalate_after`` in a row for this identity (read now),
    the check reports the plain failure, which carries the /bug-fix repair. Neither is
    recorded as the version's result. Called under the identity's lock."""
    path = store.path(key, "inconclusive")
    try:
        seen = json.loads(path.read_text(encoding="utf-8")).get("count")
    except (OSError, ValueError, AttributeError):
        seen = None
    count = (seen if type(seen) is int and seen > 0 else 0) + 1
    store.write({"key": key, "attempt_id": "inconclusive", "count": count})
    limit = int(capability_settings(load_config())["inconclusive_escalate_after"])
    if count >= limit:
        return (
            f"{failure} (inconclusive {count} times in a row: machine-contention never "
            f"read ADMIT while measuring, last contention={verdict}; reported as the "
            "failure, not recorded)"
        )
    return (
        f"probe-inconclusive: {failure} (measured while machine-contention read "
        f"contention={verdict}; not recorded, the next check measures again)"
    )


def _contention() -> str:
    """This host's machine-contention verdict (ADMIT, LOOK or HOLD), else ``unmeasured``.

    Read through the installed CLI: the library imports nothing from ``scripts``."""
    tool = shutil.which("machine-contention")
    if tool is None:
        return "unmeasured"
    try:
        with subprocess.Popen(
            [tool, "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=CONTENTION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return "unmeasured"
        verdict = (json.loads(stdout).get("verdict") or {}).get("verdict")
    except (OSError, ValueError, AttributeError):
        return "unmeasured"
    return verdict if verdict in {"ADMIT", "LOOK", "HOLD"} else "unmeasured"


def _admit_measured(store: AttemptStore, version: str, failure: str | None):
    """The result to cache for a fresh measurement; an unmeasured ctx-pct stage is not cached."""
    seen = _record_ctx_pct_result(store, version, failure)
    if not _ctx_pct_inconclusive(failure):
        return failure
    if seen < CTX_PCT_INCONCLUSIVE_LIMIT:
        raise _Inconclusive
    # Two unmeasured attempts: the restart notice now carries the bug, so stop paying for
    # a model call on every check and admit the version from cache.
    return None


def _ctx_pct_inconclusive(failure):
    return isinstance(failure, str) and failure.startswith("ctx-pct: inconclusive ")


def _record_ctx_pct_result(
    store: AttemptStore, version: str, failure: str | None
) -> int:
    """Count consecutive inconclusive results for a version; return the new count."""
    # This lives beside the per-identity cache, but survives a keybinding change.
    key = f"ctx-pct-{version}"
    if not (failure or "").startswith(("ctx-pct:", "probe-error: ctx-pct")):
        if failure is not None or not store.read(key):
            return 0
    with (store.directory / f"ctx-pct-{version}.lock").open("a") as lock:
        deadline = time.monotonic() + _probe_timeout(PROBE_TIMEOUT_SECONDS)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("ctx-pct counter lock deadline") from None
                time.sleep(0.05)
        rows = store.read(key)
        count = rows[0].get("count", 0) if rows else 0
        count = (
            count
            if type(count) is int and 0 <= count <= CTX_PCT_INCONCLUSIVE_LIMIT
            else 0
        )
        count = (
            min(CTX_PCT_INCONCLUSIVE_LIMIT, count + 1)
            if _ctx_pct_inconclusive(failure)
            else 0
        )
        store.write(
            {
                "key": key,
                "attempt_id": "inconclusive",
                "count": count,
                "failure": failure,
            }
        )
        return count


def ctx_pct_notices(root: Path | None = None):
    """Ctx-pct failures and repeated inconclusives use restart refusal notices."""
    store = AttemptStore(
        (root or claude_root()) / "state", ledger="prompt-injection-capabilities"
    )
    for record in store.all():
        match = re.fullmatch(r"ctx-pct-(\d+\.\d+\.\d+)", str(record.get("key", "")))
        if not match or record.get("attempt_id") != "inconclusive":
            continue
        if record.get("count") == CTX_PCT_INCONCLUSIVE_LIMIT:
            # Name the last measurement's kind: the bare count made every recurrence look
            # alike, so the fixes that followed it could only guess (ISSUE-REF).
            last = _without_record(record.get("failure"))
            kind = (
                last.removeprefix("ctx-pct: inconclusive ")
                if _ctx_pct_inconclusive(last)
                and re.fullmatch(SAFE_OBSERVATION, last.removeprefix("ctx-pct: "))
                else None
            )
            yield (
                match[1],
                "ctx-pct: inconclusive attempts=2"
                + (f" (last: {kind})" if kind else ""),
            )
        else:
            failure = _without_record(record.get("failure"))
            if isinstance(failure, str) and failure in {
                "ctx-pct: unavailable attribution attempts=2",
                "probe-error: ctx-pct disallowed tool call",
                "probe-error: ctx-pct launch failed",
            }:
                yield match[1], failure


def session_start() -> str | None:
    """Enabled SessionStart warmup; delivery uses the identical fail-closed gate."""
    binary = shutil.which("claude")
    version = _claude_version_at.__wrapped__(binary) if binary else None
    if version in defaults()["multiline_mode"]["verified_cc_versions"]:
        return None
    return check_capability(version)


def _measure_probe(version, binary, root):
    try:
        return _run_probe(version, binary, root)
    except _TeardownFailed:
        return "probe-teardown: private session could not be stopped"
    except subprocess.TimeoutExpired:
        return "probe-timeout: TimeoutExpired"
    except OSError as error:
        return f"probe-setup: {type(error).__name__} errno={error.errno}"
    except Exception as error:  # noqa: BLE001 - every probe error fails closed and is cached
        return f"probe-error: {type(error).__name__}"


def _report_failure(result, version: str) -> str | None:  # noqa: PLR0911 - strict report branches
    if result.returncode != 0:
        # Classify known bootstrap failures without relaying child stderr, which
        # can contain private paths, pane contents or credential-bearing errors.
        stderr = getattr(result, "stderr", "") or ""
        if re.search(
            r"ModuleNotFoundError: No module named ['\"]ai_chassis(?:\.|['\"])", stderr
        ):
            return f"probe-process: child could not import ai_chassis (exit={result.returncode})"
        return f"probe-process: child exited with status {result.returncode}"
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return "probe-report: unreadable"
    if (
        isinstance(report, dict)
        and set(report) == {"passed", "version"}
        and report["passed"] is True
        and report["version"] == version
    ):
        return None
    check = report.get("check") if isinstance(report, dict) else None
    if not isinstance(check, str) or check not in PROBE_CHECKS:
        return "probe-report: invalid"
    observation = report.get("observation")
    if observation is None:
        return f"{check}: failed"
    # The child emits only this vocabulary and bounded numbers/booleans. Never
    # relay arbitrary strings from a subprocess report or its stderr.
    if (
        not isinstance(observation, str)
        or re.fullmatch(SAFE_OBSERVATION, observation) is None
    ):
        return "probe-report: invalid observation"
    return f"{check}: {observation}"


def _cleanup_tasks(tasks: Path) -> None:
    for index in range(1, 15):
        path = tasks / f"{index}.json"
        if path.is_file() and not path.is_symlink():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("description") == "Disposable capability probe":
                path.unlink()
    # Unexpected files stay for inspection; never recursively remove them.
    if not any(tasks.iterdir()):
        tasks.rmdir()


def _reap_stale_servers() -> None:
    """Only exact private sockets in tmux's per-user socket directory are ours."""
    directory = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"
    cutoff = time.time() - PROBE_TIMEOUT_SECONDS - TEARDOWN_TIMEOUT_SECONDS
    for socket in directory.glob("aih-capability-*"):
        if re.fullmatch(r"aih-capability-[0-9a-f]{32}", socket.name) is None:
            continue
        try:
            info = socket.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_mtime < cutoff
        ):
            killed = _tmux.run(
                ("tmux", "-L", socket.name),
                ["kill-server"],
                timeout=_probe_timeout(TEARDOWN_TIMEOUT_SECONDS),
            )
            # A server killed with SIGKILL leaves its socket node behind. Only the
            # refusals that mean nothing is listening may remove it: any other
            # failure could be a live server, which a removed node would orphan.
            if (
                killed is not None
                and killed.returncode != 0
                and re.search(
                    r"no server running|error connecting|Connection refused",
                    killed.stderr,
                )
            ):
                socket.unlink(missing_ok=True)


def _lease_attached(name: str, pid: int, deadline: float) -> bool:
    """The owner's own client is attached to the lease session on its server."""
    listed = _tmux.run(
        ("tmux", "-L", name, "-f", "/dev/null"),
        ["list-clients", "-t", "lease", "-F", "#{client_pid}"],
        timeout=max(deadline - time.monotonic(), 0.01),
    )
    return (
        listed is not None
        and listed.returncode == 0
        and str(pid) in listed.stdout.split()
    )


#: The launching session's own identity, task list, pane and messaging channel: never
#: the probe's Claude Code's.
_CALLER_ENV = (
    "TMUX",
    "TMUX_PANE",
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_TASK_LIST_ID",
    "AI_CLI_SUPERVISOR_CHILD_READY_PATH",
    "AI_CLI_SUPERVISOR_LEASE_FD",
    "AI_CLI_SUPERVISOR_TERMINAL_FD",
    "AI_HARNESS_SESSION_ID",
    "AI_TMUX_SESSION",
)


def _probe_env(name: str) -> dict[str, str]:
    """The environment the private server, and so every probe pane, starts with.

    A pane inherits its server's environment, not the environment of the client that
    created its session, so scrubbing only the probe child's own copy left the caller's
    session id, task list and messaging channel in the probe's Claude Code (ISSUE-REF).
    The host login stays in place; no credential file is read or copied."""
    env = {key: value for key, value in os.environ.items() if key not in _CALLER_ENV}
    env["CLAUDE_CODE_TASK_LIST_ID"] = name
    env["CLAUDE_CODE_GOAL_CHECKIN_MINUTES"] = "0"
    return env


@contextlib.contextmanager
def _server_owner(name: str, config: Path):
    """A control client's stdin is a POSIX lifetime lease, including on SIGKILL.

    Configure exit-unattached at server startup, before creating any session.
    EOF disconnects the client and tmux itself kills every session.
    This also works when the server moves into a different systemd scope.

    The client's output goes to /dev/null, never to a pipe this process reads. tmux
    lets a control client exit only once its queued output is written, and output
    queued for a dead owner's pipe never is (EPIPE), so the client stayed attached
    and the server alive (ISSUE-REF). Readiness is asked of the server instead.
    """
    startup_timeout = _probe_timeout(TEARDOWN_TIMEOUT_SECONDS)
    owner = subprocess.Popen(
        [
            "tmux",
            "-L",
            name,
            "-f",
            str(config),
            "-C",
            "new-session",
            "-s",
            "lease",
            f"exec sleep {PROBE_TIMEOUT_SECONDS + TEARDOWN_TIMEOUT_SECONDS}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # The server's environment is every probe pane's (ISSUE-REF).
        env=_probe_env(name),
    )
    try:
        deadline = time.monotonic() + startup_timeout
        while not _lease_attached(name, owner.pid, deadline):
            if owner.poll() is not None or time.monotonic() >= deadline:
                raise _TeardownFailed
            time.sleep(0.02)
        yield
    finally:
        owner.stdin.close()
        try:
            owner.wait(timeout=TEARDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=TEARDOWN_TIMEOUT_SECONDS)
            raise _TeardownFailed from None


def _run_probe(version: str, binary: str, root: Path) -> str | None:
    _reap_stale_servers()
    # Read afresh on a cache miss; the PATH spelling can stay the same on upgrade.
    if _claude_version_at.__wrapped__(binary) != version:
        return (
            "probe-version-match: no executable of the target version on disk "
            "(installed executable differs from target)"
        )
    if shutil.which("tmux") is None:
        return "probe-start: tmux executable unavailable"
    name = f"aih-capability-{uuid.uuid4().hex}"
    command = ("tmux", "-L", name, "-f", "/dev/null")
    tasks = root / "tasks" / name
    try:
        tasks.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        return f"probe-task-namespace: {type(error).__name__} errno={error.errno}"
    try:
        for index in range(1, 15):
            (tasks / f"{index}.json").write_text(
                json.dumps(
                    {
                        "id": str(index),
                        "subject": f"Capability probe row {index}",
                        "description": "Disposable capability probe",
                        "status": "pending",
                        "blocks": [],
                        "blockedBy": [],
                    }
                ),
                encoding="utf-8",
            )
        with (
            tempfile.TemporaryDirectory(prefix=name + "-") as scratch,
            contextlib.ExitStack() as lifetime,
        ):
            config = Path(scratch) / "tmux.conf"
            config.write_text("set-option -s exit-unattached on\n", encoding="utf-8")
            lifetime.enter_context(_server_owner(name, config))
            failure = "probe-process: incomplete"
            try:
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "ai_chassis.prompt_injection.capability",
                            "--probe",
                            version,
                            binary,
                            name,
                            scratch,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=_probe_timeout(PROBE_TIMEOUT_SECONDS),
                        env=_child_env(),
                    )
                    failure = _report_failure(result, version)
                except subprocess.TimeoutExpired:
                    failure = "probe-timeout: TimeoutExpired"
                if failure is not None:
                    # The scratch goes with this block; its evidence must not (ISSUE-REF).
                    kept = _keep_record(root, name, Path(scratch), failure)
                    if kept is not None:
                        failure = f"{failure} record={kept}"
                return failure
            finally:
                # Kill only the private session, BEFORE removing its scratch cwd.
                stopped = _tmux.run(
                    command,
                    ["kill-session", "-t", "probe"],
                    timeout=TEARDOWN_TIMEOUT_SECONDS,
                )
                if failure is None and (stopped is None or stopped.returncode):
                    raise _TeardownFailed
    finally:
        _cleanup_tasks(tasks)


def _keep_record(root: Path, name: str, scratch: Path, failure: str) -> Path | None:
    """Copy a failed measurement's evidence out of its scratch before the scratch is
    removed: the injector's run records and frames, the probe pane's last frame and the
    ctx-pct stage's output. None when there is nothing to keep or it cannot be kept."""
    sources = [
        p for p in (scratch / "resume", scratch / "probe-frame.txt") if p.exists()
    ]
    sources += sorted(scratch.glob("ctx-pct-*.jsonl"))
    if not sources:
        return None
    runs = root / "state" / "prompt-injection-capabilities" / "runs"
    kept = runs / name
    try:
        kept.mkdir(mode=0o700, parents=True)
        for source in sources:
            if source.is_dir():
                shutil.copytree(source, kept / source.name, symlinks=True)
            else:
                shutil.copy2(source, kept / source.name)
        (kept / "report.json").write_text(
            json.dumps({"check": failure.partition(":")[0], "failure": failure}),
            encoding="utf-8",
        )
        _prune_records(runs)
    except OSError:
        return None
    return kept


def _prune_records(runs: Path) -> None:
    """Keep the newest KEPT_PROBE_RECORDS run records; only exact probe names are ours."""
    records = sorted(
        (
            path
            for path in runs.iterdir()
            if re.fullmatch(r"aih-capability-[0-9a-f]{32}", path.name)
            and path.is_dir()
            and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for old in records[KEPT_PROBE_RECORDS:]:
        shutil.rmtree(old)


def _without_record(failure: object) -> object:
    """A recorded failure without the ``record=<path>`` suffix a kept run appends."""
    if isinstance(failure, str) and " record=" in failure:
        return failure.rsplit(" record=", 1)[0]
    return failure


PROBE_CHECKS = frozenset(
    {
        "probe-start",
        "probe-error",
        "probe-geometry",
        "probe-readiness",
        "extended-key-negotiation",
        "walk-scroll-line-local",
        "post-submit-landing",
        "probe-version-match",
        "ctx-pct",
    }
)

#: The injector's own refusal reason and detail on a walk or landing observation
#: (ISSUE-REF): the reason is a reason token, the detail is built by the injector from
#: numbers and tokens, and anything else is withheld before it reaches a report.
_SAFE_REASON = r"[a-z][a-z0-9-]{0,63}"
_SAFE_DETAIL = r"[A-Za-z0-9 =:;,()._-]{1,400}"
_INJECTOR_SAYS = rf"(?: reason={_SAFE_REASON}(?: detail={_SAFE_DETAIL})?)?"

SAFE_OBSERVATION = (
    r"(?:tmux executable unavailable|tmux new-session (?:returned no result|exit=-?\d{1,3})"
    r"|private pane id unreadable|capture-pane (?:returned no result"
    r"|exit=-?\d{1,3}(?: server gone| probe session gone)?)"
    r"|claude (?:exited status=\d{1,3}|killed signal=[a-z0-9]{1,12}|exited) before composer"
    r"|composer cursor absent after 30s|soft-newline prerequisites missing after 30s"
    r"|folder-trust dialog still open after 30s"
    r"|pane width differs from \d{1,3}|pane height (?:unreadable|nonpositive)"
    r"|walk stage_walked=(?:True|False) rows_exceed_visible=(?:True|False)"
    r" moves=(?:True|False) filler=(?:True|False) inert_ups=(?:True|False)"
    + _INJECTOR_SAYS
    + r"|landing submitted=(?:True|False) native=(?:True|False) verified=(?:True|False)"
    + _INJECTOR_SAYS
    + r"|private namespace invalid or executable version differs|exception=[A-Za-z]{1,50}"
    r"|unavailable attribution attempts=2|inconclusive (?:no-tool-call|no-result"
    r"|no-transcript|non-text|timeout|output lines=\d)"
    r"|ctx-pct disallowed tool call|ctx-pct launch failed)"
)


def _goal(width: int, height: int = 30) -> tuple[str, str]:
    from ai_chassis.prompt_injection.preflight import wrapped_rows

    instruction = "Probe only: reply DONE, then stop. "
    if width == TYPE_WIDTH:
        return instruction + " ".join(f"t{n:03}" for n in range(1, 151)), "type"
    # Exceed the WHOLE pane, including a spare row: no panel preference can make
    # this fit. Retain wrapped logical lines and an interior blank line.
    lines = [instruction, " ".join(f"w00{n:02}" for n in range(1, 41)), ""]
    while wrapped_rows("/goal " + "\n".join(lines), width) <= height + 1:
        lines.append(" ".join(f"w{len(lines):02}{n:02}" for n in range(1, 41)))
    return "\n".join(lines), "multiline"


def _injector_says(outcome, record: dict) -> str:
    """The injector's own refusal reason and detail, for the failure report: the five
    booleans alone hid which step refused and why (ISSUE-REF)."""
    named = outcome.reason or "none"
    if re.fullmatch(_SAFE_REASON, named) is None:
        named = "withheld"
    detail = record.get("refusal_detail")
    detail = " ".join(detail.split()) if isinstance(detail, str) else ""
    if detail and re.fullmatch(_SAFE_DETAIL, detail) is None:
        detail = "withheld"
    return f" reason={named}" + (f" detail={detail}" if detail else "")


def _exercise(command, target, scratch: Path) -> ProbeFailure | None:
    """Measure the shipped walk and exact post-submit landing at all probe widths.

    Caller is the private worker only. The first two runs leave history present
    for subsequent walks. No Up is sent beyond a proved row zero by the injector.
    """
    from ai_chassis.prompt_injection.injector import GoalInjector
    from ai_chassis.prompt_injection.payload import SlashCommandPayload

    class WalkProbeInjector(GoalInjector):
        def _verify_scrolled(self, _run, _target, _text, tail):
            # An ordinary one-line delivery may prove just head + tail. This
            # disposable measurement must exercise the FULL walk instead.
            return tail

    config = defaults()
    config["multiline_mode"]["verified_cc_versions"] = [target.cc_version]
    config["completion_timeout_seconds"] = 0
    config["use_cases"]["start-goal"]["self_heal"]["enabled"] = False
    # The type-mode payload stays under 800 chars. A slightly shorter private
    # pane makes its ten rows exceed Claude's composer window even without tasks.
    for width, requested_height in ((NARROW_WIDTH, 30), (120, 30), (TYPE_WIDTH, 28)):
        _tmux.run(
            command,
            [
                "resize-window",
                "-t",
                target.pane_id,
                "-x",
                str(width),
                "-y",
                str(requested_height),
            ],
        )
        if _tmux.display(command, target.pane_id, "#{pane_width}") != str(width):
            return ProbeFailure("probe-geometry", f"pane width differs from {width}")
        height = _tmux.display(command, target.pane_id, "#{pane_height}")
        if not height or not height.isdecimal() or int(height) <= 0:
            return ProbeFailure(
                "probe-geometry",
                "pane height nonpositive"
                if height == "0"
                else "pane height unreadable",
            )
        text, mode = _goal(width, int(height))
        outcome = WalkProbeInjector(tmux_command=command, config=config).run(
            target, SlashCommandPayload.goal(text, mode=mode), landing_timeout=30
        )
        (path,) = (scratch / "resume" / "attempts").glob(f"*{outcome.attempt_id}.json")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not (
            record.get("stage_verify") == "walked"
            and (record.get("walk_rows") or 0) > (record.get("visible_rows") or 0)
            and (record.get("walk_moves") or 0) > 0
            and (record.get("walk_filler") or 0) > 0
            and not record.get("walk_inert_ups")
        ):
            return ProbeFailure(
                "walk-scroll-line-local",
                f"walk stage_walked={record.get('stage_verify') == 'walked'} "
                f"rows_exceed_visible={(record.get('walk_rows') or 0) > (record.get('visible_rows') or 0)} "
                f"moves={(record.get('walk_moves') or 0) > 0} "
                f"filler={(record.get('walk_filler') or 0) > 0} "
                f"inert_ups={bool(record.get('walk_inert_ups'))}"
                + _injector_says(outcome, record),
            )
        if not (
            outcome.submitted
            and outcome.landing in {"command-record", "command-echo"}
            and record.get("post_submit_verdict") == "post-submit-verified"
        ):
            return ProbeFailure(
                "post-submit-landing",
                f"landing submitted={bool(outcome.submitted)} "
                f"native={outcome.landing in {'command-record', 'command-echo'}} "
                f"verified={record.get('post_submit_verdict') == 'post-submit-verified'}"
                + _injector_says(outcome, record),
            )
    return None


def _probe(version: str, binary: str, name: str, scratch: Path) -> ProbeFailure | None:
    from ai_chassis.prompt_injection.targets import SessionTarget, slug

    if (
        re.fullmatch(r"aih-capability-[0-9a-f]{32}", name) is None
        or not scratch.name.startswith(name + "-")
        or _claude_version_at.__wrapped__(binary) != version
    ):
        return ProbeFailure(
            "probe-version-match",
            "private namespace invalid or executable version differs",
        )
    if shutil.which("tmux") is None:
        return ProbeFailure("probe-start", "tmux executable unavailable")
    # Mutate environment only in this disposable child. Keep the host login in
    # place; never copy or read credential files. Disable hooks/MCP and model tools.
    ctx_config = os.environ.get(
        "AIH_CONTEXT_SUPERVISOR_CONFIG_PATH",
        str(Path.home() / ".claude/state/context-supervisor-config.json"),
    )
    # The probe's panes take their environment from the server _server_owner started
    # (_probe_env); this child's own copy only serves its injector runs.
    scrubbed = _probe_env(name)
    for key in set(os.environ) - set(scrubbed):
        del os.environ[key]
    os.environ.update(scrubbed)
    os.environ["AIH_RESUME_ROOT"] = str(scratch / "resume")
    os.environ["AIH_PROMPT_INJECTION_CONFIG_PATH"] = str(scratch / "pi.json")
    os.environ["AIH_CONTEXT_SUPERVISOR_CONFIG_PATH"] = str(scratch / "cs.json")
    session_id = str(uuid.uuid4())
    # The owner is the sole server creator. If its pipe closed before this child
    # reached new-session, -N prevents recreating an unowned detached server.
    command = ("tmux", "-L", name, "-f", "/dev/null", "-N")
    # Start a shell first: extended-keys must be on BEFORE Claude negotiates.
    result = _tmux.run(
        command,
        [
            "new-session",
            "-d",
            "-s",
            "probe",
            "-x",
            "46",
            "-y",
            "30",
            "-c",
            str(scratch),
        ],
    )
    if result is None or result.returncode:
        return ProbeFailure(
            "probe-start",
            "tmux new-session returned no result"
            if result is None
            else f"tmux new-session exit={result.returncode}",
        )
    _tmux.run(command, ["set-option", "-s", "extended-keys", "on"])
    # Keep the pane if Claude Code exits before its composer, so _wait_composer can
    # read how it ended instead of only finding the session gone (ISSUE-REF).
    _tmux.run(command, ["set-option", "-w", "-t", "probe", "remain-on-exit", "on"])
    argv = [
        binary,
        "--model",
        "haiku",
        "--name",
        # Short on purpose: the name is printed in the composer's top rule, and a
        # 46-character name fills a 46-column pane so no composer is detected.
        "probe",
        "--session-id",
        session_id,
        "--settings",
        '{"disableAllHooks":true}',
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "",
    ]
    _tmux.run(
        command, ["send-keys", "-t", "probe", "exec " + shlex.join(argv), "Enter"]
    )
    pane = _tmux.display(command, "probe", "#{pane_id}")
    if not pane:
        return ProbeFailure("probe-start", "private pane id unreadable")
    failure = _wait_composer(command, pane)
    if failure:
        return failure
    _tmux.run(command, ["set-option", "-w", "-t", "probe", "-u", "remain-on-exit"])
    target = SessionTarget(
        key=name,
        pane_id=pane,
        fingerprint=_tmux.fingerprint(command, pane) or "",
        cwd=scratch,
        session_name="probe",
        cc_version=version,
        # Claude Code names the project directory from its cwd's realpath. On macOS
        # the temp dir is /var/folders/..., a symlink into /private/var (ISSUE-REF).
        transcript_path=claude_root()
        / "projects"
        / slug(os.path.realpath(scratch))
        / f"{session_id}.jsonl",
        transcript_source="hook-payload",
    )
    failure = _exercise(command, target, scratch)
    return failure or _ctx_pct_stage(
        command, binary, scratch, session_id, supervisor_config=ctx_config
    )


_CTX_PCT_UNATTRIBUTED = (
    "ctx-pct: this host has no /proc, so the calling agent cannot be identified while"
    " sub-agents are active; this is the SESSION's reading, not necessarily the caller's"
)
_CTX_PCT_BOUNDARY = (
    r"Auto-compact boundary \(.*\): ~\d+ points (?:remaining|past boundary)"
)


def _ctx_pct_reply(path: Path, invocation: str) -> str:  # noqa: C901 - validate all tool blocks before admitting output
    """Classify actual tool blocks only. Model prose never becomes evidence."""
    calls, results = {}, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        content = record.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_input = block.get("input", {})
                if (
                    record.get("type") != "assistant"
                    or block.get("name") != "Bash"
                    or tool_input.get("command") != invocation
                    or tool_input.get("run_in_background", False)
                    or calls
                ):
                    return "disallowed"
                calls[block["id"]] = True
            elif block.get("type") == "tool_result" and record.get("type") == "user":
                results[block.get("tool_use_id")] = block.get("content")
    if not calls:
        return "inconclusive no-tool-call"
    call_id = next(iter(calls))
    if call_id not in results:
        return "inconclusive no-result"
    output = results[call_id]
    if isinstance(output, list):
        output = "\n".join(b.get("text", "") for b in output if isinstance(b, dict))
    if not isinstance(output, str):
        return "inconclusive non-text"
    return _ctx_pct_output_verdict(output)


def _ctx_pct_output_verdict(output: str) -> str:
    """Classify the text one ctx-pct tool call returned."""
    output = re.sub(r"\AExit code -?\d+\r?\n", "", output.strip(), count=1)
    # Without /proc (macOS) the helper cannot attribute the call while the stage's own
    # sub-agent fixture is fresh, so it warns on stderr and serves the session reading,
    # which the tool result puts first (measured on 2.1.306 and 2.1.307).
    lines = output.removeprefix(_CTX_PCT_UNATTRIBUTED + "\n").splitlines()
    # The reading also carries ctx-pct's own auto-compact boundary line; any other
    # extra line keeps the output inconclusive.
    reading = [line for line in lines if not re.fullmatch(_CTX_PCT_BOUNDARY, line)]
    if len(reading) == 1 and re.fullmatch(r"(?:100|[0-9]{1,2})", reading[0]):
        return "number"
    # A refusal prints its reason on stderr and then `unavailable` on stdout, and the
    # tool result keeps that order (measured on 2.1.306).
    if (
        lines
        and lines[-1] == "unavailable"
        and "ctx-pct: cannot tell which agent ran this command:" in output
    ):
        return "attribution-unavailable"
    # Name the shape, never the text: how many lines were left after the known annotations
    # separates an empty result (0), a non-number (1) and extra lines (2 or more) without
    # copying a byte of model-visible output into a record that outlives the probe.
    return f"inconclusive output lines={min(len(reading), 9)}"


def _ctx_pct_stage(  # noqa: PLR0911 - keep each stage outcome explicit
    command, binary: str, scratch: Path, session_id: str, *, supervisor_config=None
):
    """Restart only the private pane: the first CLI disabled tools at startup."""
    from ai_chassis.prompt_injection.targets import slug

    helper = Path.home() / ".claude/bin/ctx-pct"
    home = scratch / "ctx-home"
    state = home / ".claude/state"
    state.mkdir(parents=True)
    config = supervisor_config or os.environ.get(
        "AIH_CONTEXT_SUPERVISOR_CONFIG_PATH",
        str(Path.home() / ".claude/state/context-supervisor-config.json"),
    )
    transcript = (
        claude_root()
        / "projects"
        / slug(os.path.realpath(scratch))
        / f"{session_id}.jsonl"
    )
    if not transcript.is_file():
        return ProbeFailure("ctx-pct", "inconclusive no-transcript")
    agents = transcript.parent / session_id / "subagents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "agent-probe.jsonl").write_text("{}\n", encoding="utf-8")
    try:
        with probe_budget(CTX_PCT_STAGE_SECONDS):
            for attempt in (1, 2):
                # A fresh state fixes the reading; the real CLI must still attribute
                # its Bash wrapper to its own pending call before it can serve it.
                (state / f"context-pct-{session_id}.json").write_text(
                    json.dumps(
                        {
                            "session_id": session_id,
                            "cwd": str(scratch),
                            "used_percentage": 1,
                            "updated_at": datetime.now(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                            "transcript_path": str(transcript),
                        }
                    ),
                    encoding="utf-8",
                )
                invocation = shlex.join(
                    [
                        "env",
                        f"HOME={home}",
                        "CTX_PCT_CACHE_TTL=0",
                        f"AIH_CONTEXT_SUPERVISOR_CONFIG_PATH={config}",
                        str(helper),
                    ]
                )
                argv = [
                    binary,
                    "-p",
                    "--model",
                    "haiku",
                    "--resume",
                    session_id,
                    "--settings",
                    '{"disableAllHooks":true}',
                    "--setting-sources",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--tools",
                    "Bash",
                    "--allowedTools",
                    f"Bash({invocation})",
                    "--permission-mode",
                    "dontAsk",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--max-turns",
                    "2",
                    "Run exactly this Bash command once, then stop: " + invocation,
                ]
                output = scratch / f"ctx-pct-{attempt}.jsonl"
                done = scratch / f"ctx-pct-{attempt}.done"
                shell = (
                    shlex.join(argv)
                    + " > "
                    + shlex.quote(str(output))
                    + " 2>/dev/null; printf done > "
                    + shlex.quote(str(done))
                    + f"; exec sleep {PROBE_TIMEOUT_SECONDS}"
                )
                result = _tmux.run(
                    command,
                    ["respawn-pane", "-k", "-t", "probe", "-c", str(scratch), shell],
                    timeout=_probe_timeout(5),
                )
                if result is None or result.returncode:
                    return ProbeFailure("probe-error", "ctx-pct launch failed")
                while not done.exists():
                    time.sleep(min(0.05, _probe_timeout(0.05)))
                _probe_timeout(1)
                verdict = _ctx_pct_reply(output, invocation)
                if verdict == "number":
                    return None
                if verdict == "disallowed":
                    return ProbeFailure("probe-error", "ctx-pct disallowed tool call")
                if verdict != "attribution-unavailable":
                    return ProbeFailure("ctx-pct", verdict)
            return ProbeFailure("ctx-pct", "unavailable attribution attempts=2")
    except subprocess.TimeoutExpired:
        return ProbeFailure("ctx-pct", "inconclusive timeout")


# A trust-dialog key whose frame has not repainted after this long is taken as swallowed.
_TRUST_KEY_RESEND_S = 3.0


def _capture_failure(command, frame) -> ProbeFailure:
    """Name what a failed capture means: the probe session gone, or its whole server."""
    if frame is None:
        return ProbeFailure("probe-start", "capture-pane returned no result")
    sessions = _tmux.run(command, ["list-sessions", "-F", "#{session_name}"])
    where = ""
    if sessions is not None and sessions.returncode != 0:
        where = " server gone"
    elif sessions is not None and "probe" not in sessions.stdout.split():
        where = " probe session gone"
    return ProbeFailure("probe-start", f"capture-pane exit={frame.returncode}{where}")


def _pane_death(command, pane: str) -> ProbeFailure | None:
    """How Claude Code ended, read from the pane remain-on-exit keeps after it exits."""
    state = _tmux.display(
        command, pane, "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
    )
    dead, status, sig = ((state or "").split(" ") + ["", "", ""])[:3]
    if dead != "1":
        return None
    # The same tmux 3.7c prints the signal's name ("kill") on example-client-host-mac and its number ("9")
    # on example-project-ec2, so a number is mapped to its name and the message reads the same.
    if sig.isdecimal():
        with contextlib.suppress(ValueError):
            sig = signal.Signals(int(sig)).name.removeprefix("SIG").lower()
    if re.fullmatch(r"[a-z0-9]{1,12}", sig):
        return ProbeFailure(
            "probe-start", f"claude killed signal={sig} before composer"
        )
    if status.isdecimal():
        return ProbeFailure(
            "probe-start", f"claude exited status={status} before composer"
        )
    return ProbeFailure("probe-start", "claude exited before composer")


def _trust_key(text: str) -> str:
    """Enter on a frame showing the cursor on Yes, else the key that jumps to Yes.

    Down toggles the two options (it wraps), so a Down resent while the pane lagged
    landed the real cursor on "No, exit" while the frame still showed Yes, and the Enter
    sent on that frame quit Claude Code (ISSUE-REF). Home and End jump to an option
    whatever the cursor is, so any number of them, resent or still queued, leaves it
    on Yes and the Enter that follows confirms Yes.
    """
    lines = text.splitlines()
    if any(
        line.lstrip().startswith("❯") and "Yes, I trust this folder" in line
        for line in lines
    ):
        return "Enter"
    yes = next(i for i, line in enumerate(lines) if "Yes, I trust this folder" in line)
    no = next((i for i, line in enumerate(lines) if "No, exit" in line), None)
    return "Home" if no is not None and yes < no else "End"


def _wait_composer(command, pane: str) -> ProbeFailure | None:
    from ai_chassis.prompt_injection.preflight import multiline_preconditions_missing

    deadline = time.monotonic() + 30
    answered_frame, answered_at = None, 0.0
    while time.monotonic() < deadline:
        frame = _tmux.run(command, ["capture-pane", "-p", "-t", "probe"])
        if frame is None or frame.returncode != 0:
            return _capture_failure(command, frame)
        death = _pane_death(command, pane)
        if death:
            return death
        text = frame.stdout
        if "Yes, I trust this folder" in text:
            # 2.1.305 opens this dialog on "No, exit", and Enter there quits Claude Code
            # (ISSUE-REF). Confirm only from the Yes line, and never read the dialog's
            # own ❯ as the composer's.
            # One key per repaint (ISSUE-REF). A loaded host repaints several captures
            # after a key: wait for a new frame, and resend only when the pane has not
            # changed for a while (a swallowed key). _trust_key keeps a resend harmless.
            stale = (
                text == answered_frame
                and time.monotonic() - answered_at < _TRUST_KEY_RESEND_S
            )
            if not stale:
                _tmux.run(command, ["send-keys", "-t", "probe", _trust_key(text)])
                answered_frame, answered_at = text, time.monotonic()
        elif "❯" in text and not multiline_preconditions_missing(command, pane):
            break
        time.sleep(0.1)
    else:
        if "Yes, I trust this folder" in text:
            return ProbeFailure(
                "probe-readiness", "folder-trust dialog still open after 30s"
            )
        return ProbeFailure(
            "extended-key-negotiation" if "❯" in text else "probe-readiness",
            "soft-newline prerequisites missing after 30s"
            if "❯" in text
            else "composer cursor absent after 30s",
        )
    # Never toggle C-t: it persists a host-global preference. The exercise sizes
    # its drafts to clip regardless of whether the panel is shown.
    if multiline_preconditions_missing(command, pane):
        return ProbeFailure(
            "extended-key-negotiation", "soft-newline prerequisites missing after 30s"
        )
    return None


def main() -> None:
    if sys.argv[1:] == ["--verify-installed"]:
        failure = session_start()
        print(json.dumps({"passed": failure is None, "failure": failure}))
        raise SystemExit(2 if failure else 0)
    if sys.argv[1:2] in (["--reverify"], ["--measure"]):
        (version,) = sys.argv[2:]
        failure = check_capability(version, refresh=sys.argv[1] == "--reverify")
        print(json.dumps({"passed": failure is None, "failure": failure}))
        raise SystemExit(2 if failure else 0)
    _, version, binary, name, scratch = sys.argv[1:]
    try:
        failure = _probe(version, binary, name, Path(scratch))
        report = (
            {
                "passed": False,
                "check": failure.check,
                "observation": failure.observation,
            }
            if failure
            else {"passed": True, "version": version}
        )
    except Exception as error:  # noqa: BLE001 - never print potentially secret exception messages
        report = {
            "passed": False,
            "check": "probe-error",
            "observation": f"exception={type(error).__name__}",
        }
    if not report["passed"]:
        _keep_frame(name, Path(scratch))
    print(json.dumps(report))


def _keep_frame(name: str, scratch: Path) -> None:
    """The private probe pane's last frame, beside the run records the parent keeps for
    a failure: a stage that runs no injector leaves no other picture of what it saw."""
    frame = _tmux.run(
        ("tmux", "-L", name, "-f", "/dev/null", "-N"),
        ["capture-pane", "-p", "-e", "-t", "probe"],
        timeout=TEARDOWN_TIMEOUT_SECONDS,
    )
    if frame is not None and frame.returncode == 0:
        with contextlib.suppress(OSError):
            (scratch / "probe-frame.txt").write_text(frame.stdout, encoding="utf-8")


if __name__ == "__main__":
    main()
