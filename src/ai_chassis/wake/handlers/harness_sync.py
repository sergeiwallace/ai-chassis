"""A main/install hint feeds the notifier's persisted installation episode."""

import json
from dataclasses import replace
import time
from types import SimpleNamespace

from ..events import Event
from ..route import DoorbellHandler
from ..targets import RoleTarget, SessionTarget


class _EpisodeAuthority:
    """Adapt event-maintained eligibility to the existing episode reducer.

    Reuse the notifier's authority without invoking its scheduler, process probes
    or source passes. Both selectors own the same state file during cutover.
    """

    def __init__(self, dispatcher, facts):
        self.dispatcher, self.facts = dispatcher, facts
        self.state = SimpleNamespace(root=dispatcher.root.parent)

    def _harness_sync_wake(self):
        return self.facts

    def _sync_owner(self, episode, _installer, *, probe):
        if not probe:
            return False, bool(episode.get("owner"))
        owners = sorted(
            session.slug
            for session in self.dispatcher.directory()
            if "installer_owner" in session.roles
        )
        candidate = owners[0] if owners else None
        assigned = candidate is not None and candidate != episode.get("owner")
        if assigned:
            episode["owner"] = candidate
            episode["owner_term"] += 1
        return assigned, candidate is not None

    def observe(self):
        from ai_chassis.agent_mail_notifier import Notifier

        return Notifier._with_harness_sync(self, "")


class HarnessCensus(DoorbellHandler):
    def __init__(self, census=None, dispatcher=None):
        self.census, self.dispatcher = census or self._census, dispatcher
        self.status = "ok"

    def handle(self, doorbell):
        facts = self.census()
        revision = facts.get("origin_main")
        if not revision:
            raise OSError("census-unverified")
        if not (facts["wake"]["current"] or facts["wake"]["needed"]):
            raise OSError("installation-state-unverified")
        self._settle_legacy(facts)
        authority = _EpisodeAuthority(self.dispatcher, facts["wake"])
        _, tokens, status = authority.observe()
        # The reducer never changes its episode/deadline merely because main moved.
        episode = self._episode()
        self._sync_deadlines(episode)
        self._settle_episodes(episode, status)
        return [
            Event(
                "harness-sync",
                "install",
                token,
                SessionTarget(owner),
                requires_action=True,
                origin_machine=doorbell.origin_machine,
            )
            for owner, issued in tokens.items()
            for token in issued
        ]

    def _episode(self):
        path = self.dispatcher.root.parent / "harness-sync-owner.json"
        return json.loads(path.read_text()).get("episode") if path.exists() else None

    def _settle_legacy(self, facts):
        installed = facts.get("installed_commit")
        if installed:
            # A matching install settles its work even if main has moved again.
            for key, row in list(self.dispatcher.obligations.items()):
                event = Event.from_record(row["event"])
                if (
                    event.source == "harness-sync"
                    and not event.revision.startswith("sync:")
                    and event.disposition_hint != "sync-reminder"
                    and self._installed(event, installed, facts)
                ):
                    self.dispatcher.cancel(key)

    def _sync_deadlines(self, episode):
        from ai_chassis.agent_mail_notifier import SYNC_REMINDER_AFTER_S

        if episode and episode["reminder_term"] is None:
            reminder = Event(
                "harness-sync",
                episode["episode"],
                "reminder",
                RoleTarget("installer_owner"),
                deadline=episode["opened_at"] + SYNC_REMINDER_AFTER_S,
                disposition_hint="sync-reminder",
            )
            self.dispatcher.deadlines.arm(
                reminder.identity, reminder.deadline, reminder
            )

    def _settle_episodes(self, episode, status):
        for key, row in list(self.dispatcher.obligations.items()):
            event = Event.from_record(row["event"])
            if (
                event.source == "harness-sync"
                and event.revision.startswith("sync:")
                and (episode is None or event.target.value != status.get("owner"))
            ):
                self.dispatcher.cancel(key)
        if episode is None:
            for key, row in list(self.dispatcher.deadlines.entries.items()):
                event = Event.from_record(row["event"])
                if (
                    event.source == "harness-sync"
                    and event.disposition_hint == "sync-reminder"
                ):
                    self.dispatcher.cancel(key)

    def catch_up(self):
        return self.handle(SimpleNamespace(origin_machine="local"))

    def expire(self, event):
        if event.source != "harness-sync" or event.disposition_hint != "sync-reminder":
            return False
        episode = self._episode()
        if not episode or episode["episode"] != event.key:
            return True
        # Re-read the census/stamp before any reminder side effect. Unknown state
        # arms a distinct recovery deadline; the dispatcher retires this expiry.
        try:
            self.dispatcher.accept(self.handle(event))
        except (OSError, ValueError, KeyError):
            retry = replace(
                event, revision=event.revision + ":retry", deadline=time.time() + 30
            )
            self.dispatcher.deadlines.arm(retry.identity, retry.deadline, retry)
            self.status = "installation-state-unverified"
        return True

    @staticmethod
    def _installed(event, installed, facts):
        if event.revision == installed:
            return True
        if not facts.get("repo"):
            return False
        from pathlib import Path
        from ai_chassis.fleet_liveness import run_git

        proof = run_git(
            ["merge-base", "--is-ancestor", event.revision, installed],
            Path(facts["repo"]),
            None,
        )
        if proof.rc not in {0, 1}:
            raise OSError("installed-ancestry-unverified")
        return proof.rc == 0

    @staticmethod
    def _census():
        from ..producers import _script

        census = _script("harness_sync_census")
        facts = census.refresh_on_event()
        return {**facts, "wake": census.wake_line()}
