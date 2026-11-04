"""Isolated proof that a CC-scoped shadow watcher survives session idleness."""

from __future__ import annotations

import argparse
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import psutil

ISOLATED_NAMESPACE_RE = re.compile(r"^shadow-canary-[a-z0-9-]{1,80}$")


class CanaryError(RuntimeError):
    """The canary could not establish safe idle-reap evidence."""


@dataclass(frozen=True)
class CanaryResult:
    """The two identities proven live after the requested idle interval."""

    namespace: str
    watcher_survived: bool
    job_survived: bool


@dataclass(frozen=True)
class CanaryRequest:
    """Caller-supplied isolated namespace and process identities to observe."""

    namespace: str
    watcher_pid: int
    watcher_proc_start: str
    job_pid: int
    job_proc_start: str
    idle_seconds: int


def _process_start(pid: int) -> str | None:
    try:
        return f"{psutil.Process(pid).create_time():.6f}"
    except (psutil.Error, OSError, ValueError):
        return None


def _validate_namespace(namespace: str, environment: Mapping[str, str]) -> None:
    if ISOLATED_NAMESPACE_RE.fullmatch(namespace) is None:
        raise CanaryError("canary requires an explicitly isolated namespace")
    if environment.get("CLAUDE_CODE_TASK_LIST_ID") != namespace:
        raise CanaryError("isolated namespace does not match CLAUDE_CODE_TASK_LIST_ID")


def run_canary(
    request: CanaryRequest,
    *,
    environment: Mapping[str, str],
    process_start: Callable[[int], str | None] = _process_start,
    sleep: Callable[[float], None] = time.sleep,
) -> CanaryResult:
    """Observe, but never signal, a CC watcher and detached job across idleness."""
    _validate_namespace(request.namespace, environment)
    if min(request.watcher_pid, request.job_pid, request.idle_seconds) < 1:
        raise CanaryError("PIDs and idle interval must be positive")
    if not request.watcher_proc_start or not request.job_proc_start:
        raise CanaryError("watcher and job require PID-reuse-safe start tokens")
    sleep(request.idle_seconds)
    watcher_live = process_start(request.watcher_pid) == request.watcher_proc_start
    job_live = process_start(request.job_pid) == request.job_proc_start
    if not watcher_live:
        raise CanaryError("shadow watcher was reaped during the idle interval")
    if not job_live:
        raise CanaryError("detached job did not survive the idle interval")
    return CanaryResult(request.namespace, watcher_survived=True, job_survived=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow-watcher-idle-canary")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--watcher-pid", type=int, required=True)
    parser.add_argument("--watcher-proc-start", required=True)
    parser.add_argument("--job-pid", type=int, required=True)
    parser.add_argument("--job-proc-start", required=True)
    parser.add_argument("--idle-seconds", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_canary(
            CanaryRequest(
                namespace=arguments.namespace,
                watcher_pid=arguments.watcher_pid,
                watcher_proc_start=arguments.watcher_proc_start,
                job_pid=arguments.job_pid,
                job_proc_start=arguments.job_proc_start,
                idle_seconds=arguments.idle_seconds,
            ),
            environment=os.environ,
        )
    except CanaryError as exc:
        print(f"shadow watcher idle canary: {exc}", file=os.sys.stderr)
        return 1
    print(
        f"shadow watcher idle canary: PASS namespace={result.namespace} "
        "watcher=live detached-job=live"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
