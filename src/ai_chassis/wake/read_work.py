"""Read-only work completes off the writer pass, then signals one targeted retry.

Workers never deliver, acknowledge, or mutate dispatcher state. The original
incoming doorbell/obligation remains the restart authority. No readiness probes
or periodic retry is needed while a read is in flight.
"""

from collections import OrderedDict
import queue
import threading

from .dispatcher import WakeSignal

MAX_READS = 128
READ_WORKERS = 4


class ReadPending(ValueError):
    def __init__(self):
        super().__init__("evidence-read-pending")


class ReadWork:
    def __init__(self):
        self.signal = WakeSignal()
        self.jobs = queue.Queue()
        self.completed = queue.SimpleQueue()
        self.entries = OrderedDict()
        self.waiting = {}
        self.groups = {}
        self.threads = []
        self.closed = False
        self.lock = threading.Lock()

    def _start(self):
        if not self.threads:
            for _ in range(READ_WORKERS):
                thread = threading.Thread(target=self._work, daemon=True)
                self.threads.append(thread)
                thread.start()

    def _work(self):
        for job in iter(self.jobs.get, None):
            if self.closed:
                return
            token, read, group = job
            try:
                result = (True, read())
            except Exception as exc:  # A failed read is retried by its durable owner.
                result = (False, exc)
            with self.lock:
                if not self.closed:
                    self.completed.put((token, result, group))
                    self.signal.notify()

    def call(self, key, revision, read, ready_key, ready, *, group=None):  # noqa: PLR0913 - immutable read identity and targeted completion capabilities
        if self.closed:
            raise ValueError("evidence-reader-closed")
        token = (key, revision)
        self._collect()
        entry = self.entries.get(token)
        if entry is None:
            # Evict only completed reads. An in-flight read is never duplicated.
            for old, row in list(self.entries.items()):
                if len(self.entries) < MAX_READS:
                    break
                if row["result"] is not None and not row["ready"]:
                    del self.entries[old]
            if len(self.entries) >= MAX_READS:
                # Retry this specific waiter when existing capacity completes.
                self.waiting[ready_key] = ready
                raise ReadPending()
            entry = {"result": None, "ready": {}}
            self.entries[token] = entry
            self._queue_read(token, read, group)
        result = entry["result"]
        if result is None:
            entry["ready"][ready_key] = ready
            raise ReadPending()
        if not result[0]:
            del self.entries[token]
            raise result[1]
        return result[1]

    def _queue_read(self, token, read, group):
        self._start()
        if group is not None and group in self.groups:
            self.groups[group].append((token, read, group))
        else:
            if group is not None:
                self.groups[group] = []
            self.jobs.put((token, read, group))

    def _collect(self):
        while not self.completed.empty():
            token, result, group = self.completed.get_nowait()
            if token in self.entries:
                self.entries[token]["result"] = result
            if group is not None:
                pending = self.groups[group]
                if pending:
                    self.jobs.put(pending.pop(0))
                else:
                    del self.groups[group]

    def readable_handles(self):
        return (self.signal.reader,)

    def forget(self, key, revision):
        entry = self.entries.get((key, revision))
        if entry is not None and entry["result"] is not None:
            del self.entries[(key, revision)]

    def step(self, ready):
        if self.signal.reader not in ready:
            return
        self.signal.consume()
        self._collect()
        waiting, self.waiting = self.waiting, {}
        for row in list(self.entries.values()):
            if row["result"] is not None:
                callbacks, row["ready"] = row["ready"], {}
                for callback in callbacks.values():
                    callback()
        for callback in waiting.values():
            callback()

    def close(self):
        # A stuck filesystem read cannot delay daemon shutdown. Daemon workers
        # hold no writer lock and cannot write dispatcher state after close.
        with self.lock:
            self.closed = True
            self.signal.close()
        for _ in self.threads:
            self.jobs.put(None)
