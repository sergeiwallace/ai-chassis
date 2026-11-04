#!/usr/bin/env python3
# ruff: noqa: E402
"""Keep a persistent Dolt sql-server alive for a Beads store.

Design decision: `beads-concurrent-sync-resilience.md` D-7 (approved 2026-09-07).
The store's cost is a *cold scan per invocation*, not contention: with the engine
embedded and serverless, every ``bd`` call constructs a fresh in-process Dolt and
re-reads a 455 MB noms table across EFS at 1.2-1.3 MB/s, and NFS close-to-open
revalidation denies any session a warm cache. A server holds the table across
invocations, so the scan is paid once at startup.

Three constraints came with that approval, and each is a way this could ship and
still fail:

1. **The health check must not cost a ``bd`` call.** ``ensure`` probes a TCP socket
   and the process table; it never shells out to ``bd``. ``bd dolt status``
   measured 6 s on example-project-sagemaker even when healthy, and an unhealthy store is
   exactly when it gets slower -- so using it as the liveness probe would make the
   monitor pay the latency it exists to detect.
2. **Alert loudly; never heal silently.** Every restart and every failure writes a
   banner to stderr *and* appends to a notices file, because this store already
   lost a whole diagnosis cycle to a failure path that emitted nothing.
3. **A failed start must fall back to embedded VISIBLY.** ``ensure`` exits 0 on
   failure by design -- it must never block a session launch -- but it says so in
   full, naming the consequence. Silent re-embedding is the defect in
   ``ISSUE-REF``, where a failed ``--shared-server``
   init left a partial store that a bare ``bd init`` silently re-embedded.

Exit codes: ``ensure`` returns 0 whether or not the server came up (it is advisory
by construction). ``start``/``stop``/``status`` return non-zero on failure so a
human or a test can assert on them.
"""

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

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bd_common import RepoNotFoundError, bd_env, find_repo_root
from process_liveness import pid_alive

STATE_FILENAME = ".dolt-server-state.json"
NOTICES_DIRNAME = "dolt-server"

# The probe budget is deliberately tiny: a healthy local server answers a TCP
# connect in microseconds, so anything slower is a fault, not slowness. Keeping it
# small is what lets `ensure` sit on a session-launch path at all.
PROBE_TIMEOUT_SECONDS = 2.0

# Starting the server pays the one cold scan the whole design exists to amortise,
# so this bound is generous. It bounds a READ; the asymmetry is deliberate and is
# the same one `bd_new.py` documents -- a slow read costs a wait, an interrupted
# write costs a record.
START_TIMEOUT_SECONDS = 900
DEFAULT_LOOP_INTERVAL_SECONDS = 300

# The store-state probe talks SQL to an already-warm server, so it is bounded like a
# local round trip (32 ms measured on example-project-ec2) rather than like the cold scan
# `start` pays. Generous enough that a loaded host does not produce a false unknown,
# small enough to sit on a session-launch path.
STORE_QUERY_TIMEOUT_SECONDS = 20.0

# A local write (an UPDATE, a ``dolt_commit``) against the same warm server. It is bounded
# because a wedged server otherwise holds the writer forever (a fixture insert sat 21
# minutes, ISSUE-REF), and the bound is six times the read's because cutting a write
# costs certainty about a record, not only a wait. The publish transport's network
# writes (``dolt_fetch``, ``dolt_push``) stay unbounded and do not use this.
STORE_WRITE_TIMEOUT_SECONDS = 120.0
# The words that say what a statement does (`update issues`, `call dolt_commit`), never a
# literal it carries: restore statements carry issue notes, hex-encoded but reversible.
_STATEMENT_KIND = re.compile(r"\s*(call\s+\w+|\w+(?:\s+\w+)?)", re.IGNORECASE)


def statement_kind(sql: str) -> str:
    """What ``sql`` does, in its leading keywords only."""
    match = _STATEMENT_KIND.match(sql)
    return " ".join(match.group(1).lower().split()) if match else "statement"


class DoltQueryTimeout(RuntimeError):
    """A dolt client read got no answer from the sql-server within its bound.

    A wedged server otherwise holds the caller forever: two full suites sat 47 and 33
    minutes on one ``dolt_status`` count (ISSUE-REF). Local writes have their own bound
    and error, ``DoltWriteTimeout``.
    """

    def __init__(self, query: str, host: str, port: int, timeout: float) -> None:
        super().__init__(
            f"dolt client read got no answer from {host}:{port} within {timeout:g}s "
            f"(STORE_QUERY_TIMEOUT_SECONDS); the server may be wedged :: {query}"
        )


class DoltWriteUnknown(RuntimeError):
    """A dolt client write ended without a known result, so its outcome is UNKNOWN.

    The statement may have been applied, may still apply, or may never run. A caller must
    refuse, and never treat the write as done or as not done. Only a restart of the server
    ends the statement, which is why recovery waits for one
    (``bd_dolt_premerge_align.apply_post``). The message names the statement's kind and row
    ids, never its text.
    """

    def __init__(
        self, statement: str, rows: Sequence[str], host: str, port: int, why: str
    ) -> None:
        self.kind = statement_kind(statement)
        self.rows = tuple(rows)
        on_rows = f" on row(s) {', '.join(self.rows)}" if self.rows else ""
        super().__init__(
            f"dolt client write to {host}:{port} {why}; its outcome is unknown (the "
            f"server may or may not have applied it, and may still apply it) :: "
            f"{self.kind}{on_rows}"
        )


class DoltWriteTimeout(DoltWriteUnknown):
    """A dolt client write got no answer within its bound. Killing the client does not
    cancel the statement on the server."""

    def __init__(
        self, statement: str, rows: Sequence[str], host: str, port: int, timeout: float
    ) -> None:
        super().__init__(
            statement,
            rows,
            host,
            port,
            f"got no answer within {timeout:g}s (STORE_WRITE_TIMEOUT_SECONDS)",
        )


DOLT_BINARY = "dolt"
DOLT_CLIENT_USER = "root"

#: Where a Homebrew/manual `dolt` lands when PATH does not mention it. Probed in order,
#: after PATH, by `resolve_dolt_binary`.
#:
#: A bare "dolt" resolves only for a process whose PATH was built by a login shell.
#: ISSUE-REF was five LaunchAgents failing all 15 of their runs because launchd's PATH
#: lacks `/opt/homebrew/bin`, so the binary was present all along and every invocation
#: reported it absent. Anything on an installer or scheduler path must probe instead of
#: assuming, and the same list serves systemd (`~/.local/bin`) and Intel macOS
#: (`/usr/local/bin`) hosts -- nothing here is Darwin-only (ISSUE-REF).
DOLT_FALLBACK_PATHS = (
    "/opt/homebrew/bin/dolt",
    "/usr/local/bin/dolt",
    "~/.local/bin/dolt",
)


def resolve_dolt_binary() -> str | None:
    """The `dolt` client to invoke, or None when this machine has none.

    None means "cannot tell", never "the store is fine": a caller that cannot find the
    client has learned nothing about the remote and must say so rather than report a
    clean result.
    """
    found = shutil.which(DOLT_BINARY)
    if found:
        return found
    for candidate in DOLT_FALLBACK_PATHS:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return None


# Dolt's refusal when the working set is mid-merge. This is the exact text every
# blocked `bd` write saw during the 2026-09-25 outage.
MERGE_CONFLICT_ERROR = "Merge conflict detected"

_MERGE_STATUS_QUERY = (
    "select is_merging, source, target, unmerged_tables from dolt_merge_status"
)
# `table` is a reserved word and the column is NOT called `table_name`; querying
# `table_name` errors, which would have been read as "cannot tell" and gone quiet.
_CONFLICTS_QUERY = "select `table`, num_conflicts from dolt_conflicts"

_PORT_PATTERN = re.compile(r"(?:127\.0\.0\.1|localhost)[:\s]+(\d{2,5})\b")

# bd 1.2.2 names the port and never the host: `bd dolt start` prints
# "Dolt server started (PID 127543, port 3308)" and `bd dolt status` prints
# "Port: 3308" on its own line. Requiring a host next to the port therefore matched
# NEITHER, and `start` reported "no listening address" for a server that was up
# (ISSUE-REF). Reading a number introduced AS a port is not guessing -- `bd dolt
# --help` documents host as defaulting to 127.0.0.1, and `start`'s socket check
# disproves a wrong inference immediately. The word `port` is required precisely so
# that a stray integer (a pid, a count) can never become an endpoint.
_BARE_PORT_PATTERN = re.compile(r"\bport[:\s]+(\d{2,5})\b", re.IGNORECASE)

# bd's own default when `bd dolt set host` has not been used.
DEFAULT_HOST = "127.0.0.1"

# Where bd puts its shared-server state. These files are structured and
# single-purpose, so they are a better source than parsing bd's prose -- which is
# exactly what changed underneath this module.
SHARED_SERVER_SUBPATH = (".beads", "shared-server")
SHARED_SERVER_PORT_FILENAME = "dolt-server.port"
SHARED_SERVER_PID_FILENAME = "dolt-server.pid"
LOCAL_SERVER_CONFIG_FILENAME = "dolt-server-config.yaml"

LAUNCHD_LABEL = "com.ai-harness.beads-dolt-server"
LAUNCHD_PLIST_SUBPATH = ("Library", "LaunchAgents", f"{LAUNCHD_LABEL}.plist")
SYSTEMD_UNIT = "beads-dolt-server.service"
SYSTEMD_UNIT_SUBPATH = (".config", "systemd", "user", SYSTEMD_UNIT)
SUPERVISED_SHARED_SERVER_PORT = 3308


# bd's exact refusal when a server subcommand is issued against an embedded store.
# Measured on bd 1.1.2, 2026-09-07: `bd dolt start`, `bd dolt set` and `bd dolt test`
# all emit it. Matching on it is what lets `ensure` print the RIGHT remedy.
EMBEDDED_REFUSAL = "not supported in embedded mode"


class DoltServerError(RuntimeError):
    """A server operation failed in a way the caller must surface."""


class EmbeddedStoreError(DoltServerError):
    """The store is embedded, so no server can exist -- a different fault entirely.

    Kept distinct because the remedies do not overlap: a down server is restarted,
    while an embedded store needs a mode migration that only a human may authorise.
    Printing the restart remedy here would send a reader in a direction that cannot
    work, which is worse than printing none.
    """


@dataclass(frozen=True)
class ExternalSupervisor:
    """An installed service manager that exclusively owns the shared server."""

    name: str
    start_command: tuple[str, ...]
    restart_command: tuple[str, ...]


@dataclass(frozen=True)
class Endpoint:
    """Where the server listens, plus the pid that should be serving it."""

    host: str
    port: int
    pid: int | None

    def as_dict(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "pid": self.pid}


def shared_server_supervisor(
    home: Path | None = None,
) -> ExternalSupervisor | None:
    """Return the installed external owner of the shared server, if any.

    Installation artifacts are the authority. Merely having ``launchctl`` or
    ``systemctl`` on PATH does not mean this server is managed by it, and probing
    the live manager would make detection itself capable of touching host state.
    """
    root = home if home is not None else Path.home()
    if sys.platform == "darwin" and root.joinpath(*LAUNCHD_PLIST_SUBPATH).is_file():
        command = (
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/{LAUNCHD_LABEL}",
        )
        return ExternalSupervisor("launchd", command, command)
    if sys.platform.startswith("linux"):
        units = [root.joinpath(*SYSTEMD_UNIT_SUBPATH)]
        if home is None and os.environ.get("XDG_CONFIG_HOME"):
            units.insert(
                0,
                Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user" / SYSTEMD_UNIT,
            )
        if any(unit.is_file() for unit in units):
            return ExternalSupervisor(
                "systemd",
                ("systemctl", "--user", "start", SYSTEMD_UNIT),
                ("systemctl", "--user", "restart", SYSTEMD_UNIT),
            )
    return None


def _run_supervisor(
    supervisor: ExternalSupervisor, action: str, *, timeout: int
) -> None:
    command = (
        supervisor.start_command if action == "start" else supervisor.restart_command
    )
    manual = " ".join(command)
    try:
        result = _run_bd(
            None,
            list(command[1:]),
            timeout=timeout,
            executable=command[0],
        )
    except subprocess.TimeoutExpired as exc:
        raise DoltServerError(
            f"{supervisor.name} {action} exceeded {timeout}s; run manually: {manual}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no error text"
        raise DoltServerError(
            f"{supervisor.name} {action} failed ({detail}); run manually: {manual}"
        )


def restart_external_supervisor(
    *, home: Path | None = None, timeout: int = START_TIMEOUT_SECONDS
) -> bool:
    """Restart the installed shared-server owner; return False if none is installed."""
    supervisor = shared_server_supervisor(home)
    if supervisor is None:
        return False
    _run_supervisor(supervisor, "restart", timeout=timeout)
    return True


def _start_with_supervisor(
    repo: Path,
    supervisor: ExternalSupervisor,
    *,
    timeout: int,
    home: Path | None,
) -> Endpoint:
    started_at = time.monotonic()
    _run_supervisor(supervisor, "start", timeout=timeout)
    endpoint = shared_server_endpoint(home) or read_state(repo)
    if endpoint is None:
        endpoint = Endpoint(
            host=DEFAULT_HOST, port=SUPERVISED_SHARED_SERVER_PORT, pid=None
        )
    endpoint = Endpoint(host=endpoint.host, port=endpoint.port, pid=None)
    deadline = started_at + timeout
    while not socket_open(endpoint.host, endpoint.port):
        if time.monotonic() >= deadline:
            manual = " ".join(supervisor.start_command)
            raise DoltServerError(
                f"{supervisor.name} start succeeded but {endpoint.host}:"
                f"{endpoint.port} did not accept connections within {timeout}s; "
                f"run manually: {manual}"
            )
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    write_state(repo, endpoint)
    return endpoint


@dataclass(frozen=True)
class Health:
    """The outcome of a cheap probe. ``ok`` means usable, not merely configured."""

    ok: bool
    reason: str
    endpoint: Endpoint | None = None
    restartable: bool = True
    """Whether starting a server could plausibly fix this.

    A down server is restartable; a store whose working set refuses writes is not,
    because that state is persisted on disk and a fresh server inherits it. Without
    this distinction ``ensure`` would run ``bd dolt start`` against a server that is
    already up, find the socket accepting, and announce a ``(RE)START`` that changed
    nothing -- reporting recovery from an outage that is still in progress.
    """


def beads_dir(repo: Path) -> Path:
    return repo / ".beads"


def store_repo(repo: Path) -> Path | None:
    """The repo whose ``.beads/`` actually holds ``repo``'s store, or None if none does.

    A repo does not necessarily own its own store, so ``repo / ".beads"`` is a
    location, not a resolution. The fleet routes ``example-client-host-semantic-knowledge-graph``'s
    store to ``example-client-host-workspace``: a ``.beads/`` in the former is committed content,
    so every issue title and internal display id would become teammate-visible on
    the next PR, and it therefore must NEVER be given one.

    Assuming the store is local had three wrong consequences for that repo, on every
    single session launch. ``probe`` reported "no recorded endpoint" for a store that
    was not missing but elsewhere; ``ensure`` then tried to start a server for it;
    and the resulting notice announced an EMBEDDED store whose reads "take minutes"
    -- when there are no ``bd`` calls against this repo at all -- and prescribed a
    destructive, human-gated ``export -> re-init -> import`` over a store that does
    not exist.

    ``find_repo_root`` already resolves this, honouring ``BEADS_DIR`` exactly as
    ``bd`` does (ISSUE-REF), and its own docstring names this very topology. Reuse it
    rather than re-deriving the routing here, so the two resolvers cannot disagree
    about which store is being served.

    Returns None when nothing resolves. That is distinct from a degraded store and
    must stay quiet: there is nothing to serve, so there is nothing to warn about.
    """
    try:
        return find_repo_root(repo)
    except RepoNotFoundError:
        return None


def state_path(repo: Path) -> Path:
    return beads_dir(repo) / STATE_FILENAME


def notices_path(repo: Path, *, home: Path | None = None) -> Path:
    """Where a loud notice is persisted so it survives the emitting process.

    Deliberately NOT under ``.beads/``: a notice about the store being unusable
    should not require the store's own directory to be writable.
    """
    root = home if home is not None else Path.home()
    return root / ".claude" / "notices" / NOTICES_DIRNAME / f"{_repo_slug(repo)}.md"


def _repo_slug(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(repo)).strip("-")[-120:] or "repo"


def read_state(repo: Path) -> Endpoint | None:
    """Return the recorded endpoint, or None when absent or unreadable.

    Unreadable is treated as absent on purpose: this file is a cache, and the
    socket probe below is the authority on whether a server is usable. Failing
    closed here would refuse to serve a session whose server is in fact healthy.
    """
    try:
        raw = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    host, port, pid = raw.get("host"), raw.get("port"), raw.get("pid")
    if not isinstance(host, str) or not isinstance(port, int):
        return None
    return Endpoint(host=host, port=port, pid=pid if isinstance(pid, int) else None)


def write_state(repo: Path, endpoint: Endpoint) -> None:
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(endpoint.as_dict(), recorded_at=time.time())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def process_alive(pid: int | None) -> bool:
    """Whether ``pid`` names a live process.

    A recorded pid comes off disk, so junk is expected and is NOT the "cannot
    decide" case ``pid_alive`` fails closed on: a state file carrying ``null`` or
    ``0`` records no owner at all, and treating that as live would keep a dead
    endpoint healthy forever.

    Delegates the platform question to :mod:`process_liveness`, which never reads
    argv -- on this fleet a Dolt/bd command line can carry a run handle, and
    printing one is a secret disclosure
    (ISSUE-REF). This read ``/proc/<pid>`` alone
    until example-client-host-mac, where procfs does not exist and every live server therefore
    reported ``recorded pid N is gone``.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    return pid_alive(pid)


def socket_open(
    host: str, port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> bool:
    """Whether something accepts a TCP connection at ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def flag_is_set(value: object) -> bool:
    """Read one of Dolt's boolean columns without trusting how it was rendered.

    Measured on dolt 2.3.4, the SAME query (``select is_merging from
    dolt_merge_status``) printed ``true`` from one CLI invocation and ``1`` from
    another, and the JSON formatter renders it as the STRING ``"1"``. Comparing
    against any single one of those would leave this probe blind to the very state
    it exists to detect, so normalise instead of picking a representation.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true"}


def parse_rows(stdout: str) -> list[dict[str, object]] | None:
    """Rows from ``dolt sql -r json``, or None when the output cannot be read.

    Two shapes from dolt 2.3.4 drive this. An empty result -- an unwedged
    ``dolt_conflicts`` -- answers ``{}`` with no ``rows`` key at all, so indexing
    it would raise on exactly the healthy case. And NULL columns are OMITTED from
    each row object rather than rendered as nulls, so every field read has to
    tolerate absence.

    None and ``[]`` are kept distinct deliberately: ``[]`` means "asked, nothing
    wrong", while None means "could not tell". Collapsing them would let an
    unparseable answer read as a clean store, which is how a probe reports health
    it never established.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def store_database(repo: Path) -> str | None:
    """The Dolt database name holding this repo's store, from bd's own metadata.

    Structured signal, consistent with the rest of this module: ``metadata.json``
    records ``dolt_database`` (``AIH`` for this repo), and one server hosts several
    of them, so querying without naming the database would inspect whichever one
    the connection happened to default to.
    """
    try:
        raw = json.loads(
            (beads_dir(repo) / "metadata.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    database = raw.get("dolt_database")
    return database if isinstance(database, str) and database else None


def _dolt_client_argv(
    binary: str, endpoint: Endpoint, database: str, query: str
) -> list[str]:
    """The one client invocation shape this module speaks to the shared server.

    Factored out so the remote helpers below cannot drift into a second connection
    path: `--no-tls`, an explicit empty password and `--use-db` are all required on
    this build, and a mutation that omitted one would fail in a way that reads like a
    permissions problem.
    """
    return [
        binary,
        "--host",
        endpoint.host,
        "--port",
        str(endpoint.port),
        "--user",
        DOLT_CLIENT_USER,
        "--password",
        "",
        "--no-tls",
        "--use-db",
        database,
        "sql",
        "-r",
        "json",
        "-q",
        query,
    ]


def store_remotes(
    endpoint: Endpoint,
    database: str,
    *,
    timeout: float = STORE_QUERY_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Every Dolt remote the LIVE server holds for ``database``, or None if unanswerable.

    `dolt_remotes` is a per-database system table inside the server, so this is the
    authoritative machine-local answer. It is deliberately not the tracked
    `.beads/config.yaml`: the remote URL is per-machine (HTTPS-with-a-PAT on one host,
    an SSH host alias on another), so no tracked value can be right on both and the
    key stays out of version control entirely (ISSUE-REF, ISSUE-REF).

    None distinguishes "could not ask" (no client, server down, timeout) from the empty
    dict, which is a real answer meaning the store has no remotes at all.
    """
    proc = _query_store(
        endpoint, database, "select name, url from dolt_remotes", timeout=timeout
    )
    if proc is None or proc.returncode != 0:
        return None
    rows = parse_rows(proc.stdout)
    if rows is None:
        return None
    return {
        str(row["name"]): str(row["url"])
        for row in rows
        if row.get("name") and row.get("url")
    }


def add_store_remote(
    endpoint: Endpoint,
    database: str,
    name: str,
    url: str,
    *,
    timeout: float = STORE_QUERY_TIMEOUT_SECONDS,
) -> str | None:
    """Add remote ``name`` to the live ``database``. None on success, else the reason.

    Uses the server-side `dolt_remote` stored procedure rather than `bd dolt remote
    add`, and that choice is the whole point of this function. `bd dolt remote add`
    writes `sync.remote` into the tracked `.beads/config.yaml` AND commits it to the
    main working tree -- three such bookkeeping commits per repo were measured on
    example-client-host-mac on 2026-10-08. Since the correct URL differs per machine, those commits
    propagate one host's URL to every other host as a tracked fact. The stored
    procedure writes only the server's own `dolt_remotes` table, which is exactly the
    machine-local scope this wants (option C of ISSUE-REF, decided by the repo owner
    2026-10-08).
    """
    quoted = [_sql_quote(part) for part in ("add", name, url)]
    proc = _query_store(
        endpoint, database, f"call dolt_remote({', '.join(quoted)})", timeout=timeout
    )
    if proc is None:
        return "no dolt client, or the server did not answer"
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "").strip() or (
            f"dolt_remote('add') exited {proc.returncode}"
        )
    return None


def _sql_quote(value: str) -> str:
    """One single-quoted SQL literal. Backslash and quote both need escaping for Dolt."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _query_store(
    endpoint: Endpoint, database: str, query: str, *, timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run one query against the live server, or None if it could not run.

    Almost every caller is read-only; `add_store_remote` is the one deliberate
    exception, so that the remote mutation shares this exact connection shape instead
    of opening a second path to the same server.

    Uses the ``dolt`` client rather than ``bd`` so constraint 1 still holds: this is
    a direct SQL round trip against an already-warm server, measured at 32 ms on
    example-project-ec2, where ``bd dolt status`` measured 6 s even when healthy.

    All of ``--no-tls``, an explicit empty password and ``--use-db`` are required on
    this build; revision-database syntax fails.
    """
    binary = resolve_dolt_binary()
    if binary is None:
        return None
    try:
        return subprocess.run(
            _dolt_client_argv(binary, endpoint, database, query),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # No client binary, or it did not finish. Both mean "cannot tell", which is
        # not the same as "the store is fine" -- but the caller must not block a
        # session launch on an unanswerable question either.
        return None


def store_write_refusal(
    repo: Path,
    endpoint: Endpoint,
    *,
    timeout: float = STORE_QUERY_TIMEOUT_SECONDS,
) -> str | None:
    """Why this store refuses writes, or None when it does not (or cannot be asked).

    This is the check that was missing. ``socket_open`` answers whether the SERVER
    is up; it says nothing about whether the STORE accepts a write, and on
    2026-09-25 those two answers disagreed for hours while the probe reported only
    the first one.

    Deliberately NOT a write-and-rollback probe. That was the obvious design and it
    is provably unable to see this failure: Dolt raises ``Error 1105 Merge conflict
    detected`` when the transaction COMMITS, so a probe that rolls back never
    reaches the error. Measured on a wedged scratch server, both a zero-row
    ``delete`` and an explicit ``start transaction; insert; rollback`` returned
    rc=0 while a real insert returned rc=1. A probe that is harmless because it
    rolls back and a probe that detects this because it commits are mutually
    exclusive, so this reads the store's declared state instead -- which is both
    harmless and diagnostic.

    Fails OPEN on anything it cannot read. The socket and pid checks already cover a
    server that is down, and this module must never refuse a session whose store is
    in fact healthy; the failure being fixed is a false NEGATIVE, so the fix must
    not introduce false positives that block every launch.
    """
    database = store_database(repo)
    if database is None:
        # Nothing names a database to inspect, so there is no question to ask here.
        return None
    return _merge_refusal(endpoint, database, timeout) or _conflict_refusal(
        endpoint, database, timeout
    )


def _merge_refusal(endpoint: Endpoint, database: str, timeout: float) -> str | None:
    """An unfinished merge holding the working set open, described for a reader."""
    status = _query_store(endpoint, database, _MERGE_STATUS_QUERY, timeout=timeout)
    if status is None:
        return None
    if MERGE_CONFLICT_ERROR in f"{status.stdout}\n{status.stderr}":
        # The store objected in the very terms every blocked caller saw. Believe it.
        return (
            "store refuses writes: the server reported a merge conflict "
            f"({MERGE_CONFLICT_ERROR})"
        )
    rows = parse_rows(status.stdout)
    if not rows or not flag_is_set(rows[0].get("is_merging")):
        return None
    row = rows[0]
    # Both refs are named because the remedy differs by which merge this is, and an
    # alert that cannot say what to abort leaves a reader guessing under pressure.
    return (
        "store refuses writes: an unfinished merge of "
        f"{row.get('source') or 'an unnamed ref'} into "
        f"{row.get('target') or 'an unnamed ref'} is still in progress "
        f"(unmerged tables: {row.get('unmerged_tables') or 'none reported'})"
    )


def _conflict_refusal(endpoint: Endpoint, database: str, timeout: float) -> str | None:
    """Conflicts persisted in the store with no merge still active.

    A separate axis from ``_merge_refusal``: conflicts can outlive the merge that
    created them if a client committed them, so checking only ``is_merging`` would
    miss a store that is just as unwritable.
    """
    conflicts = _query_store(endpoint, database, _CONFLICTS_QUERY, timeout=timeout)
    if conflicts is None:
        return None
    rows = parse_rows(conflicts.stdout)
    if not rows:
        # None (unreadable) and [] (genuinely clean) both mean "no refusal to
        # report": an unreadable answer must not become an alert, per fail-open.
        return None
    blocked = sorted(
        str(row.get("table")) for row in rows if flag_is_set(row.get("num_conflicts"))
    )
    if not blocked:
        return None
    return (
        "store refuses writes: unresolved merge conflicts persist in "
        f"{', '.join(blocked)}"
    )


def probe(repo: Path, *, home: Path | None = None) -> Health:
    """Cheap liveness check. Costs no ``bd`` invocation (constraint 1)."""
    conflict = server_topology_conflict(repo)
    if conflict is not None:
        return Health(ok=False, reason=conflict)
    endpoint = read_state(repo)
    if endpoint is None:
        # A server started by a bare `bd dolt start` leaves no state file here, and
        # it is still a healthy server. Fall back to bd's own recorded endpoint
        # before declaring the store degraded (ISSUE-REF).
        if repo_uses_shared_server(repo):
            endpoint = shared_server_endpoint(home)
    if endpoint is None:
        return Health(ok=False, reason="no recorded endpoint")
    if endpoint.pid is not None and not process_alive(endpoint.pid):
        return Health(
            ok=False,
            reason=f"recorded pid {endpoint.pid} is gone",
            endpoint=endpoint,
        )
    if not socket_open(endpoint.host, endpoint.port):
        return Health(
            ok=False,
            reason=f"nothing listening on {endpoint.host}:{endpoint.port}",
            endpoint=endpoint,
        )
    # A socket that accepts is where this probe used to stop, and that is the defect:
    # the server can be up and answering SELECTs while the store refuses every write.
    refusal = store_write_refusal(repo, endpoint)
    if refusal is not None:
        return Health(ok=False, reason=refusal, endpoint=endpoint, restartable=False)
    return Health(ok=True, reason="socket accepted", endpoint=endpoint)


def _run_bd(
    repo: Path | None,
    args: list[str],
    *,
    timeout: int,
    executable: str = "bd",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        cwd=str(repo) if repo is not None else None,
        env=bd_env() if executable == "bd" else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_endpoint(text: str) -> Endpoint | None:
    """Extract a host:port from ``bd dolt status``/``show`` output.

    Returns None rather than guessing: a wrong port would make the probe report a
    healthy server as dead forever, which is worse than having no cache at all.
    """
    match = _PORT_PATTERN.search(text)
    if match is not None:
        host_text = match.group(0).split(":")[0].split()[0]
        host = DEFAULT_HOST if host_text == "localhost" else host_text
        return Endpoint(host=host, port=int(match.group(1)), pid=None)
    bare = _BARE_PORT_PATTERN.search(text)
    if bare is None:
        return None
    return Endpoint(host=DEFAULT_HOST, port=int(bare.group(1)), pid=None)


def shared_server_dir(home: Path | None = None) -> Path:
    """Where bd keeps shared-server state, which is outside the repo entirely."""
    root = home if home is not None else Path.home()
    return root.joinpath(*SHARED_SERVER_SUBPATH)


def repo_uses_shared_server(repo: Path) -> bool:
    """Whether THIS repo's store is the shared server, read from bd's own config.

    Without this gate the shared-server fallback below would answer for a repo that
    does not use it: one machine can hold both an embedded store and a server-mode
    one, and ``$HOME/.beads/shared-server`` is shared by every repo that does. A
    probe of the embedded repo would then report the OTHER repo's healthy server --
    a false positive, and the exact mistake this module's docstring warns about in
    the opposite direction ("a wrong port would make the probe report a healthy
    server as dead forever").

    Structured signal first. Cheap: two small file reads, no ``bd`` call.
    """
    try:
        raw = json.loads((beads_dir(repo) / "metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    else:
        if isinstance(raw, dict) and raw.get("dolt_mode") == "server":
            return True
    try:
        config = (beads_dir(repo) / "config.yaml").read_text()
    except OSError:
        return False
    return any(
        line.split("#", 1)[0].strip().replace(" ", "") == "dolt.shared-server:true"
        for line in config.splitlines()
    )


def server_topology_conflict(repo: Path) -> str | None:
    """Describe a repo-local server config that contradicts shared-server mode.

    bd's shared-server files are machine-global while its direct SQL-server
    lifecycle writes a config in ``.beads``. When both claims exist, a socket
    accepted on the shared endpoint cannot establish that it serves this repo's
    database. Do not select or start either endpoint automatically.
    """
    if not (beads_dir(repo) / LOCAL_SERVER_CONFIG_FILENAME).is_file():
        return None
    if not repo_uses_shared_server(repo):
        return None
    return (
        "repo-local dolt-server-config.yaml conflicts with shared-server mode; "
        "refusing to infer this repo's database endpoint"
    )


def shared_server_endpoint(home: Path | None = None) -> Endpoint | None:
    """The endpoint bd itself recorded for the shared server, or None.

    Reads bd's own ``dolt-server.port``/``dolt-server.pid`` rather than parsing its
    output. This is what lets a probe recognise a server THIS module did not start:
    the state file under ``.beads/`` is written only by ``start`` below, so before
    ISSUE-REF a server started by a bare ``bd dolt start`` was reported as
    "no recorded endpoint" while it was serving normally.

    Two small file reads, so it does not breach constraint 1 -- and it is strictly
    cheaper than the ``bd dolt status`` fallback this module already paid at start.
    """
    shared = shared_server_dir(home)
    try:
        port = int((shared / SHARED_SERVER_PORT_FILENAME).read_text().strip())
    except (OSError, ValueError):
        return None
    pid: int | None
    try:
        pid = int((shared / SHARED_SERVER_PID_FILENAME).read_text().strip())
    except (OSError, ValueError):
        pid = None
    return Endpoint(host=DEFAULT_HOST, port=port, pid=pid)


def server_pid(repo: Path, *, home: Path | None = None) -> int | None:
    """The pid of a dolt server for this store, from bd's own pidfile if present.

    Checks the shared-server pidfile first: in server mode bd writes it OUTSIDE the
    repo, so the repo-local glob below found nothing for the one mode this module
    exists to supervise (ISSUE-REF). The glob remains for embedded-mode stores,
    where the exact filename is not part of bd's published interface.
    """
    if repo_uses_shared_server(repo):
        shared = shared_server_endpoint(home)
        if shared is not None and process_alive(shared.pid):
            return shared.pid
    for candidate in sorted(beads_dir(repo).glob("*.pid")):
        try:
            pid = int(candidate.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if process_alive(pid):
            return pid
    return None


def start(
    repo: Path, *, timeout: int = START_TIMEOUT_SECONDS, home: Path | None = None
) -> Endpoint:
    """Start the server and record where it listens.

    Raises DoltServerError when the server does not come up, so the caller decides
    whether that is fatal. ``ensure`` deliberately treats it as non-fatal.
    """
    supervisor = (
        shared_server_supervisor(home) if repo_uses_shared_server(repo) else None
    )
    if supervisor is not None:
        return _start_with_supervisor(repo, supervisor, timeout=timeout, home=home)

    try:
        started = _run_bd(repo, ["dolt", "start"], timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
        raise DoltServerError(
            f"bd dolt start exceeded {timeout}s (the one-time cold scan may still "
            "be running; re-run before assuming failure)"
        ) from exc
    combined = f"{started.stdout}\n{started.stderr}"
    if EMBEDDED_REFUSAL in combined:
        # Distinguishing this from "the server is down" is the whole point: the two
        # look identical from the outside and have completely different remedies.
        raise EmbeddedStoreError(
            "this store is in EMBEDDED mode, so bd refuses every server subcommand; "
            "no daemon can be started against it"
        )
    endpoint = parse_endpoint(combined)
    if endpoint is None:
        # bd's own state files before its prose: structured beats parsed.
        if repo_uses_shared_server(repo):
            endpoint = shared_server_endpoint(home)
    if endpoint is None:
        # Fall back to asking bd where it listens. This is a start-time cost, not a
        # health-check cost, so it does not violate constraint 1.
        with contextlib.suppress(subprocess.TimeoutExpired):
            shown = _run_bd(repo, ["dolt", "status"], timeout=120)
            endpoint = parse_endpoint(f"{shown.stdout}\n{shown.stderr}")
    if endpoint is None:
        raise DoltServerError(
            "server start reported no listening address; "
            f"bd dolt start exited {started.returncode}"
        )
    endpoint = Endpoint(
        host=endpoint.host, port=endpoint.port, pid=server_pid(repo, home=home)
    )
    write_state(repo, endpoint)
    if not socket_open(endpoint.host, endpoint.port):
        raise DoltServerError(
            f"recorded {endpoint.host}:{endpoint.port} but nothing accepts a connection"
        )
    return endpoint


_DIRTY_STATUS_QUERY = "select count(*) as n from dolt_status"


def dirty_working_sets(
    repo: Path, *, home: Path | None = None, timeout: float = 15.0
) -> list[str]:
    """The served databases whose working set differs from HEAD, or cannot be read.

    ``bd dolt stop`` runs ``CALL DOLT_COMMIT('-Am', 'auto-flush: commit working set before
    server stop')`` on the databases it serves (the string is in the bd binary), so a stop
    COMMITS whatever the working set holds. A working set that has drifted from HEAD is
    therefore published as truth: on 2026-10-05 00:57:00 UTC one stop committed a stale
    working set onto four stores and rewrote their issues tables from an older snapshot
    (ISSUE-REF). Nothing was added, only older rows and deleted closed issues, so every
    store looked healthy until someone diffed it.

    An unreadable database is reported rather than skipped: this guard fails closed, because
    "could not tell" is exactly the state in which a stale set gets committed. Returns an
    empty list when no server is listening, since then there is nothing to flush.
    """
    endpoint = read_state(repo)
    if endpoint is None and repo_uses_shared_server(repo):
        endpoint = shared_server_endpoint(home)
    if endpoint is None or not socket_open(endpoint.host, endpoint.port):
        return []
    databases: list[str] = []
    if repo_uses_shared_server(repo):
        data_dir = shared_server_dir(home) / "dolt"
        if data_dir.is_dir():
            databases = sorted(
                entry.name
                for entry in data_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
    else:
        own = store_database(repo)
        databases = [own] if own else []
    dirty: list[str] = []
    for database in databases:
        answer = _query_store(endpoint, database, _DIRTY_STATUS_QUERY, timeout=timeout)
        rows = parse_rows(answer.stdout) if answer is not None else None
        if not rows:
            dirty.append(f"{database} (working set unreadable)")
        elif str(rows[0].get("n", "")).strip() not in {"", "0"}:
            dirty.append(f"{database} ({rows[0].get('n')} table(s) differ from HEAD)")
    return dirty


def stop(repo: Path, *, timeout: int = 300, home: Path | None = None) -> bool:
    """Stop the server. Returns whether bd reported success.

    Refuses, raising :class:`DoltServerError`, while any served database has a working set
    that differs from HEAD: the stop would commit it (see :func:`dirty_working_sets`). The
    remedy is to publish first (``scripts/bd_dolt_publish.py`` settles derived columns and
    commits real work deliberately) or to inspect ``dolt_status`` and ``dolt_diff``, never to
    stop and hope. There is no override flag; a store that cannot be made clean is a finding.
    """
    dirty = dirty_working_sets(repo, home=home)
    if dirty:
        raise DoltServerError(
            "refusing to stop the Dolt server: `bd dolt stop` would auto-commit these "
            f"working sets as they stand: {', '.join(dirty)}. Publish each store with "
            "scripts/bd_dolt_publish.py, or diff dolt_status first. (ISSUE-REF)"
        )
    try:
        result = _run_bd(repo, ["dolt", "stop"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    with contextlib.suppress(OSError):
        state_path(repo).unlink(missing_ok=True)
    return result.returncode == 0


def _banner(lines: list[str]) -> str:
    width = max(len(line) for line in lines)
    rule = "=" * min(width, 100)
    return "\n".join([rule, *lines, rule])


def emit_notice(
    repo: Path, lines: list[str], *, home: Path | None = None
) -> Path | None:
    """Write a loud notice to stderr AND persist it (constraint 2).

    Persisting matters because the process that notices is usually a hook whose
    stderr nobody reads. Returns the notice path, or None if it could not be
    written -- a failure to persist must not suppress the stderr half.
    """
    print(_banner(lines), file=sys.stderr)
    path = notices_path(repo, home=home)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n## {stamp}\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
            )
    except OSError:
        return None
    return path


_SHARED_CONSEQUENCE = [
    "CONSEQUENCE: every bd call rebuilds an in-process Dolt and rescans",
    "the store, so reads take minutes and rc=124 stops meaning failure.",
    "Do NOT bound a bd write on this path (see bd_new.py's unbounded-write note).",
]


def _failure_notice(repo: Path, health: Health, exc: DoltServerError) -> list[str]:
    """Build the notice, choosing the remedy that can actually work.

    The two failures are indistinguishable from the outside and have disjoint
    remedies, so a single generic notice necessarily prints a wrong one for one of
    them. Telling a reader to restart a server that cannot exist sends them down a
    path with no end, which is worse than admitting the tool cannot fix it.
    """
    head = [
        "DOLT SERVER UNAVAILABLE -- falling back to the EMBEDDED engine.",
        f"repo: {repo}",
        f"probe said: {health.reason}",
        f"start failed: {exc}",
        *_SHARED_CONSEQUENCE,
    ]
    if isinstance(exc, EmbeddedStoreError):
        return [
            *head,
            "NO RESTART CAN FIX THIS: bd selects server mode at INIT time only",
            "(--shared-server / --server / --proxied-server-config-path), so reaching",
            "it means export -> re-init -> import over the live store.",
            "That is destructive and irreversible, so it is HUMAN-GATED: do not run it",
            "unattended. See beads-concurrent-sync-resilience.md D-7.",
        ]
    return [*head, "Remedy: python3 scripts/dolt_server.py start --repo <repo>"]


def _write_refusal_notice(repo: Path, health: Health) -> list[str]:
    """The notice for a server that is UP while its store refuses every write.

    Deliberately not the embedded-fallback notice: nothing has fallen back here, so
    naming the embedded engine would describe a machine state that does not exist.
    The consequence is what makes this urgent and it is not obviously about Beads at
    all -- `bd-invariants` reads the store on pre-push, so the visible symptom is
    that `git push` fails everywhere, which is how this went three sessions with a
    human eventually noticing.

    Names no flag that forces the write path open. There is one, and setting it
    commits the conflicted state rather than repairing it, so it must not appear in
    a notice a reader may follow under pressure.
    """
    return [
        "DOLT STORE REFUSING WRITES -- the server is UP and answering reads.",
        f"repo: {repo}",
        f"probe said: {health.reason}",
        "CONSEQUENCE: every bd write fails, and because bd-invariants reads the",
        "store on pre-push, EVERY git push in this repository fails until it clears.",
        "NO RESTART CAN FIX THIS: the state lives in the branch's working set on",
        "disk, so a newly started server inherits it unchanged.",
        "HUMAN-GATED: inspect dolt_merge_status and dolt_conflicts, then clear the",
        "merge deliberately -- dolt_merge('--abort') picks NEITHER side -- or",
        "resolve the conflicts. Do NOT force the write path open instead: that",
        "commits the broken state rather than repairing it.",
    ]


def ensure(repo: Path, *, home: Path | None = None) -> Health:
    """Idempotent entry point for launchers and hooks.

    Never raises and never blocks the caller: a session must be able to start even
    when the store is degraded. What it will not do is stay quiet about it.

    Resolve which repo actually holds the store BEFORE probing, because a routed or
    absent store is not a degraded one -- see ``store_repo``. Only ``ensure`` routes:
    ``probe`` and ``start`` keep taking the repo they are given, so a caller that has
    already resolved a target can still address it directly.
    """
    target = store_repo(repo)
    if target is None:
        # No store is routed here, so there is no server to run and nothing to say.
        # Warning here would fire on every launch of a repo that is correctly
        # configured, and its remedy would name a store that does not exist.
        return Health(ok=True, reason="no beads store is routed to this repo")
    repo = target
    conflict = server_topology_conflict(repo)
    if conflict is not None:
        health = Health(ok=False, reason=conflict)
        emit_notice(
            repo,
            [
                "DOLT SERVER TOPOLOGY CONFLICT -- no automatic start attempted.",
                f"repo: {repo}",
                f"probe said: {conflict}",
                "A shared endpoint can accept TCP while lacking this repository's database.",
                "HUMAN-GATED: inspect both stores and follow the split-brain remediation plan "
                "before changing processes or data.",
            ],
            home=home,
        )
        return health
    health = probe(repo, home=home)
    if health.ok:
        return health
    if not health.restartable:
        # Constraint 2, the half that was missing: be loud, and do not heal silently.
        # Restarting here would find the socket still accepting and announce a
        # recovery, so the one thing that must not happen is an automatic attempt.
        emit_notice(repo, _write_refusal_notice(repo, health), home=home)
        return health
    try:
        endpoint = start(repo, home=home)
    except DoltServerError as exc:
        emit_notice(repo, _failure_notice(repo, health, exc), home=home)
        return Health(ok=False, reason=str(exc))
    emit_notice(
        repo,
        [
            "DOLT SERVER (RE)STARTED by dolt_server.py ensure.",
            f"repo: {repo}",
            f"why: {health.reason}",
            f"now listening: {endpoint.host}:{endpoint.port} (pid {endpoint.pid})",
        ],
        home=home,
    )
    return Health(ok=True, reason="started by ensure", endpoint=endpoint)


def supervise(
    repo: Path,
    *,
    interval_seconds: int = DEFAULT_LOOP_INTERVAL_SECONDS,
    max_iterations: int | None = None,
    ensure_runner=ensure,
    sleeper=time.sleep,
) -> None:
    """Periodically run the advisory check without turning degradation into a crash.

    Circus respawns a process which exits.  Keeping the interval here means a
    degraded (including embedded) store cannot turn a respawning watcher into a
    tight loop.  ``ensure`` remains the single place that starts or visibly
    degrades the store; this wrapper deliberately performs no store mutation.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        ensure_runner(repo)
        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            sleeper(interval_seconds)


def _resolve_repo(raw: str | None) -> Path:
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def main(  # noqa: C901, PLR0911 -- fixed, closed subcommand dispatch: one branch and one exit status per command
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=("ensure", "loop", "mode", "start", "stop", "status")
    )
    parser.add_argument("--repo", help="Beads store repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_SECONDS,
        help="seconds between loop checks (default: %(default)s)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="bound loop passes for testing; omit to run until supervised",
    )
    args = parser.parse_args(argv)
    repo = _resolve_repo(args.repo)

    if args.command == "loop":
        try:
            supervise(
                repo,
                interval_seconds=args.interval_seconds,
                max_iterations=args.max_iterations,
            )
        except ValueError as exc:
            parser.error(str(exc))
        return 0

    if args.command == "mode":
        # Exposes repo_uses_shared_server() to shell callers, so a caller that must
        # branch on store mode reuses THIS predicate instead of re-greppping the
        # config for `dolt.shared-server:true`. A hand-rolled copy loses the
        # metadata.json arm and the comment-stripping, and a mode check that looks in
        # the wrong place does not fail closed -- it reports "embedded" and lets the
        # caller act on a shared-server store. Pure file reads, no network probe: the
        # question is which store this repo uses, not whether it is healthy.
        shared = repo_uses_shared_server(repo)
        if args.json:
            print(json.dumps({"shared_server": shared}))
        else:
            print("shared-server" if shared else "embedded")
        return 0 if shared else 1

    if args.command == "status":
        health = probe(repo)
        payload = {
            "ok": health.ok,
            "reason": health.reason,
            "endpoint": health.endpoint.as_dict() if health.endpoint else None,
        }
        print(json.dumps(payload, indent=2) if args.json else _status_text(health))
        return 0 if health.ok else 1

    if args.command == "stop":
        try:
            return 0 if stop(repo) else 1
        except DoltServerError as exc:
            print(f"dolt_server stop: {exc}", file=sys.stderr)
            return 2

    if args.command == "start":
        try:
            endpoint = start(repo)
        except DoltServerError as exc:
            print(f"dolt_server: {exc}", file=sys.stderr)
            return 1
        print(f"dolt_server: listening on {endpoint.host}:{endpoint.port}")
        return 0

    health = ensure(repo)
    if not args.json and health.ok:
        print(f"dolt_server: healthy ({health.reason})")
    elif args.json:
        print(json.dumps({"ok": health.ok, "reason": health.reason}))
    # ensure is advisory: exit 0 even when degraded, having already said so loudly.
    return 0


def _status_text(health: Health) -> str:
    where = (
        f"{health.endpoint.host}:{health.endpoint.port}"
        if health.endpoint
        else "unknown"
    )
    if health.ok:
        verdict = "HEALTHY"
    elif not health.restartable:
        # Saying "embedded fallback in effect" here would be false: the server is up
        # and serving reads, so nothing has fallen back. Naming the wrong machine
        # state sends a reader looking for a process that is running fine.
        verdict = "DEGRADED (server UP, store REFUSING WRITES)"
    else:
        verdict = "DEGRADED (embedded fallback in effect)"
    return f"dolt_server: {verdict} at {where} -- {health.reason}"


if __name__ == "__main__":
    raise SystemExit(main())
