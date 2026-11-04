"""Public metadata never contains source values."""

from dataclasses import asdict, dataclass


class HandoffError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class Interrupted(BaseException):
    pass


INVALID = "secret-handoff: invalid input."
REFUSED = "secret-handoff: refused: secret access requires your own terminal outside an agent session."
SOURCE_FAILED = "secret-handoff: source failed."
CLEAR_FAILED = "secret-handoff: clipboard clear failed; clear it manually."


@dataclass(frozen=True)
class Request:
    recipe: str
    project: str | None = None
    config: str | None = None
    operation: str | None = None
    host_id: str | None = None
    repo_dir: str | None = None
    name: str | None = None
    backend: str | None = None


@dataclass(frozen=True)
class SourceDescription:
    names: tuple[str, ...]
    requirements: tuple[str, ...]
    side_effect: str


@dataclass(frozen=True)
class Plan:
    schema_version: int
    enabled: bool
    recipe: str
    source_id: str
    target_project: str
    target_config: str
    operation: str
    names: tuple[str, ...]
    count: int
    owner_command_argv: tuple[str, ...]
    requires_local_tty: bool
    reason_codes: tuple[str, ...]

    def metadata(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Receipt:
    name: str
    ordinal: int
    delivered: bool
    cleared: bool


@dataclass(frozen=True)
class CapabilityResult:
    supported: bool
    write: bool
    read: bool
    clear: bool
