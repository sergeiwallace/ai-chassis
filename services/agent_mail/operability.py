from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OperabilityEvidence:
    authentication_defaults: bool
    backend_bypass_resistance: bool
    backup_restore: bool
    git_sqlite_concurrency: bool
    update_rollback: bool
    malicious_content: bool

    @classmethod
    def all_passed(cls) -> "OperabilityEvidence":
        return cls(**dict.fromkeys(cls.__dataclass_fields__, True))

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)

    def failed(self) -> list[str]:
        return [name for name, passed in self.as_dict().items() if not passed]
