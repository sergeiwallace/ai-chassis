#!/usr/bin/env python3
# ruff: noqa: E402
"""The fixed launchd-owned runner for one macOS broker workload."""

from __future__ import annotations

import os as _bootstrap_os
import signal as _bootstrap_signal
import sys as _bootstrap_sys

if __name__ == "__main__":
    # Hold TERM and INT from the first line until run() has installed its handlers. A
    # Terminate that lands during startup otherwise kills the runner before it can write
    # a receipt, leaving the job `terminating` forever. The mask survives the interpreter
    # bootstrap's re-exec, so it also covers the venv interpreter's startup.
    _bootstrap_signal.pthread_sigmask(
        _bootstrap_signal.SIG_BLOCK,
        {_bootstrap_signal.SIGTERM, _bootstrap_signal.SIGINT},
    )

_bootstrap_directory = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
while not _bootstrap_os.path.isfile(
    _bootstrap_os.path.join(_bootstrap_directory, "pyproject.toml")
):
    _bootstrap_parent = _bootstrap_os.path.dirname(_bootstrap_directory)
    if _bootstrap_parent == _bootstrap_directory:
        break
    _bootstrap_directory = _bootstrap_parent
_bootstrap_sys.path.insert(0, _bootstrap_os.path.join(_bootstrap_directory, "scripts"))
try:
    from py_interpreter_bootstrap import ensure_supported_python  # noqa: E402
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):  # noqa: E402
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
import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

TERM_GRACE_SECONDS = 5
# The broker reads this file's ABSENCE, once launchd has dropped the label, as proof the
# workload never started (ISSUE-REF). Exclusive creation also makes it the label's single
# start claim, so a label bootstrapped twice cannot run its workload twice (ISSUE-REF).
RUNNER_STARTED_MARKER = "runner-started.json"


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def run(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or _digest(spec.get("request")) != spec.get(
        "request_sha256"
    ):
        raise ValueError("invalid immutable broker job specification")
    if not isinstance(spec.get("boot_id"), str) or not spec["boot_id"]:
        raise ValueError("invalid broker boot identity")
    request = spec["request"]
    command = spec.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        raise ValueError("invalid allowlisted broker command")
    started = datetime.now(UTC).isoformat()
    child: subprocess.Popen[bytes] | None = None
    termination_reason = "natural_exit"

    def terminate(_signum: int, _frame: object) -> None:
        nonlocal termination_reason
        termination_reason = "service_terminated"
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()

    held = {signal.SIGTERM, signal.SIGINT}
    previous = {signum: signal.signal(signum, terminate) for signum in held}
    # A TERM held since startup is delivered here, before there is a child to forward it to;
    # the check after Popen forwards it once there is.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, held)
    try:
        child = subprocess.Popen(command, cwd=request["cwd"], env=spec["environment"])
        if termination_reason == "service_terminated":
            terminate(signal.SIGTERM, None)
        status = child.wait()
        receipt = {
            "schema_version": 1,
            "owner_token_sha256": hashlib.sha256(
                spec["owner_token"].encode()
            ).hexdigest(),
            "job_label": spec["job_label"],
            "kind": request["kind"],
            "owner_generation": spec["owner_generation"],
            "boot_id": spec["boot_id"],
            "fence_sha256": _digest(request["fence"]),
            "job_spec_sha256": _digest(spec),
            "child_wait_status": status,
            "termination_reason": termination_reason,
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        _atomic_json(spec_path.parent / "runner-terminal.json", receipt)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 - status if status < 0 else status


def kill_own_process_group() -> None:
    """SIGKILL the runner's own process group, itself included (design D-17).

    launchd's job-group cleanup sends only SIGTERM on macOS 27, so a descendant that
    ignores TERM outlives the job. The group cannot be recycled while its leader lives,
    and the runner is that leader, so this addresses no guessed PID or PGID. A runner
    that is not its group's leader does nothing: the group would then be someone else's.
    """
    leader = os.getpid()
    if os.getpgrp() != leader:
        return
    os.killpg(leader, signal.SIGKILL)


def claim_start(spec_path: Path) -> bool:
    """Durably record that this label's runner started; False if one already had."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(spec_path.parent / RUNNER_STARTED_MARKER, flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"schema_version": 1, "started_at": datetime.now(UTC).isoformat()}
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
    # The broker reads the marker's ABSENCE as proof the workload never started (D-18 (a)), so the
    # new directory entry must survive a crash too, not only the file's bytes.
    directory = os.open(spec_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    spec_path = parser.parse_args().spec
    if not claim_start(spec_path):
        sys.stderr.write("runner refused: this job label's runner already started\n")
        return 1
    status = run(spec_path)
    kill_own_process_group()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
