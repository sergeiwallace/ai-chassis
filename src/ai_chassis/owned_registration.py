"""Owned JSON entries and convergent publication using the existing atomic writer.

Callers retain their own path, ownership, error-reporting and transaction policies.
None denotes removal; a merge returning None needs no publication or serialization.
"""

import json
from pathlib import Path

from . import codex_integration


class RegistrationError(ValueError):
    """A registration document is not a JSON object."""


def json_object(raw: str | bytes | None) -> dict:
    value = json.loads(raw) if raw is not None else {}
    if not isinstance(value, dict):
        raise RegistrationError("Registration conflict")
    return value


def merge_owned_entry(
    document: dict, mapping: str | None, key: str, entry: object = None
) -> dict | None:
    """Set/remove one owned key, retaining every other entry and document field."""
    entries = dict(document.get(mapping) or {}) if mapping else dict(document)
    if entry is None:
        if key not in entries:
            return None
        del entries[key]
    else:
        if entries.get(key) == entry:
            return None
        entries[key] = entry
    return {**document, mapping: entries} if mapping else entries


def publish_if_changed(path: Path, content: str, *, mode: int | None = None) -> bool:
    """Publish atomically only when bytes differ; keep a converged file untouched."""
    try:
        if path.read_bytes() == content.encode():
            return False
    except FileNotFoundError:
        pass
    codex_integration._replace_config(path, content)
    if mode is not None:
        path.chmod(mode)
    return True
