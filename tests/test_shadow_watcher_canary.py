"""Phase-0 T-0.3 isolated idle-reap canary coverage."""

from __future__ import annotations

import pytest

from ai_harness.shadow_watcher_canary import CanaryError, CanaryRequest, run_canary


def _request() -> CanaryRequest:
    return CanaryRequest(
        namespace="shadow-canary-proof",
        watcher_pid=11,
        watcher_proc_start="11.000000",
        job_pid=12,
        job_proc_start="12.000000",
        idle_seconds=1,
    )


def test_canary_requires_the_callers_explicit_isolated_namespace() -> None:
    with pytest.raises(CanaryError, match="isolated namespace"):
        run_canary(
            CanaryRequest(
                namespace="production-session",
                watcher_pid=11,
                watcher_proc_start="11.000000",
                job_pid=12,
                job_proc_start="12.000000",
                idle_seconds=1,
            ),
            environment={"CLAUDE_CODE_TASK_LIST_ID": "production-session"},
            process_start=lambda _pid: "unused",
            sleep=lambda _seconds: None,
        )

    with pytest.raises(CanaryError, match="does not match"):
        run_canary(
            _request(),
            environment={"CLAUDE_CODE_TASK_LIST_ID": "other-session"},
            process_start=lambda _pid: "unused",
            sleep=lambda _seconds: None,
        )


def test_canary_passes_only_when_watcher_and_detached_job_survive_idle_interval() -> (
    None
):
    sleeps: list[int] = []

    result = run_canary(
        CanaryRequest(
            namespace="shadow-canary-proof",
            watcher_pid=11,
            watcher_proc_start="11.000000",
            job_pid=12,
            job_proc_start="12.000000",
            idle_seconds=31,
        ),
        environment={"CLAUDE_CODE_TASK_LIST_ID": "shadow-canary-proof"},
        process_start=lambda pid: f"{pid}.000000",
        sleep=sleeps.append,
    )

    assert sleeps == [31]
    assert result.watcher_survived is True
    assert result.job_survived is True
    assert result.namespace == "shadow-canary-proof"


@pytest.mark.parametrize(
    ("watcher_start", "job_start", "message"),
    [
        (None, "12.000000", "watcher was reaped"),
        ("reused", "12.000000", "watcher was reaped"),
        ("11.000000", None, "detached job did not survive"),
    ],
)
def test_canary_fails_without_signalling_either_process(
    watcher_start: str | None, job_start: str | None, message: str
) -> None:
    observed = {11: watcher_start, 12: job_start}

    with pytest.raises(CanaryError, match=message):
        run_canary(
            _request(),
            environment={"CLAUDE_CODE_TASK_LIST_ID": "shadow-canary-proof"},
            process_start=observed.get,
            sleep=lambda _seconds: None,
        )
