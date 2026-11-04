"""Long-running, lease-owning process entrypoint for a shadow watcher."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ai_harness.shadow_watcher import (
    CapabilityProbe,
    ShadowWatcher,
    TaskRowResolver,
    TaskTarget,
    WatcherConfig,
    guarded_task_update,
)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
BIND_WAIT_SECONDS = 120.0
BIND_POLL_SECONDS = 0.05
CLAUDE_CODE_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


class RunnerError(RuntimeError):
    """The watcher cannot safely acquire or retain its own lease."""


class Waitable(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def installed_claude_code_version() -> str:
    """Return a diagnostic CLI version signal, or an untrusted empty value."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if getattr(result, "returncode", 0) != 0:
        return ""
    match = CLAUDE_CODE_VERSION_RE.search(getattr(result, "stdout", ""))
    return match.group(1) if match is not None else ""


def _registry_module():
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import agent_session_registry

    return agent_session_registry


def load_watcher_config(path: Path | None = None) -> WatcherConfig:
    """Load the documented job-supervisor watcher section on every cycle."""
    selected = (
        path
        or Path(
            os.environ.get(
                "AIH_SHADOW_WATCHER_CONFIG", "~/.config/ai-harness/config.toml"
            )
        ).expanduser()
    )
    try:
        with selected.open("rb") as handle:
            document = tomllib.load(handle)
        section = document["job_supervisor"]["shadow_watcher"]
        if not isinstance(section, dict):
            raise ValueError("shadow watcher configuration is not a table")
        refresh = section.get(
            "refresh_seconds", WatcherConfig().refresh_interval_seconds
        )
        grace = section.get(
            "status_read_grace_seconds", WatcherConfig().status_read_grace_seconds
        )
        failures = section.get(
            "max_consecutive_failures", WatcherConfig().max_consecutive_failures
        )
        if (
            isinstance(refresh, bool)
            or isinstance(grace, bool)
            or isinstance(failures, bool)
        ):
            raise ValueError("shadow watcher configuration contains a boolean")
        return WatcherConfig(
            refresh_interval_seconds=float(refresh),
            status_read_grace_seconds=float(grace),
            max_consecutive_failures=int(failures),
        )
    except (KeyError, OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid shadow watcher configuration: {exc}") from exc


def bounded_status_read(
    read_status: Callable[[], Mapping[str, object]], grace_seconds: float
) -> Mapping[str, object]:
    """Bound one ledger read without letting a stuck reader hold watcher exit."""
    result: queue.Queue[Mapping[str, object] | Exception] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put_nowait(read_status())
        except Exception as exc:  # noqa: BLE001 - preserve the reader's typed failure.
            result.put_nowait(exc)

    threading.Thread(
        target=read, name="shadow-watcher-status-read", daemon=True
    ).start()
    try:
        outcome = result.get(timeout=grace_seconds)
    except queue.Empty as exc:
        raise OSError(
            f"supervisor status read exceeded {grace_seconds:g}s grace"
        ) from exc
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def write_terminal_projection(
    store: object,
    binding: object,
    lease: object,
    record: Mapping[str, object],
) -> None:
    """Complete one still-live task row; periodic projection remains Phase 2."""
    del record  # Phase 1 projects only the terminal CC status, not periodic detail.
    resolver = TaskRowResolver(Path.home() / ".claude" / "tasks")
    target = TaskTarget(lease.task_namespace, lease.cc_task_id)
    path = resolver.resolve(target.namespace, target.cc_task_id)
    capability = CapabilityProbe(client_version=installed_claude_code_version()).check(
        path
    )

    def lease_is_live() -> bool:
        return store.renew_watcher_lease(
            binding.identity,
            lease.lease_id,
            lease.generation,
            now=datetime.now(UTC),
        )

    guarded_task_update(
        resolver,
        target,
        probe=capability,
        lease_is_live=lease_is_live,
        mutate=lambda task: {
            **{name: value for name, value in task.items() if name != "activeForm"},
            "status": "completed",
        },
    )


def wait_for_bound_watcher_lease(  # noqa: PLR0913 - injectable bounded wait.
    store: object,
    binding: object,
    claim_id: str,
    watcher_identity: object,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = BIND_WAIT_SECONDS,
    poll_seconds: float = BIND_POLL_SECONDS,
) -> object:
    """Wait for the post-Bash atomic bind before promoting the watcher lease."""
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("binding wait requires positive timing bounds")
    deadline = monotonic() + timeout_seconds
    while True:
        lease = store.activate_watcher_lease(
            binding.identity,
            claim_id,
            watcher_identity,
            now=now(),
        )
        if lease is not None:
            return lease
        current = store.binding_for_watcher_claim(claim_id, binding.job_id)
        if current != binding:
            raise RunnerError("watcher claim was cleaned up or became inconsistent")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RunnerError("timed out waiting for the live-turn binding")
        sleep(min(poll_seconds, remaining))


def activate_watcher(job_id: str, claim_id: str) -> ShadowWatcher:
    """Promote one exact bound claim to this process's fenced watcher lease."""
    registry = _registry_module()
    store = registry.SQLiteFallback()
    store.initialize()
    binding = store.binding_for_watcher_claim(claim_id, job_id)
    if binding is None:
        raise RunnerError(
            "watcher claim is missing, expired, or belongs to another job"
        )
    probe = registry.SystemProcessProbe()
    start = probe.process_start(os.getpid())
    if start is None:
        raise RunnerError("watcher process identity is unavailable")
    watcher_identity = registry.ProcessIdentity(
        probe.host_id, probe.boot_id, os.getpid(), start
    )
    lease = wait_for_bound_watcher_lease(
        store,
        binding,
        claim_id,
        watcher_identity,
    )

    def read_status() -> dict[str, object]:
        for raw in store.all_current_payloads():
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise RunnerError("registry returned a non-object process record")
            identity_key = (
                payload.get("hostId"),
                payload.get("bootId"),
                payload.get("pid"),
                payload.get("procStart"),
            )
            if identity_key != binding.identity.key:
                continue
            if payload.get("recordId") != job_id:
                raise RunnerError("registry returned a mismatched process record")
            return {**payload, "job_id": job_id, "state": "running"}
        return {"job_id": job_id, "state": "unknown"}

    return ShadowWatcher(
        read_status=read_status,
        renew_lease=lambda: store.renew_watcher_lease(
            binding.identity,
            lease.lease_id,
            lease.generation,
            now=datetime.now(UTC),
        ),
        release_lease=lambda: store.release_watcher_lease(
            binding.identity, lease.lease_id, lease.generation
        ),
        project=lambda record, terminal: (
            write_terminal_projection(store, binding, lease, record)
            if terminal
            else None
        ),
        load_config=load_watcher_config,
        read_status_with_grace=bounded_status_read,
    )


def drive_watcher(
    watcher: ShadowWatcher,
    stop: Waitable,
) -> int:
    """Drive cycles with an interruptible configured wait and stable exit codes."""
    while not stop.is_set():
        cycle = watcher.cycle()
        if cycle.stopped:
            return (
                EXIT_SUCCESS
                if cycle.reason in {"terminal", "stopped"}
                else EXIT_FAILURE
            )
        if stop.wait(cycle.interval_seconds):
            break
    try:
        watcher.stop()
    except (OSError, RuntimeError, ValueError):
        return EXIT_FAILURE
    return EXIT_SUCCESS


def install_shutdown_handlers(stop: threading.Event) -> None:
    """Request an interruptible graceful release; never signal the detached job."""

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow-watcher")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--claim-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        watcher = activate_watcher(args.job_id, args.claim_id)
        stop = threading.Event()
        install_shutdown_handlers(stop)
        return drive_watcher(watcher, stop)
    except (OSError, RunnerError, RuntimeError, ValueError) as exc:
        print(f"shadow watcher: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
