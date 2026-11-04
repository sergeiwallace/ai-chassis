"""Temporary stage 1 producers, scheduled only by the host dispatcher.

Workers own no scheduler. A child exits after one call; its process sentinel is
the readiness handle. Separate children allow a stuck transport to be killed
without leaving a thread, holding the marker lock, or delaying another rung.
"""

import asyncio
import contextlib
import datetime
import json
import multiprocessing
import os
from pathlib import Path
import tempfile

from ai_chassis.git_inbox_watch import Ledger
from ai_chassis.wake.events import Event
from ai_chassis.wake.route import Receipt
from ai_chassis.wake.targets import MachineTarget, SessionTarget


BRIDGED_RUNGS = ("board-fetch", "cross-pair-git-inbox-poll", "harness-sync-census")
"""Board fetch retires when a board source is registered. Git-inbox poll and
census retire when cross-pair event sources under the open GitHub-events
decision exist. Remove a rung by deleting its entry and its handler.
"""


BRIDGE_STATUS_NAME = "bridge-status.json"
"""Per-rung counters under the state root, read by ``agent-mail-notifier status``."""


def require_fork():
    """Refuse loudly where the bridge cannot run, never skip its rungs silently.

    OS-agnostic exception (ISSUE-REF), ruled by the example-host chief 2026-10-31: the
    temporary stage 1 bridge is POSIX-only because each rung runs in a forked
    child that keeps the injected clients. It retires with the rungs.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("bridge-requires-fork")


class BridgeStatus:
    """Per-rung failure and overrun-kill counters, persisted for the status command.

    A rung that keeps overrunning its bound is killed each time; the count and
    the last kill time make that visible instead of silent.
    """

    def __init__(self, path, names):
        self.path = Path(path)
        self.rungs = {
            name: {
                "failures": 0,
                "kills": 0,
                "last_kill_at": None,
                "last_failure": None,
            }
            for name in names
        }
        self.persist()

    def failed(self, name, word):
        rung = self.rungs[name]
        rung["failures"] += 1
        rung["last_failure"] = word
        if word == "timeout":
            rung["kills"] += 1
            rung["last_kill_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="seconds")
        self.persist()

    def persist(self):
        # A status write must never hold up or break the dispatcher.
        from ai_chassis.agent_mail_notifier import write_if_changed

        with contextlib.suppress(OSError):
            write_if_changed(self.path, json.dumps(self.rungs, sort_keys=True))


def _execute(handler, path):
    # A fork must not inherit the daemon's SIGTERM handler or live asyncio loop.
    import signal

    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        result = {"events": [event.as_record() for event in handler()]}
    except Exception as exc:
        result = {"failure": type(exc).__name__}
    # A fresh result is owned by this one call; never overwrite another call.
    with contextlib.suppress(OSError):
        with Path(path).open("x", encoding="utf-8") as stream:
            json.dump(result, stream)


class RungTask:
    """One non-overlapping call, with its own dispatcher-owned execution bound.

    The bound is the rung's configured cadence. An overrun ends only that child;
    the next attempt uses the same cadence. No worker reads the clock or retries.
    This temporary example-host bridge uses POSIX fork to retain injected clients.
    """

    def __init__(  # noqa: PLR0913 - one rung's injected child and parent capabilities
        self, name, handler, cadence, notifier, dispatcher, *, on_complete=None
    ):
        self.name, self.handler, self.cadence = name, handler, cadence
        self.status = None
        self.notifier, self.dispatcher = notifier, dispatcher
        self.on_complete = on_complete
        self.at = dispatcher.clock()
        self.child = None
        self.bound = None
        self.scratch = Path(tempfile.mkdtemp(prefix="notifier-bridge-"))
        self.result = self.scratch / "result.json"

    def next_due(self):
        return self.bound if self.child is not None else self.at

    def readable_handles(self):
        return (self.child.sentinel,) if self.child is not None else ()

    def _failed(self, word):
        if self.status is not None:
            self.status.failed(self.name, word)
        self.notifier._phase_failed(self.name)
        with contextlib.suppress(Exception):
            self.notifier.reporter.log(f"{self.name}:failed:{word}")

    def _reap(self):
        child, self.child = self.child, None
        if child is not None:
            if child.is_alive():
                # The worker creates its own session before any transport child.
                # Bound that exact owned process group, never a pattern.
                import signal

                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Stop raced with the child's setsid(), or macOS refused the
                    # group signal (EPERM) while the child is still alive.
                    child.kill()
            child.join(timeout=1)
            if not child.is_alive():
                child.close()
        if self.result.exists():
            self.result.read_bytes()  # Inspect the actual owned target before deletion.
            self.result.unlink()

    def step(self, ready, now):
        if self.child is not None:
            if self.child.sentinel in ready:
                try:
                    result = json.loads(self.result.read_text(encoding="utf-8"))
                    if "failure" in result:
                        self._failed(result["failure"])
                    else:
                        events = [Event.from_record(row) for row in result["events"]]
                        # Dispatcher state belongs to this thread, never the
                        # fork's inherited copy. Completion shares rung isolation.
                        if self.on_complete is not None:
                            events.extend(self.on_complete())
                        self.dispatcher.accept(events)
                except Exception as exc:
                    self._failed(type(exc).__name__)
                finally:
                    self._reap()
            elif now >= self.bound:
                self._failed("timeout")
                self._reap()
            return
        if self.at is None or now < self.at:
            return
        try:
            period = self.cadence()
            self.at = now + period
            self.bound = now + period
            self.child = multiprocessing.get_context("fork").Process(
                target=_execute, args=(self.handler, self.result), daemon=True
            )
            self.child.start()
        except Exception as exc:
            self.child = None
            # Configuration failure has no safe cadence: retain the task but
            # wait for restart/configuration repair, never spin on a due time.
            self.at = None
            self._failed(type(exc).__name__)

    def close(self):
        self._reap()
        # Only our mkdtemp directory and its inspected result were ever created.
        if not list(self.scratch.iterdir()):
            self.scratch.rmdir()


class InboxDelivery:
    """Share the existing delivered ledger across polls and doorbell deliveries.

    The dispatcher already owns durable retry obligations. Partial records keep
    future recipients eligible while recording this recipient's written output.
    Never wait for another process's ledger lock on the dispatcher thread.
    """

    def __init__(self, path):
        self.ledger = Ledger(path)

    def __call__(self, channel, session, event):
        if event.source != "git-inbox":
            return channel.deliver(session, [event])
        try:
            with self.ledger.locked(blocking=False):
                view = self.ledger.read()
                if event.key in view.done_ids or session.slug in view.reached.get(
                    event.key, ()
                ):
                    return Receipt(True)
                receipt = channel.deliver(session, [event])
                if receipt.accepted:
                    self.ledger.append(
                        [
                            {
                                "id": event.key,
                                "targets": [session.slug],
                                "outcome": "written",
                                "partial": True,
                            }
                        ]
                    )
                return receipt
        except (OSError, ValueError):
            return Receipt(False, "git-inbox-ledger-unavailable")


class TickBridge:
    """Adapt exactly the named legacy producers, never the legacy tick."""

    def __init__(self, notifier, dispatcher):
        self.notifier, self.dispatcher = notifier, dispatcher

    def tasks(self):
        require_fork()
        status = BridgeStatus(
            self.notifier.state.root / BRIDGE_STATUS_NAME, BRIDGED_RUNGS
        )
        tasks = tuple(
            RungTask(
                name,
                handler,
                cadence,
                self.notifier,
                self.dispatcher,
                on_complete=self._harness_sync_observe
                if name == "harness-sync-census"
                else None,
            )
            for name in BRIDGED_RUNGS
            for handler, cadence in [getattr(self, name.replace("-", "_"))()]
        )
        for task in tasks:
            task.status = status
        return tasks

    def board_fetch(self):
        from ai_chassis.agent_mail_notifier import poll_interval

        def fetch():
            notifier = self.notifier
            if not notifier.agent_mail_enabled or notifier.settings is None:
                return []

            async def read():
                # Identity enrolment is required to address the fetch. Do not run
                # directory sweeps, retirement, presence, liveness or watchdogs.
                client = notifier._client()
                live = {s.slug for s in self.dispatcher.directory()}
                identities = await notifier._ensure_identities(client, live)
                found = notifier._polled_identities(identities, live)
                found = [i for i in found if i.slug is None or i.slug in live]
                await notifier._fetch_concurrently(client, found)
                return [
                    Event(
                        "agent-mail",
                        str(message.message_id),
                        "unread",
                        SessionTarget(identity.slug)
                        if identity.slug
                        else MachineTarget(notifier.host),
                        requires_action=True,
                        scope="addressed" if identity.slug else "machine",
                    )
                    for identity in found
                    for message in identity.messages
                ]

            notifier._loop = None
            notifier._board_session = None
            return asyncio.run(read())

        return fetch, lambda: poll_interval(self.notifier.cfg)

    def cross_pair_git_inbox_poll(self):
        from ai_chassis import git_inbox_watch as inbox

        def fetch():
            watcher = self.notifier.git_inbox_watch
            if watcher is None:
                return []
            watcher.ledger_path = self.notifier.state.git_inbox_ledger
            # The bridge reports failures by class at the parent boundary.
            watcher.log = lambda _message: None
            live = {s.slug for s in self.dispatcher.directory()}
            # Poll only: no _git_inbox_poll restart recovery or heartbeat.
            batch = watcher.poll(live=live, registered=live)
            if watcher.last_fetch_ok is False:
                raise OSError("inbox-fetch-unverified")
            files = {row["id"]: row for row in batch.records}
            return [
                Event(
                    "git-inbox",
                    batch.ids[message.message_id],
                    files[batch.ids[message.message_id]]["blob"],
                    SessionTarget(slug),
                    requires_action=message.message_class == "direct"
                    or bool(message.requires_action),
                    sender=message.sender,
                    scope="machine"
                    if message.message_class == "broadcast"
                    else "addressed",
                )
                for slug, messages in batch.per_slug.items()
                for message in messages
            ] + [
                Event(
                    "git-inbox",
                    batch.ids[message.message_id],
                    files[batch.ids[message.message_id]]["blob"],
                    MachineTarget(self.notifier.host),
                    scope="machine",
                )
                for message in batch.machine
            ]

        def cadence():
            watcher = self.notifier.git_inbox_watch
            return inbox.poll_interval_s(watcher.config_loader() if watcher else {})

        return fetch, cadence

    def harness_sync_census(self):
        from ai_chassis.agent_mail_notifier import REPO_ROOT, _load_module

        def census():
            if self.notifier.harness_sync is not None:
                if self.notifier.harness_sync():
                    raise OSError("census-unverified")
            return []

        def cadence():
            return (
                _load_module(
                    "harness_sync_census", REPO_ROOT / "scripts/harness_sync_census.py"
                )
                .load_config()
                .refresh_interval_s
            )

        return census, cadence

    def _harness_sync_observe(self):
        """Reduce persisted refresh facts on the dispatcher's own thread.

        The live stamp can advance after the child's census write. Read it and
        the wake facts again, without the doorbell reader's network refresh.
        """
        from ai_chassis.agent_mail_notifier import REPO_ROOT, _load_module
        from ai_chassis.wake.handlers.harness_sync import HarnessCensus

        if self.notifier.harness_sync_line is None:
            return []

        def facts():
            census = _load_module(
                "harness_sync_census", REPO_ROOT / "scripts/harness_sync_census.py"
            )
            return {
                **(census.read_census() or {}),
                "installed_commit": census.read_installed_commit(),
                "wake": self.notifier.harness_sync_line(),
            }

        return HarnessCensus(facts, self.dispatcher).catch_up()
