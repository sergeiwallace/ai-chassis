"""Durable local queue backed by OS notifications, with no sampling fallback.

Linux uses close-write/moved-to inotify, macOS vnode kqueue, Windows overlapped
ReadDirectoryChangesW. Producers close complete newline-terminated records, or
rename a completed staging file. No content or arbitrary paths enter an Event.
"""

import ctypes
import errno
import math
import os
from pathlib import Path
import re
import select
import socket
import stat
import sys
import threading
import uuid

from ..events import Event, LIFECYCLE_INPUT_KINDS, digest, sync_directory
from ..source import EventSource
from ..targets import SessionTarget, safe_identity

LINE = re.compile(
    rb"1 ([A-Za-z0-9_-]+) ([A-Za-z0-9_-]+) ([A-Za-z0-9_-]+) ([0-9]+)(?: ([A-Za-z0-9_=,:-]+))?\n\Z"
)
MAX_RECORD_BYTES = 512
CLAIM_PREFIX = ".claimed-"


class WatchUnavailable(OSError):
    def __init__(self, reason, *, errno_number=None, operation=None):
        self.reason = reason
        self.errno_name = errno.errorcode.get(errno_number)
        self.operation = operation
        self.detail = {
            "reason": reason + (":" + self.errno_name if self.errno_name else ""),
            "errno": self.errno_name,
            "operation": operation,
        }
        if errno_number is None:
            super().__init__(reason)
        else:
            super().__init__(errno_number, reason)


class InotifyWatch:
    def __init__(self, path, *, entry_changes=False):
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise WatchUnavailable(
                "inotify-unavailable",
                errno_number=ctypes.get_errno(),
                operation="inotify_init1",
            )
        # Intake needs completion only. Config watches also follow creation/removal
        # of a previously absent overlay directory, without changing intake semantics.
        mask = 0x8 | 0x80 | 0x400 | 0x800
        if entry_changes:
            mask |= 0x100 | 0x200 | 0x40
        # Close-write and moved-to avoid exposing an ordinary partial hook write.
        if libc.inotify_add_watch(self.fd, os.fsencode(path), mask) < 0:
            error = ctypes.get_errno()
            self.close()
            raise WatchUnavailable(
                "inotify-watch-unavailable",
                errno_number=error,
                operation="inotify_add_watch",
            )

    def readable_handles(self):
        return (self.fd,)

    def drain(self):
        # An invalidated watch cannot be replaced with a state-reading retry.
        import struct

        names = set()
        overflow = False
        # A buffer boundary is not a completed collection boundary: a later
        # ready chunk may cancel work from an earlier one. Read only queued
        # notifications through EAGAIN before releasing dependent effects.
        while True:
            try:
                raw = os.read(self.fd, 65536)
            except BlockingIOError:
                return None if overflow else names
            offset = 0
            while offset < len(raw):
                _, mask, _, size = struct.unpack_from("iIII", raw, offset)
                if mask & (0x400 | 0x800 | 0x8000):
                    raise WatchUnavailable("watch-invalidated")
                overflow |= bool(mask & 0x4000)
                name = raw[offset + 16 : offset + 16 + size].split(b"\0", 1)[0]
                if name:
                    decoded = os.fsdecode(name)
                    # Our rename is ownership transfer, not producer completion.
                    # An open claimed prefix finishes only on its close-write event.
                    if not (mask & 0x80 and decoded.startswith(CLAIM_PREFIX)):
                        names.add(decoded)
                offset += 16 + size

    def close(self):
        os.close(self.fd)


class KqueueWatch:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY)
        self.files = {}
        try:
            self.queue = select.kqueue()
            event = select.kevent(
                self.fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_DELETE,
            )
            self.queue.control([event], 0)
        except BaseException:
            os.close(self.fd)
            raise

    def readable_handles(self):
        return (self.queue.fileno(),)

    def drain(self):
        for event in self.queue.control(None, 64, 0):
            if event.ident == self.fd and event.fflags & (
                select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE
            ):
                raise WatchUnavailable("watch-invalidated")
            if event.fflags & (select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE):
                for path, fd in list(self.files.items()):
                    if event.ident == fd:
                        self.forget(path)

    def track(self, path):
        if path not in self.files:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                event = select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE,
                )
                self.queue.control([event], 0)
            except BaseException:
                os.close(fd)
                raise
            self.files[path] = fd

    def forget(self, path):
        fd = self.files.pop(path, None)
        if fd is not None:
            os.close(fd)

    def close(self):
        self.queue.close()
        for fd in self.files.values():
            os.close(fd)
        self.files.clear()
        os.close(self.fd)


class WindowsWatch:
    """A native overlapped wait bridges Windows HANDLEs to the socket wait set."""

    def __init__(self, path, *, entry_changes=False):
        import win32con
        import win32event
        import win32file
        import pywintypes

        self.api, self.events = win32file, win32event
        self.filter = 0x1 | 0x10 | (0x2 if entry_changes else 0)
        self.handle = win32file.CreateFile(
            str(path),
            win32con.FILE_LIST_DIRECTORY,
            win32con.FILE_SHARE_READ
            | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED,
            None,
        )
        self.stop_handle = win32event.CreateEvent(None, True, False, None)
        self.overlap = pywintypes.OVERLAPPED()
        self.overlap.hEvent = win32event.CreateEvent(None, True, False, None)
        self.buffer = win32file.AllocateReadBuffer(65536)
        self.reader, self.writer = socket.socketpair()
        self.writer.setblocking(False)
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._observe, daemon=True)
        self.thread.start()
        self.ready.wait()
        if self.error:
            self.close()
            raise WatchUnavailable("directory-watch-failed")

    def _signal(self):
        try:
            self.writer.send(b"1")
        except BlockingIOError:
            pass  # A queued notification already makes the socket readable.

    def _observe(self):
        try:
            while True:
                self.events.ResetEvent(self.overlap.hEvent)
                self.api.ReadDirectoryChangesW(
                    self.handle, self.buffer, False, self.filter, self.overlap, None
                )
                self.ready.set()
                result = self.events.WaitForMultipleObjects(
                    (self.stop_handle, self.overlap.hEvent), False, self.events.INFINITE
                )
                if result == self.events.WAIT_OBJECT_0:
                    self.api.CancelIoEx(self.handle, self.overlap)
                    return
                self.api.GetOverlappedResult(self.handle, self.overlap, True)
                self._signal()
        except Exception as exc:
            self.error = type(exc).__name__
            self.ready.set()
            self._signal()

    def readable_handles(self):
        return (self.reader,)

    def drain(self):
        self.reader.recv(65536)
        if self.error:
            raise WatchUnavailable("directory-watch-failed")

    def close(self):
        self.events.SetEvent(self.stop_handle)
        self.thread.join()
        self.handle.Close()
        self.stop_handle.Close()
        self.overlap.hEvent.Close()
        self.reader.close()
        self.writer.close()


def native_watch(path, *, platform=None, entry_changes=False):
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return InotifyWatch(path, entry_changes=entry_changes)
    if platform == "darwin" and hasattr(select, "kqueue"):
        return KqueueWatch(path)
    if platform == "win32":
        return WindowsWatch(path, entry_changes=entry_changes)
    raise WatchUnavailable("unsupported-platform")


def parse_record(raw):
    match = LINE.fullmatch(raw)
    if not match or len(raw) > MAX_RECORD_BYTES:
        raise ValueError("invalid-intake")
    kind, session_id, slug, epoch, args = match.groups()
    safe_identity(slug.decode())
    if not math.isfinite(float(epoch)):
        raise ValueError("invalid-epoch")
    # args are reserved content-free lifecycle metadata for later handlers.
    metadata = (args or b"").decode()
    turn = re.search(r"(?:^|,)turn=([0-9]{1,18})(?:,|$)", metadata)
    sequence = re.search(r"(?:^|,)seq=([0-9]{1,18})(?:,|$)", metadata)
    return Event(
        kind.decode(),
        digest([session_id.decode(), kind.decode(), epoch.decode(), metadata]),
        epoch.decode(),
        SessionTarget(slug.decode(), session_id.decode()),
        requires_action=kind == b"poke",
        observed_at=float(epoch),
        turn_generation=int(turn[1]) if turn else 0,
        lifecycle_sequence=int(sequence[1]) if sequence else 0,
    )


def incomplete_record(raw):
    """Only a potentially valid prefix may await another native write event."""
    patterns = (
        rb"1",
        rb"[A-Za-z0-9_-]+",
        rb"[A-Za-z0-9_-]+",
        rb"[A-Za-z0-9][A-Za-z0-9_-]*",
        rb"[0-9]+",
        rb"[A-Za-z0-9_=,:-]+",
    )
    parts = raw.split(b" ")
    return (
        len(raw) <= MAX_RECORD_BYTES
        and len(parts) <= len(patterns)
        and all(
            re.fullmatch(patterns[index], part)
            or (index == len(parts) - 1 and not part)
            for index, part in enumerate(parts)
        )
    )


class IntakeSource(EventSource):
    def __init__(self, intake_dir: Path, watch_factory=native_watch):
        self.root = Path(intake_dir)
        self.watch_factory = watch_factory
        self.watch = None
        self.pending = {}
        self.incomplete = set()
        self.status = "ok"

    @property
    def recovery_complete(self):
        return not self.incomplete

    def recovery_blocks(self, event):
        # Intake carries prompt/compact cancellations. Missing catch-up authority
        # holds dependent lifecycle work, while independent sources still progress.
        return event.source in LIFECYCLE_INPUT_KINDS

    def open(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.watch = self.watch_factory(self.root)

    def readable_handles(self):
        return self.watch.readable_handles()

    def drain(self):
        names = self.watch.drain()
        if names is None:
            return self.catch_up(None)
        return self._collect([self.root / name for name in sorted(names)])

    def _reject(self, path):
        rejected = self.root / "rejected"
        rejected.mkdir(exist_ok=True, mode=0o700)
        name = (
            path.name.split(".", 2)[2]
            if path.name.startswith(CLAIM_PREFIX)
            else path.name
        )
        destination = rejected / name
        if destination.exists() or destination.is_symlink():
            destination = rejected / f"{path.name}-{digest(str(path.lstat()))}"
        if destination.exists():
            raise OSError("rejection-collision")
        path.rename(destination)
        self._forget(path)
        sync_directory(rejected)
        sync_directory(self.root)
        self.status = "degraded:invalid-intake"

    def _forget(self, path):
        forget = getattr(self.watch, "forget", None)
        if forget:
            forget(path)

    def catch_up(self, _cursor):
        return self._collect(sorted(self.root.iterdir()))

    def _claim(self, path):
        if path.name.startswith(CLAIM_PREFIX):
            return path
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            return None
        # The producer owns the public name and may replace it at any time.
        # Move this entry into our namespace before reading or acknowledging.
        claimed = self.root / f"{CLAIM_PREFIX}{uuid.uuid4().hex}.{path.name}"
        if claimed.exists() or claimed.is_symlink():
            raise OSError("claim-collision")
        try:
            path.rename(claimed)
        except PermissionError:
            # Windows can notify creation while the hook still holds a handle
            # without delete sharing. The next native write event retries it.
            self.status = "degraded:claim-unavailable"
            self.incomplete.add(path)
            return None
        self.incomplete.discard(path)
        self._forget(path)
        sync_directory(self.root)
        return claimed

    def _collect(self, paths):
        events = []
        for path in paths:
            if (
                path.name.startswith(".") and not path.name.startswith(CLAIM_PREFIX)
            ) or path.name == "rejected":
                continue
            try:
                path = self._claim(path)
            except FileNotFoundError:
                self.incomplete.discard(path)
                continue
            if path is None:
                continue
            self.incomplete.discard(path)
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                if stat.S_ISLNK(mode):
                    self._reject(path)
                continue
            track = getattr(self.watch, "track", None)
            if track:
                track(path)
            with path.open("rb") as handle:
                raw = handle.read(MAX_RECORD_BYTES + 1)
            try:
                event = parse_record(raw)
            except ValueError:
                # vnode and Windows can signal creation before the hook has closed
                # its redirection. The subsequent native write event finishes it.
                if incomplete_record(raw):
                    self.status = "degraded:incomplete-intake"
                    self.incomplete.add(path)
                    continue
                self._reject(path)
                continue
            self.pending.setdefault(event.identity, {})[path] = raw
            events.append(event)
        return events

    def commit(self, events):
        for event in events:
            for path, raw in self.pending.pop(event.identity, {}).items():
                # Only consumer-owned names are acknowledged. A producer's successor
                # remains at the public name for the next native notification.
                if not path.is_symlink() and path.exists() and path.read_bytes() == raw:
                    path.unlink()
                    self._forget(path)
                    sync_directory(self.root)

    def close(self):
        if self.watch is not None:
            self.watch.close()
            self.watch = None
