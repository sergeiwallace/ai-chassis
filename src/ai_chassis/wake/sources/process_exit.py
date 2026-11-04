"""Process exit subscriptions; unsupported kernels degrade without a probe."""

import os
from pathlib import Path
import select
import sys
import threading

from ..dispatcher import WakeSignal, wait_readable
from ..events import Event, digest
from ..source import EventSource
from ..targets import SessionTarget
from .intake import WatchUnavailable


def process_stamp(pid):
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.Error as exc:
        raise WatchUnavailable("process-identity-unavailable") from exc


class PidfdWatch:
    def __init__(self, pid):
        try:
            self.fd = os.pidfd_open(pid, 0)
        except (AttributeError, OSError) as exc:
            raise WatchUnavailable("pidfd-unavailable") from exc

    def readable_handles(self):
        return (self.fd,)

    def close(self):
        os.close(self.fd)


class KqueueProcessWatch:
    def __init__(self, pid):
        self.queue = select.kqueue()
        try:
            self.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
            )
        except BaseException:
            self.queue.close()
            raise

    def readable_handles(self):
        return (self.queue.fileno(),)

    def close(self):
        self.queue.close()


class WindowsProcessWatch:
    def __init__(self, pid):
        import win32api
        import win32event

        self.events = win32event
        self.process = win32api.OpenProcess(0x00100000, False, pid)
        self.stop_handle = win32event.CreateEvent(None, True, False, None)
        self.signal = WakeSignal()
        self.thread = threading.Thread(target=self._observe, daemon=True)
        self.thread.start()

    def _observe(self):
        result = self.events.WaitForMultipleObjects(
            (self.stop_handle, self.process), False, self.events.INFINITE
        )
        if result != self.events.WAIT_OBJECT_0:
            self.signal.notify()

    def readable_handles(self):
        return (self.signal.reader,)

    def close(self):
        self.events.SetEvent(self.stop_handle)
        self.thread.join()
        self.signal.close()
        self.process.Close()
        self.stop_handle.Close()


def native_process_watch(pid):
    try:
        if sys.platform.startswith("linux"):
            return PidfdWatch(pid)
        if sys.platform == "darwin" and hasattr(select, "kqueue"):
            return KqueueProcessWatch(pid)
        if sys.platform == "win32":
            return WindowsProcessWatch(pid)
    except (OSError, ImportError) as exc:
        raise WatchUnavailable("process-watch-unavailable") from exc
    raise WatchUnavailable("process-watch-unavailable")


class ProcessExitSource(EventSource):
    def __init__(
        self,
        state_dir: Path,
        watch_factory=native_process_watch,
        identity=process_stamp,
    ):
        self.root = Path(state_dir)
        self.watch_factory = watch_factory
        self.watches = {}
        self.status = {}
        self.identity = identity
        self.exited = {}
        self.signal = None
        self.emitted = set()

    def open(self):
        from .. import sessions
        from ..targets import SessionRef

        self.signal = WakeSignal()
        for row in sessions.bindings(self.root.parent):
            if row.get("pid") and row.get("start_stamp") is not None:
                self.register(
                    SessionRef(row["slug"], row["session_id"]),
                    row["pid"],
                    row["start_stamp"],
                )

    def register(self, session, pid, start_stamp):  # noqa: PLR0911 - identity checks bracket the OS subscription
        key = (session.slug, session.session_id, str(start_stamp))
        if key in self.watches:
            return True
        if key in self.exited:
            return False
        try:
            if self.identity(pid) != start_stamp:
                self._exit(key, session)
                return False
            watch = self.watch_factory(pid)
            if self.identity(pid) != start_stamp:
                watch.close()
                self._exit(key, session)
                return False
        except (OSError, AttributeError) as exc:
            if self.identity(pid) != start_stamp:
                self._exit(key, session)
                return False
            self.status[session.slug] = "degraded:" + getattr(
                exc, "reason", "process-watch-unavailable"
            )
            return False
        self.watches[key] = (session, watch)
        self.status[session.slug] = "ok"
        return True

    def _exit(self, key, session):
        self.exited[key] = Event(
            "process-exit",
            digest(key),
            key[2],
            SessionTarget(session.slug, session.session_id),
        )
        if self.signal is not None:
            self.signal.notify()

    def readable_handles(self):
        return tuple(
            handle
            for _, watch in self.watches.values()
            for handle in watch.readable_handles()
        ) + ((self.signal.reader,) if self.signal else ())

    def drain(self):
        ready = set(wait_readable(self.readable_handles(), 0))
        events = []
        if self.signal and self.signal.reader in ready:
            self.signal.consume()
            for key, event in self.exited.items():
                if key not in self.emitted:
                    events.append(event)
                    self.emitted.add(key)
        for key, (session, watch) in list(self.watches.items()):
            if ready.intersection(watch.readable_handles()):
                self._exit(key, session)
                events.append(self.exited[key])
                self.emitted.add(key)
                watch.close()
                del self.watches[key]
        return events

    def catch_up(self, _cursor):
        return list(self.exited.values())

    def recovery_blocks(self, _event):
        return False

    def close(self):
        for _, watch in self.watches.values():
            watch.close()
        self.watches.clear()
        if self.signal:
            self.signal.close()
            self.signal = None
