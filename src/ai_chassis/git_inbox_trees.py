"""Immutable inbox trees cached by object ID; moving refs are observed each pass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ai_chassis.fleet_liveness import GitResult, _refusal_reason
from ai_chassis.notifier_pass import current

NAMESPACE = "refs/fleet-inbox/"
MAIN = "refs/fleet-inbox-read/main"
DIRECTORY = "coordination/session-inbox/"


@dataclass
class Trees:
    git: Callable[[list[str]], GitResult]
    _scope: object | None = field(default=None, init=False)
    _tips: dict[str, str] = field(default_factory=dict, init=False)
    _trees: dict[str, list[tuple[str, str]]] = field(default_factory=dict, init=False)
    reason: str = ""

    def tips(self) -> dict[str, str]:
        scope = current.get()
        if scope is not None and self._scope is scope:
            return self._tips
        self.reason = ""
        result = self.git(
            [
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                NAMESPACE,
                MAIN,
            ]
        )
        if result.rc:
            self.reason = f"for-each-ref-failed ({_refusal_reason(result)})"
            # A failed enumeration is not proof that any cached tip was removed.
            return {}
        tips = {}
        for line in result.out.splitlines():
            fields = line.split()
            if len(fields) == 2:  # noqa: PLR2004 - refname and immutable object ID
                tips[fields[0]] = fields[1]
            elif len(fields) == 1:
                # Injected runners may provide the original refname-only result.
                # Keep it readable, but never cache its mutable tree as an object ID.
                tips[fields[0]] = fields[0]
        self._tips, self._scope = tips, scope
        self._trees = {
            oid: tree for oid, tree in self._trees.items() if oid in tips.values()
        }
        return tips

    def files(self, oid: str) -> list[tuple[str, str]]:
        if oid not in self._trees:
            result = self.git(["ls-tree", "-r", "-z", oid, "--", DIRECTORY])
            if result.rc:
                self.reason = (
                    self.reason or f"ls-tree-failed ({_refusal_reason(result)})"
                )
                return []
            files = []
            for entry in result.out.split("\0"):
                meta, _, path = entry.partition("\t")
                fields = meta.split()
                if len(fields) == 3 and fields[1] == "blob":  # noqa: PLR2004 - Git tree tuple
                    files.append((path, fields[2]))
            if oid.startswith("refs/"):
                return files
            self._trees[oid] = files
        return self._trees[oid]

    def invalidate(self) -> None:
        """A completed fetch may have moved refs inside the current scope."""
        self._scope = None
