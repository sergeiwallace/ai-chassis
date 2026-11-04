"""Post-boot verification: one PASS/FAIL/SKIP record per system, for every boot (ISSUE-REF).

``ai-harness-boot-check.service`` runs ``python -m ai_chassis.boot_check`` once per boot: it waits,
bounded, for the network and Docker, lets the restart-policy containers and the Postgres client daemons
settle, then probes each system by its own end-to-end check and appends one JSON row per check to
``<state>/ai-harness/boot-check/<boot_id>.jsonl`` plus a human summary to ``<boot_id>.txt``
(``<state>`` is ``$XDG_STATE_HOME``, default ``~/.local/state``). ``ai-harness boot-check --now``
runs the same checks by hand without the wait.

Each row is ``{boot_id, at, system, check, verdict, evidence, remedy}``. A check that raises is a
FAIL row naming the exception, never the end of the run. The exit code is 0 when every row is PASS
or SKIP and 1 otherwise, so ``systemctl --user --failed`` lists a failed boot check too.

Credential checks record reachability only: the AWS check keeps the caller's account id and the
credo check keeps its service row's status words; every evidence string also passes through
:func:`redact`. Every subprocess goes through one injected :data:`Runner`, which is the seam the
tests replace. Settings: ``config/boot-check.toml``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import aws_profile, host_boot
from .docker_roots import DEFAULT_CONTAINERD_ROOT, containerd_config_path, targets
from .storage import (
    DOCKER_ROOT_PLACEMENT,
    NotDeclared,
    Runner,
    StorageDeclaration,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
UNIT = "ai-harness-boot-check.service"
HARNESS_ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path("config") / "boot-check.toml"
ETC_ROOT = Path("/etc")
REDACTED = "<redacted>"
BUG_FIX = "/bug-fix with this row"

_SECRET_SHAPES = (
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(
        r"""(?i)("?(?:secret[_-]?access[_-]?key|session[_-]?token|access[_-]?token|"""
        r"""refresh[_-]?token|password|token)"?\s*[:=]\s*)("[^"]*"|\S+)"""
    ),
    # Headers and key=value forms whose values are often far shorter than 40 characters.
    re.compile(r"""(?i)\b((?:set-)?cookie"?\s*[:=]\s*)([^\r\n]+)"""),
    re.compile(
        r"""(?i)\b((?:x-)?(?:api[_-]?key|apikey)"?\s*[:=]\s*)("[^"]*"|[^\s;,]+)"""
    ),
    re.compile(
        r"""(?i)\b(authorization"?\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)("[^"]*"|\S+)"""
    ),
    re.compile(r"""(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{6,})"""),
    re.compile(r"[A-Za-z0-9+/=]{40,}"),
)
_LOG_LINE = re.compile(r"^(\S+)\s+(\S+)\s+key=(\S+)")
_RESUME_STAGE = re.compile(r"^(?:REBOOT-SWEEP|CC-LAUNCH-RESUME-)")
_DELIVERED_STAGES = ("CC-LAUNCH-RESUME-NATIVE-CONSUMED", "CC-LAUNCH-RESUME-FIRED")
_PID = re.compile(r"pid=(\d+)")
_CAPPED_DRIVERS = frozenset({"local", "journald", "none"})
_COMPOSE_PROJECT = "com.docker.compose.project"


def redact(text: str) -> str:
    """``text`` with every token-, key- and password-shaped value replaced by ``<redacted>``."""
    for shape in _SECRET_SHAPES:
        if shape.groups:
            text = shape.sub(rf"\1{REDACTED}", text)
        else:
            text = shape.sub(REDACTED, text)
    return text


@dataclass(frozen=True)
class Row:
    system: str
    check: str
    verdict: str
    evidence: str
    remedy: str = ""


@dataclass
class Context:
    run: Runner
    machine: str
    harness_root: Path
    settings: Mapping[str, Any]
    declaration: StorageDeclaration | NotDeclared | None
    boot: host_boot.BootIdentity
    home: Path = field(default_factory=Path.home)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def call(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=float(self.settings.get("probe_timeout_seconds", 120)),
            **({"env": dict(env)} if env is not None else {}),
        )


def _detail(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    first = text.splitlines()[0] if text else ""
    return f"rc={completed.returncode}" + (f": {first[:200]}" if first else "")


def _properties(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)


def _placement(ctx: Context, name: str):
    if not isinstance(ctx.declaration, StorageDeclaration):
        return None
    return ctx.declaration.placements.get(name)


def _docker_state(ctx: Context) -> tuple[str, str]:
    """(LoadState, ActiveState) of docker.service, read without contacting the daemon."""
    completed = ctx.call(
        ["systemctl", "show", "docker.service", "-p", "LoadState", "-p", "ActiveState"]
    )
    values = _properties(completed.stdout)
    return values.get("LoadState", "unknown"), values.get("ActiveState", "unknown")


def _docker_unavailable(ctx: Context, system: str, check: str) -> Row | None:
    """A SKIP row when Docker is not installed or not running; the engine row carries the FAIL."""
    load, active = _docker_state(ctx)
    if load == "not-found":
        return Row(system, check, SKIP, "docker.service is not installed")
    if active != "active":
        return Row(
            system, check, SKIP, f"docker.service is {active}; see docker/engine-root"
        )
    return None


# --- the checks --------------------------------------------------------------------------------


def check_storage(ctx: Context) -> list[Row]:
    if not isinstance(ctx.declaration, StorageDeclaration):
        return [
            Row("storage", "volumes", SKIP, f"no storage is declared for {ctx.machine}")
        ]
    rows = []
    for volume in ctx.declaration.volumes:
        check = f"mount {volume.mount}"
        completed = ctx.call(
            [
                "findmnt",
                "-J",
                "-o",
                "TARGET,SOURCE,FSTYPE",
                "--mountpoint",
                str(volume.mount),
            ]
        )
        if completed.returncode != 0:
            rows.append(
                Row(
                    "storage",
                    check,
                    FAIL,
                    f"{volume.mount} is not a mount point ({_detail(completed)})",
                    f"`findmnt {volume.mount}`, `journalctl -b | grep -i mount`; {BUG_FIX}",
                )
            )
            continue
        found = (json.loads(completed.stdout).get("filesystems") or [{}])[0]
        evidence = (
            f"{found.get('target')} from {found.get('source')}, {found.get('fstype')}"
        )
        want = volume.verify.get("fstype")
        if want and found.get("fstype") != want:
            rows.append(
                Row(
                    "storage",
                    check,
                    FAIL,
                    f"{evidence}; declared {want}",
                    f"the wrong filesystem is mounted at {volume.mount}; {BUG_FIX}",
                )
            )
            continue
        rows.append(Row("storage", check, PASS, evidence))
    return rows


def check_mount_ordering(ctx: Context) -> list[Row]:
    placement = _placement(ctx, DOCKER_ROOT_PLACEMENT)
    if placement is None:
        return [
            Row(
                "storage",
                "docker/containerd ordered after the mount",
                SKIP,
                "no docker_root placement is declared",
            )
        ]
    mount = ctx.declaration.volume(placement.volume).mount
    rows = []
    for unit in ("docker.service", "containerd.service"):
        completed = ctx.call(["systemctl", "show", unit, "-p", "RequiresMountsFor"])
        paths = _properties(completed.stdout).get("RequiresMountsFor", "").split()
        ordered = [p for p in paths if Path(p) == mount or mount in Path(p).parents]
        check = f"{unit} RequiresMountsFor {mount}"
        if ordered:
            rows.append(
                Row("storage", check, PASS, f"RequiresMountsFor={' '.join(paths)}")
            )
        else:
            rows.append(
                Row(
                    "storage",
                    check,
                    FAIL,
                    f"RequiresMountsFor={' '.join(paths) or '(none)'}",
                    f"install the {unit} drop-in with RequiresMountsFor={placement.path} "
                    "(ISSUE-REF), or the daemon can start on empty roots on /",
                )
            )
    return rows


def check_docker_engine(ctx: Context) -> list[Row]:
    load, active = _docker_state(ctx)
    if load == "not-found":
        return [Row("docker", "engine-root", SKIP, "docker.service is not installed")]
    placement = _placement(ctx, DOCKER_ROOT_PLACEMENT)
    if active != "active":
        return [
            Row(
                "docker",
                "engine-root",
                FAIL,
                f"docker.service is {active}",
                f"`systemctl status docker.service`, `journalctl -b -u docker.service`; {BUG_FIX}",
            )
        ]
    completed = ctx.call(
        ["docker", "info", "--format", "{{.DockerRootDir}}\t{{.LoggingDriver}}"]
    )
    if completed.returncode != 0:
        return [
            Row(
                "docker",
                "engine-root",
                FAIL,
                f"docker info failed ({_detail(completed)})",
                BUG_FIX,
            )
        ]
    root, _, driver = completed.stdout.strip().partition("\t")
    rows = []
    if placement is None:
        rows.append(Row("docker", "engine-root", PASS, f"DockerRootDir {root}"))
    else:
        want = targets(placement.path)[0]
        rows.append(
            Row("docker", "engine-root", PASS, f"DockerRootDir {root}")
            if Path(root) == want
            else Row(
                "docker",
                "engine-root",
                FAIL,
                f"DockerRootDir {root}, expected {want}",
                "Docker started on the wrong root: check the /data mount came up before "
                f"docker.service; docs/procedures/docker-root-migration-runbook.md; {BUG_FIX}",
            )
        )
    wanted = (
        ctx.declaration.docker_default_log_driver
        if isinstance(ctx.declaration, StorageDeclaration)
        else None
    )
    if wanted and driver != wanted:
        rows.append(
            Row(
                "docker",
                "default log driver",
                FAIL,
                f"LoggingDriver {driver}, declared {wanted}",
                "`ai-harness apply --only storage` merges it; a daemon restart applies it",
            )
        )
    else:
        rows.append(
            Row("docker", "default log driver", PASS, f"LoggingDriver {driver}")
        )
    return rows


def check_containerd_root(ctx: Context) -> list[Row]:
    placement = _placement(ctx, DOCKER_ROOT_PLACEMENT)
    if placement is None:
        return [Row("containerd", "root", SKIP, "no docker_root placement is declared")]
    want = targets(placement.path)[1]
    config = containerd_config_path(ETC_ROOT)
    completed = ctx.call(["containerd", "--config", str(config), "config", "dump"])
    if completed.returncode != 0:
        return [
            Row(
                "containerd",
                "root",
                FAIL,
                f"containerd config dump failed ({_detail(completed)})",
                BUG_FIX,
            )
        ]
    root = tomllib.loads(completed.stdout).get("root") or str(DEFAULT_CONTAINERD_ROOT)
    if Path(root) == want:
        return [Row("containerd", "root", PASS, f"root {root}")]
    return [
        Row(
            "containerd",
            "root",
            FAIL,
            f"root {root}, expected {want}",
            f"docs/procedures/docker-root-migration-runbook.md; {BUG_FIX}",
        )
    ]


def _container_ok(state: str, status: str) -> bool:
    if state == "running":
        return "(unhealthy)" not in status and "(health: starting)" not in status
    # A compose one-shot (minio-init) exits 0 by design.
    return state == "exited" and status.startswith("Exited (0)")


def check_compose_stacks(ctx: Context) -> list[Row]:
    unavailable = _docker_unavailable(ctx, "docker", "compose stacks")
    if unavailable:
        return [unavailable]
    completed = ctx.call(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={_COMPOSE_PROJECT}",
            "--format",
            "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "
            f'"{_COMPOSE_PROJECT}"'
            "}}",
        ]
    )
    if completed.returncode != 0:
        return [
            Row(
                "docker",
                "compose stacks",
                FAIL,
                f"docker ps failed ({_detail(completed)})",
                BUG_FIX,
            )
        ]
    projects: dict[str, list[tuple[str, str, str]]] = {}
    for line in completed.stdout.splitlines():
        name, state, status, project = (line.split("\t") + ["", "", "", ""])[:4]
        if name:
            projects.setdefault(project, []).append((name, state, status))
    if not projects:
        return [Row("docker", "compose stacks", SKIP, "no compose containers exist")]
    rows = []
    for project, containers in sorted(projects.items()):
        bad = [c for c in containers if not _container_ok(c[1], c[2])]
        listing = "; ".join(f"{n}: {s}" for n, _, s in sorted(containers))
        rows.append(
            Row(
                "docker",
                f"compose stack {project}",
                FAIL if bad else PASS,
                listing,
                (
                    f"`docker compose -p {project} ps`, then `docker logs <container>` for "
                    f"{', '.join(n for n, _, _ in bad)}; {BUG_FIX}"
                )
                if bad
                else "",
            )
        )
    return rows


def check_log_drivers(ctx: Context) -> list[Row]:
    unavailable = _docker_unavailable(ctx, "docker", "container log drivers")
    if unavailable:
        return [unavailable]
    ids = ctx.call(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        return [Row("docker", "container log drivers", SKIP, "no running containers")]
    completed = ctx.call(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Name}}\t{{.HostConfig.LogConfig.Type}}\t{{json .HostConfig.LogConfig.Config}}",
            *ids,
        ]
    )
    if completed.returncode != 0:
        return [
            Row(
                "docker",
                "container log drivers",
                FAIL,
                f"docker inspect failed ({_detail(completed)})",
                BUG_FIX,
            )
        ]
    uncapped, seen = [], []
    for line in completed.stdout.splitlines():
        name, driver, options = (line.split("\t") + ["", "", "{}"])[:3]
        name = name.lstrip("/")
        opts = json.loads(options or "{}") or {}
        capped = driver in _CAPPED_DRIVERS or (
            driver == "json-file" and "max-size" in opts
        )
        seen.append(
            f"{name}: {driver}" + (f" {opts['max-size']}" if "max-size" in opts else "")
        )
        if not capped:
            uncapped.append(name)
    return [
        Row(
            "docker",
            "container log drivers",
            FAIL if uncapped else PASS,
            "; ".join(seen),
            f"uncapped logs on {', '.join(uncapped)}: recreate with a capped driver"
            if uncapped
            else "",
        )
    ]


def check_agent_mail(ctx: Context) -> list[Row]:
    if not ctx.settings.get("agent_mail"):
        return [
            Row(
                "agent-mail",
                "self send and fetch",
                SKIP,
                f"not configured for {ctx.machine}",
            )
        ]
    script = ctx.harness_root / "scripts" / "agent_mail" / "cross_machine_check.py"
    completed = ctx.call(
        [
            sys.executable,
            str(script),
            "--peer",
            ctx.machine,
            "--send",
            "--expect-from",
            ctx.machine,
            "--wait",
            str(ctx.settings.get("agent_mail_wait_seconds", 60)),
            "--mark-read",
        ]
    )
    steps: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "step" in record:
            steps[record["step"]] = record
    sent = steps.get("send") or {}
    message_id = sent.get("message_id") if sent.get("ok") else None
    matched = (steps.get("inbound") or {}).get("matched") or []
    fetched = message_id is not None and any(
        m.get("message_id") == message_id for m in matched
    )
    failed = [
        f"{name}: {step.get('error') or step.get('skipped') or 'not ok'}"
        for name, step in steps.items()
        if step.get("ok") is not True and name not in ("mark-read",)
    ]
    remedy = (
        "`agent-mail-notifier status`, `systemctl --user status "
        f"ai-harness-agent-mail-notifier.service`, `docker logs` on the secure-agent-mail stack; {BUG_FIX}"
    )
    if fetched:
        rows = [
            Row(
                "agent-mail",
                "self send and fetch",
                PASS,
                f"message {message_id} sent as {sent.get('sender')} and fetched back",
            )
        ]
    else:
        rows = [
            Row(
                "agent-mail",
                "self send and fetch",
                FAIL,
                (
                    f"message {message_id} sent but not fetched back"
                    if message_id is not None
                    else "nothing was sent"
                )
                + (f"; {'; '.join(failed)}" if failed else f"; {_detail(completed)}"),
                remedy,
            )
        ]
    marked = steps.get("mark-read") or {}
    if message_id is None:
        rows.append(Row("agent-mail", "probe marked read", SKIP, "nothing was sent"))
    elif marked.get("ok") is True:
        rows.append(
            Row(
                "agent-mail",
                "probe marked read",
                PASS,
                f"message {message_id} marked read",
            )
        )
    else:
        rows.append(
            Row(
                "agent-mail",
                "probe marked read",
                FAIL,
                f"message {message_id} not marked read: {marked.get('error') or 'no receipt'}",
                f"`agent-mail-notifier mark-read {message_id} --as-machine` from outside any session; {BUG_FIX}",
            )
        )
    return rows


def check_buzz(ctx: Context) -> list[Row]:
    if not ctx.settings.get("buzz"):
        return [Row("buzz", "relay upgrade", SKIP, f"not configured for {ctx.machine}")]
    completed = ctx.call(
        [
            "buzz",
            "probe-relay",
            "--json",
            "--wait-s",
            str(ctx.settings.get("buzz_wait_seconds", 30)),
        ]
    )
    try:
        result = json.loads(completed.stdout or "{}")
    except ValueError:
        result = {}
    if completed.returncode == 0 and result.get("ok"):
        return [
            Row(
                "buzz",
                "relay upgrade",
                PASS,
                f"{result.get('status')} {result.get('reason')}",
            )
        ]
    return [
        Row(
            "buzz",
            "relay upgrade",
            FAIL,
            f"{result.get('reason') or _detail(completed)}",
            f"`buzz probe-relay`, then `docker compose ls` for the relay's stack; {BUG_FIX}",
        )
    ]


def check_postgres(ctx: Context) -> list[Row]:
    """The configured Postgres container answers, and each configured client daemon holds an
    established connection to the endpoint it declares (a process merely running is not enough)."""
    table = ctx.settings.get("postgres")
    if not isinstance(table, Mapping):
        return [Row("postgres", "ready", SKIP, f"not configured for {ctx.machine}")]
    container = str(table["container"])
    completed = ctx.call(
        [
            "docker",
            "exec",
            container,
            "pg_isready",
            "-U",
            str(table["user"]),
            "-d",
            str(table["db"]),
        ]
    )
    rows = [
        Row("postgres", f"{container} ready", PASS, completed.stdout.strip())
        if completed.returncode == 0
        else Row(
            "postgres",
            f"{container} ready",
            FAIL,
            f"pg_isready in {container}: {_detail(completed)}",
            f"`docker logs {container}`; {BUG_FIX}",
        )
    ]
    ports = {
        line.rsplit(":", 1)[-1].strip()
        for line in ctx.call(
            ["docker", "port", container, "5432/tcp"]
        ).stdout.splitlines()
        if ":" in line
    }

    def holders(port: str) -> set[int]:
        sockets = ctx.call(
            ["ss", "-Htnp", "state", "established", "dport", "=", f":{port}"]
        ).stdout
        return {int(pid) for pid in _PID.findall(sockets)}

    postgres = (
        f"Postgres :{', :'.join(sorted(ports))}"
        if ports
        else "Postgres (none published)"
    )
    on_postgres = set().union(*(holders(port) for port in sorted(ports)))
    processes = ctx.call(["ps", "-eo", "pid=,args="]).stdout.splitlines()
    for name, spec in sorted((table.get("daemons") or {}).items()):
        # A daemon names the endpoint it holds at rest: the container's published Postgres port
        # unless `port` names another (the scheduling worker holds only NATS between jobs).
        module = str(spec["module"])
        if "port" in spec:
            endpoint = f"{spec.get('endpoint', 'port')} :{spec['port']}"
            connected = holders(str(spec["port"]))
        else:
            endpoint, connected = postgres, on_postgres
        pids = {
            int(line.split(None, 1)[0])
            for line in processes
            if line.strip() and module in line
        }
        check = f"daemon {name} connected"
        holding = sorted(pids & connected)
        if holding:
            rows.append(
                Row(
                    "postgres",
                    check,
                    PASS,
                    f"{len(pids)} process(es) run {module}; pid {', '.join(map(str, holding))} "
                    f"hold(s) a connection to {endpoint}",
                )
            )
            continue
        rows.append(
            Row(
                "postgres",
                check,
                FAIL,
                f"{len(pids)} process(es) run {module}; none holds a connection to {endpoint}",
                f"{table.get('daemon_remedy') or 'the daemon log'}; {BUG_FIX}",
            )
        )
    return rows


def check_timers(ctx: Context) -> list[Row]:
    listed = ctx.call(
        [
            "systemctl",
            "--user",
            "list-unit-files",
            "ai-harness-*.timer",
            "--no-legend",
            "--plain",
        ]
    )
    timers = [
        fields[0]
        for fields in map(str.split, listed.stdout.splitlines())
        if fields[1:2] == ["enabled"]
    ]
    if not timers:
        return [
            Row(
                "timers",
                "ai-harness user timers",
                FAIL,
                f"no enabled ai-harness timer ({_detail(listed)})",
                "re-run install.sh; `systemctl --user list-timers`",
            )
        ]
    rows = []
    for timer in timers:
        values = _properties(
            ctx.call(
                [
                    "systemctl",
                    "--user",
                    "show",
                    timer,
                    "-p",
                    "ActiveState",
                    "-p",
                    "Unit",
                ]
            ).stdout
        )
        service = values.get("Unit") or timer.removesuffix(".timer") + ".service"
        result = _properties(
            ctx.call(
                [
                    "systemctl",
                    "--user",
                    "show",
                    service,
                    "-p",
                    "Result",
                    "-p",
                    "ExecMainStatus",
                ]
            ).stdout
        )
        evidence = (
            f"timer {values.get('ActiveState', 'unknown')}; {service} result "
            f"{result.get('Result', 'unknown')} (status {result.get('ExecMainStatus', '?')})"
        )
        ok = values.get("ActiveState") == "active" and result.get("Result") == "success"
        rows.append(
            Row(
                "timers",
                timer,
                PASS if ok else FAIL,
                evidence,
                ""
                if ok
                else f"`systemctl --user status {timer} {service}`, "
                f"`journalctl --user -b -u {service}`; {BUG_FIX}",
            )
        )
    return rows


def check_machine_contention(ctx: Context) -> list[Row]:
    try:
        completed = ctx.call(["machine-contention", "status", "--json"])
    except FileNotFoundError:
        return [
            Row(
                "machine-contention",
                "sampling",
                SKIP,
                "machine-contention is not installed",
            )
        ]
    report = json.loads(completed.stdout or "{}") if completed.returncode == 0 else {}
    verdict = report.get("verdict")
    if isinstance(verdict, dict):
        age = ctx.now().timestamp() - float(verdict.get("observed_at", 0))
        return [
            Row(
                "machine-contention",
                "sampling",
                PASS,
                f"verdict {verdict.get('verdict')} observed {age:.0f} s ago",
            )
        ]
    return [
        Row(
            "machine-contention",
            "sampling",
            FAIL,
            str(report.get("verdict_error") or _detail(completed)),
            "`systemctl --user status ai-harness-machine-contention.timer`; "
            f"`machine-contention sample`; {BUG_FIX}",
        )
    ]


def check_credo(ctx: Context) -> list[Row]:
    service = ctx.settings.get("credo_service")
    if not service:
        return [Row("credo", "service row", SKIP, f"not configured for {ctx.machine}")]
    env = dict(ctx.env)
    suffix = ctx.settings.get("aws_account_suffix")
    if suffix:
        try:
            env["AWS_PROFILE"] = aws_profile.resolve_name(
                env=env, account_suffix=str(suffix)
            )
        except aws_profile.ProfileResolutionError:
            pass
    try:
        completed = ctx.call(["credo", "status"], env=env)
    except FileNotFoundError:
        return [
            Row(
                "credo",
                f"{service} row",
                FAIL,
                "credo is not on PATH",
                "re-run install.sh",
            )
        ]
    row = next(
        (
            line.split()
            for line in completed.stdout.splitlines()
            if line.split()[:1] == [service]
        ),
        None,
    )
    if row and len(row) > 1 and row[1] == "VALID":
        return [Row("credo", f"{service} row", PASS, " ".join(row[:4]))]
    return [
        Row(
            "credo",
            f"{service} row",
            FAIL,
            " ".join(row[:2]) if row else f"no {service} row ({_detail(completed)})",
            f"`credo login --service {service} </dev/null` (no human needed on an enrolled "
            "host; a vault password prompt is a human step, see "
            "docs/procedures/example-project-ec2-host-facts.md)",
        )
    ]


def check_aws(ctx: Context) -> list[Row]:
    suffix = ctx.settings.get("aws_account_suffix")
    if not suffix:
        return [
            Row("aws", "sso caller identity", SKIP, f"not configured for {ctx.machine}")
        ]
    try:
        profile = aws_profile.resolve_name(
            env=dict(ctx.env), account_suffix=str(suffix)
        )
    except aws_profile.ProfileResolutionError as exc:
        return [
            Row(
                "aws",
                "sso caller identity",
                FAIL,
                str(exc),
                "docs/procedures/aws-sso-profile-naming.md",
            )
        ]
    completed = ctx.call(
        [
            "aws",
            "sts",
            "get-caller-identity",
            "--profile",
            profile,
            "--output",
            "json",
        ]
    )
    if completed.returncode == 0:
        account = json.loads(completed.stdout or "{}").get("Account")
        return [
            Row(
                "aws",
                "sso caller identity",
                PASS,
                f"profile {profile} account {account}",
            )
        ]
    return [
        Row(
            "aws",
            "sso caller identity",
            FAIL,
            f"profile {profile}: {_detail(completed)}",
            f"`sso-reauth --profile {profile} --source manual`, then verify with sts; "
            "a sentinel that did not re-auth is a defect to /bug-fix",
        )
    ]


def _resume_lines(path: Path, since: datetime) -> list[tuple[int, str, str, str]]:
    """``(line number, ts, stage, key)`` of every resume stage logged at or after ``since``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _LOG_LINE.match(line)
        if not match or not _RESUME_STAGE.match(match.group(2)):
            continue
        try:
            moment = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment >= since:
            found.append((number, match.group(1), match.group(2), match.group(3)))
    return found


def check_cc_resume(ctx: Context) -> list[Row]:
    root = Path(ctx.env.get("AIH_RESUME_ROOT") or ctx.home / ".claude" / "resume")
    if not root.is_dir():
        return [Row("cc-resume", "boot sweep", SKIP, f"no resume store at {root}")]
    sweep = host_boot.read_json(root / "boot-stamps" / "last-sweep.json") or {}
    if sweep.get("boot_id") != ctx.boot.boot_id:
        return [
            Row(
                "cc-resume",
                "boot sweep",
                FAIL,
                f"last-sweep.json names boot {sweep.get('boot_id') or '(none)'}, not this one",
                "`systemctl --user status ai-harness-reboot-resume.service`, "
                f"`journalctl --user -b -u ai-harness-reboot-resume.service`; {BUG_FIX}",
            )
        ]
    log = root / "resume.log"
    booted = datetime.fromtimestamp(ctx.boot.boot_time, UTC)
    lines = _resume_lines(log, booted)
    span = (
        f"{log} lines {lines[0][0]}-{lines[-1][0]}"
        if lines
        else f"no stage in {log} yet"
    )
    dispositions = sweep.get("dispositions") or {}
    pending = sorted(k for k, v in dispositions.items() if v == "pending")
    rows = [
        Row(
            "cc-resume",
            "boot sweep",
            PASS,
            f"swept {sweep.get('swept_at')}: {len(pending)} pending of "
            f"{len(dispositions)} stamp(s); {span}",
        )
    ]
    if not pending:
        rows.append(
            Row("cc-resume", "relaunches", SKIP, "no session was pending a resume")
        )
        return rows
    stages: dict[str, set[str]] = {}
    for _, _, stage, key in lines:
        stages.setdefault(key, set()).add(stage)
    undelivered = [
        k
        for k in pending
        if "CC-LAUNCH-RESUME-CLASSIFIED" in stages.get(k, set())
        and not stages[k] & set(_DELIVERED_STAGES)
    ]
    waiting = [
        k for k in pending if "CC-LAUNCH-RESUME-CLASSIFIED" not in stages.get(k, set())
    ]
    delivered = [k for k in pending if k not in undelivered and k not in waiting]
    evidence = (
        f"delivered {', '.join(delivered) or 'none'}; classified but not delivered "
        f"{', '.join(undelivered) or 'none'}; not relaunched yet {', '.join(waiting) or 'none'}; "
        f"{span}"
    )
    if undelivered:
        verdict, remedy = (
            FAIL,
            (
                "grep those keys in resume.log; docs/procedures/example-project-ec2-reboot-resume.md "
                f"step 2; {BUG_FIX} (launch-resume)"
            ),
        )
    elif waiting:
        verdict, remedy = (
            SKIP,
            "re-run `ai-harness boot-check --now` once `ai c -R` has run",
        )
    else:
        verdict, remedy = PASS, ""
    rows.append(Row("cc-resume", "relaunches", verdict, evidence, remedy))
    return rows


Check = Callable[[Context], list[Row]]
#: ``(system, check)``: a check that raises is reported under its system's name.
CHECKS: tuple[tuple[str, Check], ...] = (
    ("storage", check_storage),
    ("storage", check_mount_ordering),
    ("docker", check_docker_engine),
    ("containerd", check_containerd_root),
    ("docker", check_compose_stacks),
    ("docker", check_log_drivers),
    ("agent-mail", check_agent_mail),
    ("buzz", check_buzz),
    ("postgres", check_postgres),
    ("timers", check_timers),
    ("machine-contention", check_machine_contention),
    ("credo", check_credo),
    ("aws", check_aws),
    ("cc-resume", check_cc_resume),
)


def run_checks(ctx: Context, checks: Iterable[tuple[str, Check]] = CHECKS) -> list[Row]:
    """Every check, in order; one that raises becomes a FAIL row and the rest still run."""
    rows: list[Row] = []
    for system, check in checks:
        try:
            rows.extend(check(ctx))
        except Exception as exc:  # noqa: BLE001 - a failing probe is a row, never the end
            rows.append(
                Row(
                    system,
                    f"{check.__name__.removeprefix('check_')} probe",
                    FAIL,
                    f"the check raised {type(exc).__name__}: {exc}",
                    f"{BUG_FIX} (ai_chassis.boot_check.{check.__name__})",
                )
            )
    return rows


def wait_until_ready(ctx: Context) -> Row:
    """Poll each ready unit until all read ``active``, up to ``ready_timeout_seconds``."""
    units = list(ctx.settings.get("ready_units", ()))
    timeout = float(ctx.settings.get("ready_timeout_seconds", 300))
    start = ctx.monotonic()
    problems: dict[str, str] = {}

    def active(unit: str) -> bool:
        try:
            state = ctx.call(["systemctl", "is-active", unit]).stdout.strip()
        except Exception as exc:  # noqa: BLE001 - a probe that raises is "not active yet"
            problems[unit] = type(exc).__name__
            return False
        problems.pop(unit, None)
        return state == "active"

    while True:
        waiting = [unit for unit in units if not active(unit)]
        elapsed = ctx.monotonic() - start
        if not waiting:
            return Row(
                "boot",
                "ready wait",
                PASS,
                f"{', '.join(units)} active after {elapsed:.0f} s",
            )
        if elapsed >= timeout:
            return Row(
                "boot",
                "ready wait",
                FAIL,
                f"still not active after {elapsed:.0f} s: "
                + ", ".join(
                    f"{unit} (probe raised {problems[unit]})"
                    if unit in problems
                    else unit
                    for unit in waiting
                ),
                f"`systemctl status {' '.join(waiting)}`; {BUG_FIX}",
            )
        ctx.sleep(5)


# --- the record --------------------------------------------------------------------------------


def state_dir(env: Mapping[str, str], home: Path) -> Path:
    base = env.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(base) / "ai-harness" / "boot-check"


def summary(
    rows: list[Row], *, boot: host_boot.BootIdentity, at: str, trigger: str
) -> str:
    counts = {v: sum(r.verdict == v for r in rows) for v in (PASS, FAIL, SKIP)}
    failed = [r for r in rows if r.verdict == FAIL]
    lines = [
        f"== boot check {at} ({trigger}) boot {boot.boot_id} booted "
        f"{host_boot.iso(datetime.fromtimestamp(boot.boot_time, UTC))}",
        f"PASS {counts[PASS]}  FAIL {counts[FAIL]}  SKIP {counts[SKIP]}",
    ]
    for row in rows:
        lines.append(f"{row.verdict:<4} {row.system} / {row.check}: {row.evidence}")
        if row.verdict != PASS and row.remedy:
            lines.append(f"     remedy: {row.remedy}")
    lines.append(
        "To /bug-fix: "
        + (
            ", ".join(f"{r.system} / {r.check}" for r in failed)
            if failed
            else "nothing"
        )
    )
    return "\n".join(redact(line) for line in lines) + "\n"


def record(
    rows: list[Row],
    *,
    boot: host_boot.BootIdentity,
    directory: Path,
    at: str,
    trigger: str,
) -> tuple[Path, Path, str]:
    """Append the rows to ``<boot_id>.jsonl`` and the summary to ``<boot_id>.txt``."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    jsonl = directory / f"{boot.boot_id}.jsonl"
    text = directory / f"{boot.boot_id}.txt"
    with jsonl.open("a", encoding="utf-8") as handle:
        for row in rows:
            values = {key: redact(value) for key, value in asdict(row).items()}
            handle.write(
                json.dumps(
                    {"boot_id": boot.boot_id, "at": at, **values}, sort_keys=True
                )
                + "\n"
            )
    rendered = summary(rows, boot=boot, at=at, trigger=trigger)
    with text.open("a", encoding="utf-8") as handle:
        handle.write(rendered + "\n")
    return jsonl, text, rendered


# --- entry points ------------------------------------------------------------------------------


def load_settings(config_path: Path, machine: str) -> dict[str, Any]:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    machine_table = (data.get("machine") or {}).get(machine) or {}
    return {**(data.get("defaults") or {}), **machine_table}


def _declaration(
    harness_root: Path, machine: str
) -> StorageDeclaration | NotDeclared | None:
    from .config import load_config
    from .storage import resolve

    if not machine:
        return None
    return resolve(
        load_config(harness_root / "projects-wide-session-config").data, machine
    )


def main(  # noqa: PLR0913 - the CLI plus its injected seams
    argv: list[str] | None = None,
    *,
    run: Runner = subprocess.run,
    boot: host_boot.BootIdentity | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    declaration: StorageDeclaration | NotDeclared | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-chassis boot-check", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="run every check at once, without the boot wait",
    )
    parser.add_argument("--config", type=Path, default=HARNESS_ROOT / CONFIG)
    args = parser.parse_args(argv)
    environment = dict(os.environ if env is None else env)
    machine = environment.get("AI_HOST", "")
    home = home or Path.home()
    clock = now or (lambda: datetime.now(UTC))
    leading: list[Row] = []
    if boot is None:
        try:
            boot = host_boot.identity()
        except host_boot.BootIdentityUnavailable as exc:
            moment = clock()
            boot = host_boot.BootIdentity(
                f"unknown-boot-{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                int(moment.timestamp()),
                platform.system().lower(),
                "unavailable",
            )
            leading.append(
                Row(
                    "boot",
                    "boot identity",
                    FAIL,
                    f"the boot id is unreadable ({exc}); this record is named {boot.boot_id}",
                    f"check that host_boot can read the kernel boot id; {BUG_FIX}",
                )
            )
    if not machine:
        leading.append(
            Row(
                "boot",
                "identity",
                FAIL,
                "AI_HOST is unset",
                "re-run `ai-harness apply --only boot-check`",
            )
        )
    if declaration is None:
        try:
            declaration = _declaration(HARNESS_ROOT, machine)
        except Exception as exc:  # noqa: BLE001 - an unreadable declaration is a row
            leading.append(Row("storage", "declaration", FAIL, str(exc), BUG_FIX))
    ctx = Context(
        run=run,
        machine=machine,
        harness_root=HARNESS_ROOT,
        settings=load_settings(args.config, machine),
        declaration=declaration,
        boot=boot,
        home=home,
        env=environment,
        sleep=sleep,
        monotonic=monotonic,
        now=clock,
    )
    if args.now:
        leading.append(Row("boot", "ready wait", SKIP, "run by hand (--now): no wait"))
    else:
        leading.append(wait_until_ready(ctx))
        sleep(float(ctx.settings.get("settle_seconds", 120)))
    rows = leading + run_checks(ctx)
    at = host_boot.iso(ctx.now())
    jsonl, _text, rendered = record(
        rows,
        boot=boot,
        directory=state_dir(environment, home),
        at=at,
        trigger="manual" if args.now else "boot",
    )
    sys.stdout.write(rendered)
    print(f"rows: {jsonl}")
    return 1 if any(row.verdict == FAIL for row in rows) else 0


# --- the unit (`ai-harness apply --only boot-check`) --------------------------------------------


def render_unit(harness_root: Path, machine: str) -> str:
    """The template with the checkout, AI_HOST and the interpreter running this install, which
    is the one that imports ai_chassis and the board client's dependencies."""
    template = (harness_root / "systemd" / UNIT).read_text(encoding="utf-8")
    return (
        template.replace("__AI_HARNESS_ROOT__", str(harness_root))
        .replace("__MACHINE__", machine)
        .replace("__PYTHON__", sys.executable)
    )


def _write_unit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{UNIT}.{os.getpid()}.tmp")
    staged.write_text(text, encoding="utf-8")
    os.chmod(staged, 0o644)
    os.replace(staged, path)


def _reload_and_enable(
    user: Callable[..., subprocess.CompletedProcess[str]], *, reload: bool
) -> int:
    """Daemon-reload after a write, then enable the unit unless it already is: 1 on a refusal."""
    if reload:
        reloaded = user("daemon-reload")
        if reloaded.returncode != 0:
            print(
                f"  [FAIL] boot-check unit: daemon-reload failed ({_detail(reloaded)})",
                file=sys.stderr,
            )
            return 1
    if user("is-enabled", UNIT).returncode != 0:
        enabled = user("enable", UNIT)
        if enabled.returncode != 0 or user("is-enabled", UNIT).returncode != 0:
            print(
                f"  [FAIL] boot-check unit: enable failed ({_detail(enabled)})",
                file=sys.stderr,
            )
            return 1
    return 0


def install_unit(  # noqa: PLR0911 - one return per precondition and per failed mutation
    harness_root: Path,
    machine: str,
    *,
    run: Runner | None = None,
    unit_dir: Path | None = None,
    system: str | None = None,
) -> int:
    """Write the unit when its text differs, reload only then, and enable it (not start it).

    Enabling without ``--now`` leaves the first run to the next boot or to a hand
    ``ai-harness boot-check --now``. A missing precondition (no user manager, including no
    ``systemctl`` to ask, or no AI_HOST) is a warning and exit 0, and a config root outside a
    checkout, with no unit template, is a skip, so the full ``ai-harness apply`` over it still
    succeeds (D-17). A write, reload or enable that fails, by its exit code or by an OSError, is
    one ``[FAIL]`` line and exit 1, never a raise, so the apply's trust tail still runs.
    """
    host_os = system or platform.system()
    if host_os != "Linux":
        print(f"  [skip] boot-check unit: no systemd user manager on {host_os}")
        return 0
    template = harness_root / "systemd" / UNIT
    if not template.is_file():
        print(f"  [skip] boot-check unit: no unit template at {template}")
        return 0
    if not machine:
        print(
            "  [warn] boot-check unit: AI_HOST is unset, so the unit was not rendered",
            file=sys.stderr,
        )
        return 0

    runner = run or subprocess.run
    operation = ""

    def user(*args: str) -> subprocess.CompletedProcess[str]:
        nonlocal operation
        argv = ["systemctl", "--user", *args]
        operation = " ".join(argv)
        return runner(argv, capture_output=True, text=True, check=False)

    try:
        manager = user("show-environment")
    except OSError as exc:
        manager = subprocess.CompletedProcess([], 127, "", str(exc))
    if manager.returncode != 0:
        print(
            "  [warn] boot-check unit: the systemd user manager is unavailable "
            f"({_detail(manager)}); nothing installed",
            file=sys.stderr,
        )
        return 0
    directory = (
        unit_dir
        or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd"
        / "user"
    )
    path = directory / UNIT
    try:
        operation = "read"
        desired = render_unit(harness_root, machine)
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        changed = current != desired
        if changed:
            operation = "write"
            _write_unit(path, desired)
        failed = _reload_and_enable(user, reload=changed)
    except OSError as exc:
        # D-17: an operational error is this step's failure, so the apply's trust tail still runs.
        print(
            f"  [FAIL] boot-check unit: {operation} failed for {path} ({exc})",
            file=sys.stderr,
        )
        return 1
    if failed:
        return failed
    state = "written" if changed else "unchanged"
    print(
        f"  [ok]  boot-check unit: {UNIT} {state} and enabled; runs once per boot, "
        "`ai-harness boot-check --now` by hand"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
