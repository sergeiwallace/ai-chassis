"""Tunnel supervision from child exits and first-use connection observations.

The only timeout here is a restart backoff armed by a particular failure. Port
checks belong to a real caller's first use, never to that restart deadline.
"""

import json
import os
from pathlib import Path
import uuid

from .dispatcher import wait_readable
from .events import write_record
from .sources.intake import native_watch
from .sources.process_exit import native_process_watch, process_stamp


def event_dir(state_path):
    return Path(state_path).with_name(Path(state_path).name + ".events")


def used(state_path, forward, healthy):
    write_record(
        event_dir(state_path) / (uuid.uuid4().hex + ".json"),
        {"forward": forward, "healthy": healthy},
    )


def owner_alive(document):
    pid, stamp = document.get("supervisor_pid"), document.get("supervisor_start")
    if type(pid) is not int or pid <= 0 or stamp is None:
        return False
    try:
        return process_stamp(pid) == stamp
    except OSError:
        return False


class TunnelEvents:
    def __init__(
        self,
        tunnel,
        *,
        wait=wait_readable,
        watch=native_watch,
        process_watch=native_process_watch,
    ):
        self.tunnel, self.wait = tunnel, wait
        self.watch_factory, self.process_watch = watch, process_watch
        self.processes = {}
        self.handled = set()
        self.watch = None
        self.root = event_dir(tunnel._state_path)

    def _entries(self):
        return getattr(self.tunnel, "_supervised", ())

    def _subscribe(self, entry):
        if entry.child is not None:
            self.processes[entry.forward.name] = self.process_watch(entry.child.pid)

    def open(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.watch = self.watch_factory(self.root)
        self.tunnel._extra_state = {
            **getattr(self.tunnel, "_extra_state", {}),
            "event_driven": True,
            "supervisor_pid": os.getpid(),
            "supervisor_start": process_stamp(os.getpid()),
        }
        for entry in self._entries():
            self.tunnel._start(entry)
            if entry.child is None:
                entry.ready_at = self.tunnel._effects.clock() + entry.backoff_seconds
            self._subscribe(entry)
        self.tunnel._publish()
        self._requests({path.name for path in self.root.glob("*.json")})

    def _requests(self, names):
        for name in names:
            if name in self.handled or not name.endswith(".json"):
                continue
            receipt = self.root / ".handled" / name
            if receipt.exists():
                self.handled.add(name)
                continue
            row = json.loads((self.root / name).read_text())
            if type(row.get("healthy")) is not bool:
                raise ValueError("invalid-tunnel-use")
            if not self._entries():
                # An externally managed loopback/Tailscale transport has no child
                # to replace. This call is an actual use observation.
                self.tunnel._transition("up" if row["healthy"] else "down")
            else:
                for entry in self._entries():
                    if entry.forward.name != row["forward"]:
                        continue
                    if row["healthy"]:
                        entry.transition("up", self.tunnel._effects.timestamp())
                        entry.backoff_seconds = 1.0
                    else:
                        watch = self.processes.pop(entry.forward.name, None)
                        if watch:
                            watch.close()
                        self.tunnel._supervise(entry)
                        self._subscribe(entry)
            self.handled.add(name)
            write_record(receipt, {"handled": True})
        self.tunnel._publish()

    def step(self):
        now = self.tunnel._effects.clock()
        retries = [entry.ready_at for entry in self._entries() if entry.child is None]
        timeout = max(0, min(retries) - now) if retries else None
        handles = (
            *self.watch.readable_handles(),
            *(h for w in self.processes.values() for h in w.readable_handles()),
        )
        ready = set(self.wait(handles, timeout))
        if ready.intersection(self.watch.readable_handles()):
            names = self.watch.drain()
            self._requests(
                names
                if names is not None
                else {path.name for path in self.root.glob("*.json")}
            )
        for entry in self._entries():
            watch = self.processes.get(entry.forward.name)
            if watch and ready.intersection(watch.readable_handles()):
                watch.close()
                del self.processes[entry.forward.name]
                self.tunnel._supervise(entry)
                self._subscribe(entry)
            elif entry.child is None and self.tunnel._effects.clock() >= entry.ready_at:
                self.tunnel._start(entry)
                if entry.child is None:
                    entry.ready_at = (
                        self.tunnel._effects.clock() + entry.backoff_seconds
                    )
                    entry.backoff_seconds = min(entry.backoff_seconds * 2, 60)
                self._subscribe(entry)
        self.tunnel._publish()

    def run(self):
        try:
            self.open()
            while True:
                self.step()
        finally:
            self.close()

    def close(self):
        for watch in self.processes.values():
            watch.close()
        self.processes.clear()
        if self.watch:
            self.watch.close()
            self.watch = None
