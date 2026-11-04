"""Explicit local recipient sets. Unresolved identities never broaden delivery."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def safe_identity(value: str) -> str:
    if not isinstance(value, str) or not SAFE.fullmatch(value):
        raise ValueError("invalid-identity")
    return value


@dataclass(frozen=True)
class SessionRef:
    slug: str
    session_id: str = ""
    roles: frozenset[str] = frozenset()
    assignees: frozenset[str] = frozenset()
    machine: str = "local"

    def __post_init__(self):
        safe_identity(self.slug)


class TargetSelector(ABC):
    value: str
    kind: str

    @abstractmethod
    def resolve(self, directory) -> frozenset[SessionRef]:
        """Return only positively addressed local sessions."""

    def as_record(self):
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class SessionTarget(TargetSelector):
    value: str
    session_id: str = ""
    kind = "session"

    def __post_init__(self):
        safe_identity(self.value)

    def resolve(self, directory):
        return frozenset(
            s
            for s in directory
            if s.slug == self.value
            and (not self.session_id or s.session_id == self.session_id)
        )

    def as_record(self):
        return {**super().as_record(), "session_id": self.session_id}


@dataclass(frozen=True)
class RoleTarget(TargetSelector):
    value: str
    kind = "role"

    def resolve(self, directory):
        return frozenset(s for s in directory if self.value in s.roles)


@dataclass(frozen=True)
class AssigneeTarget(TargetSelector):
    value: str
    kind = "assignee"

    def resolve(self, directory):
        return frozenset(s for s in directory if self.value in s.assignees)


@dataclass(frozen=True)
class MachineTarget(TargetSelector):
    value: str
    kind = "machine"

    def resolve(self, directory):
        return frozenset(s for s in directory if s.machine == self.value)


def target_from_record(record):
    classes = {
        c.kind: c for c in (SessionTarget, RoleTarget, AssigneeTarget, MachineTarget)
    }
    kind = record["kind"]
    cls = classes[kind]
    if kind == "assignee":
        for part in record["value"].split("@"):
            safe_identity(part)
        if record["value"].count("@") > 1:
            raise ValueError("invalid-assignee")
    else:
        safe_identity(record["value"])
    return (
        cls(record["value"], record.get("session_id", ""))
        if kind == "session"
        else cls(record["value"])
    )
