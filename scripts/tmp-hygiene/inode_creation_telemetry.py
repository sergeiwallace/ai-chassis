# ruff: noqa: E402
"""Write bounded gross inode event telemetry for the platform temporary root.

The timer starts a fresh observer each interval.  Its checkpoint makes a missed
or rebooted interval explicit rather than turning that unknown activity into a
zero-rate sample.
"""

from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

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
import ctypes
import json
import os
import select
import stat
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

STATE_DIR = Path("/var/tmp/ai-harness-tmp-hygiene/inode-creation-telemetry")
CHECKPOINT_NAME = "checkpoint.json"
ACTIVE_NAME = "active.jsonl"
MAX_SEGMENTS = 8
MAX_SEGMENT_BYTES = 1024 * 1024
MAX_CHECKPOINT_BYTES = 4096
_DOWNTIME_MULTIPLIER = 1.5

# A ceiling on the sampler's OWN inotify watches, because `fs.inotify.max_user_watches`
# is a per-user budget shared with every other process the user is running (ISSUE-REF).
# Without it this sampler is an ABSORBING consumer: `_watch_tree` adds one watch per
# directory under the temporary root until the kernel answers ENOSPC, so it ends every
# interval holding whatever headroom existed when it started, and the timer re-runs it
# every 600 s against a 600 s observation window.
#
# Measured on example-project-ec2 2026-10-02 with the editor's file watcher holding 492,527 of
# the host's 524,288: this sampler took 31,389 more and total usage landed on exactly
# 524,288, headroom ZERO. Node's FSWatcher in the presentation render could then not get
# a single watch and the deck render failed. Raising the sysctl is NOT the remedy, since
# an absorbing consumer takes the increase too.
#
# 4096 is deliberately far below any plausible ceiling rather than a share of it: a
# default sized as a fraction of `max_user_watches` would still have absorbed all 31,389
# remaining watches on that host and changed nothing. Truncation is reported, never
# silent -- it travels on `observer_lost`, which already means "this interval is an
# unknown rather than a zero".
DEFAULT_MAX_WATCHES = 4096

# Linux inotify masks.  The observer deliberately stores counters only: event
# paths are never retained in the artifact.
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_Q_OVERFLOW = 0x00004000
_IN_UNMOUNT = 0x00002000
_IN_IGNORED = 0x00008000
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_ISDIR = 0x40000000
_WATCH_MASK = (
    _IN_CREATE
    | _IN_DELETE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_Q_OVERFLOW
    | _IN_UNMOUNT
    | _IN_IGNORED
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
)
_EVENT_HEADER = struct.Struct("iIII")


@dataclass(frozen=True)
class Observation:
    creations: int | None
    deletions: int | None
    observer_lost: bool = False
    filesystem_reset: bool = False
    filesystem_identities: tuple[tuple[str, int], ...] | None = None


class Observer(Protocol):
    def observe(self, interval_seconds: float) -> Observation: ...


def _boot_id() -> str | None:
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return None


def _ensure_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)


def _filesystem_identities(
    roots: tuple[Path, ...],
) -> tuple[tuple[str, int], ...] | None:
    try:
        return tuple((str(root), root.stat().st_dev) for root in roots)
    except OSError:
        return None


def _read_checkpoint(state_dir: Path) -> dict[str, object] | None:
    try:
        raw = (state_dir / CHECKPOINT_NAME).read_bytes()
        if len(raw) > MAX_CHECKPOINT_BYTES:
            return None
        checkpoint = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("completed_at"), (int, float)
    ):
        return None
    return checkpoint


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_checkpoint(
    state_dir: Path,
    completed_at: float,
    filesystem_identities: tuple[tuple[str, int], ...] | None = None,
) -> None:
    checkpoint: dict[str, object] = {
        "completed_at": completed_at,
        "boot_id": _boot_id(),
    }
    if filesystem_identities is not None:
        checkpoint["filesystem_identities"] = filesystem_identities
    payload = json.dumps(checkpoint, separators=(",", ":")).encode() + b"\n"
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError("checkpoint exceeds cap")
    _atomic_write(state_dir / CHECKPOINT_NAME, payload)


def _closed_segments(state_dir: Path) -> list[Path]:
    return sorted(state_dir.glob("segment-*.jsonl"), key=lambda path: path.name)


def append_record(state_dir: Path, record: dict[str, object]) -> Path:
    """Append one JSONL record, atomically rotating before the cap is crossed."""
    _ensure_state_dir(state_dir)
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    if len(encoded) > MAX_SEGMENT_BYTES:
        raise ValueError("telemetry record exceeds one segment")
    active = state_dir / ACTIVE_NAME
    if active.exists() and active.stat().st_size + len(encoded) > MAX_SEGMENT_BYTES:
        # Rename closes the old segment atomically.  Make room first so adding
        # a new active file can never leave more than eight telemetry inodes.
        closed = _closed_segments(state_dir)
        while len(closed) >= MAX_SEGMENTS - 1:
            closed.pop(0).unlink()
        stamp = f"{time.time_ns():020d}"
        os.replace(active, state_dir / f"segment-{stamp}.jsonl")
    with active.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return active


class InotifyUnavailableError(OSError):
    """No inotify on this host, so no interval can be observed at all.

    An ``OSError`` subclass deliberately: ``main`` already turns ``OSError`` into
    one sentence plus exit 1, and the bare ``AttributeError`` that ``ctypes``
    raises for a missing symbol walked straight through that handler, so the
    sampler died with a dlsym traceback instead of naming the platform.
    """


def inotify_available() -> bool:
    """True when this host's libc exposes ``inotify_init1`` -- i.e. on Linux.

    Tests that need a REAL observer gate on this rather than on a platform
    string, and ``test_the_inotify_probe_agrees_with_the_platform`` pins the two
    together so a probe that started answering False on Linux could not quietly
    skip them there.
    """
    try:
        return hasattr(ctypes.CDLL(None, use_errno=True), "inotify_init1")
    except (OSError, TypeError):  # no dlopen(NULL) handle to search
        return False


class InotifyObserver:
    """Recursive unprivileged inotify observer for creation/deletion events."""

    def __init__(
        self, roots: tuple[Path, ...], max_watches: int = DEFAULT_MAX_WATCHES
    ) -> None:
        self._roots = roots
        self._max_watches = max_watches
        self._libc = ctypes.CDLL(None, use_errno=True)
        try:
            inotify_init1 = self._libc.inotify_init1
        except AttributeError as error:
            raise InotifyUnavailableError(
                f"this host's libc has no inotify ({sys.platform}); the "
                "inode-creation telemetry sampler is Linux-only"
            ) from error
        self._fd = inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._watched: set[Path] = set()
        self._watch_descriptors: dict[int, Path] = {}
        self._observer_lost = False
        self._filesystem_identities = _filesystem_identities(roots)
        if self._filesystem_identities is None:
            self._observer_lost = True
        for root in roots:
            if not self._watch_tree(root):
                self._observer_lost = True

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def _watch_tree(self, root: Path) -> bool:
        try:
            if not stat.S_ISDIR(root.stat().st_mode):
                return False
        except OSError:
            return False
        try:
            directories = (root, *root.rglob("*"))
        except OSError:
            return False
        complete = True
        for directory in directories:
            if directory in self._watched:
                continue
            if len(self._watched) >= self._max_watches:
                # Leave the rest of the host's budget alone and say the coverage is
                # partial, rather than counting events from a fraction of the tree as
                # if they were the whole of it.
                return False
            try:
                if not stat.S_ISDIR(directory.stat().st_mode):
                    continue
            except OSError:
                complete = False
                continue
            result = self._libc.inotify_add_watch(
                self._fd, os.fsencode(directory), _WATCH_MASK
            )
            if result >= 0:
                self._watched.add(directory)
                self._watch_descriptors[result] = directory
            else:
                complete = False
        return complete

    @staticmethod
    def _has_entries(directory: Path) -> bool | None:
        try:
            return next(directory.iterdir(), None) is not None
        except OSError:
            return None

    def observe(self, interval_seconds: float) -> Observation:
        creations = deletions = 0
        lost = self._observer_lost
        reset = False
        deadline = time.monotonic() + interval_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([self._fd], [], [], remaining)
                if not readable:
                    continue
                data = os.read(self._fd, 65536)
                offset = 0
                while offset < len(data):
                    watch_descriptor, mask, _, length = _EVENT_HEADER.unpack_from(
                        data, offset
                    )
                    offset += _EVENT_HEADER.size
                    name = data[offset : offset + length].split(b"\0", 1)[0]
                    offset += length
                    if mask & _IN_Q_OVERFLOW:
                        lost = True
                    if mask & (
                        _IN_UNMOUNT | _IN_IGNORED | _IN_DELETE_SELF | _IN_MOVE_SELF
                    ):
                        reset = True
                    if mask & (_IN_CREATE | _IN_MOVED_TO):
                        creations += 1
                    if mask & (_IN_DELETE | _IN_MOVED_FROM):
                        deletions += 1
                    if mask & _IN_ISDIR and mask & (_IN_CREATE | _IN_MOVED_TO) and name:
                        parent = self._watch_descriptors.get(watch_descriptor)
                        if parent is None:
                            lost = True
                            continue
                        directory = parent / os.fsdecode(name)
                        if not self._watch_tree(directory):
                            lost = True
                        elif mask & _IN_CREATE and self._has_entries(directory):
                            # Entries that exist once the new directory is watched
                            # were created before that watch could observe them.
                            lost = True
        finally:
            identities = _filesystem_identities(self._roots)
            if identities is None or identities != self._filesystem_identities:
                reset = True
            self.close()
        return Observation(
            creations,
            deletions,
            observer_lost=lost,
            filesystem_reset=reset,
            filesystem_identities=identities,
        )


def sample(
    state_dir: Path, observer: Observer, interval_seconds: float
) -> dict[str, object]:
    """Observe one interval and persist a record plus a compact checkpoint."""
    _ensure_state_dir(state_dir)
    checkpoint = _read_checkpoint(state_dir)
    started_at = time.time()
    observation = observer.observe(interval_seconds)
    completed_at = time.time()
    reset = observation.filesystem_reset or checkpoint is None
    downtime = bool(
        checkpoint
        and started_at - float(checkpoint["completed_at"])
        > interval_seconds * _DOWNTIME_MULTIPLIER
    )
    if checkpoint and checkpoint.get("boot_id") != _boot_id():
        reset = True
    checkpoint_identities = (
        checkpoint.get("filesystem_identities") if checkpoint else None
    )
    if (
        checkpoint_identities is not None
        and observation.filesystem_identities is not None
        and checkpoint_identities
        != [list(item) for item in observation.filesystem_identities]
    ):
        reset = True
    invalid = reset or downtime or observation.observer_lost
    record: dict[str, object] = {
        "timestamp": completed_at,
        "interval_seconds": round(completed_at - started_at, 6),
        "creations": None if invalid else observation.creations,
        "deletions": None if invalid else observation.deletions,
        "observer_lost": observation.observer_lost,
        "filesystem_reset": reset,
        "sampler_downtime": downtime,
        "valid_for_percentiles": not invalid,
    }
    append_record(state_dir, record)
    _write_checkpoint(state_dir, completed_at, observation.filesystem_identities)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--interval-seconds", type=float, default=600)
    parser.add_argument("--temp-root", type=Path, action="append")
    parser.add_argument(
        "--max-watches",
        type=int,
        default=DEFAULT_MAX_WATCHES,
        help=(
            "ceiling on this sampler's own inotify watches, so it cannot absorb the "
            "user-wide fs.inotify.max_user_watches budget other processes need; "
            "exceeding the tree marks the interval observer_lost rather than reporting "
            "a partial count as whole"
        ),
    )
    args = parser.parse_args(argv)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.max_watches <= 0:
        parser.error("--max-watches must be positive")
    roots = tuple(args.temp_root or [Path(tempfile.gettempdir())])
    try:
        observer = InotifyObserver(roots, max_watches=args.max_watches)
        sample(args.state_dir, observer, args.interval_seconds)
    except (OSError, ValueError) as error:
        print(f"inode telemetry failed: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
