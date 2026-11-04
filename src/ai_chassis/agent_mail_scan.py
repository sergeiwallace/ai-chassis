"""One session directory observation, with conservative dormant-recipient filtering.

Filtering never removes state: revision chains, fences and claims remain in place.
Unknown/corrupt source state prevents retirement for that pass.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from ai_chassis import idle_wake
from ai_chassis.idle_wake_snapshots import SAFE

SLUG_SUFFIXES = (
    ".transport.json",
    ".notice.txt",
    ".statusline.json",
    ".delivery.id",
    ".poke.txt",
    ".wake.id",
    ".current",
    ".snapshot.json",
    ".snapshots",
)
SID_SUFFIXES = (".slug", ".protocol", ".snapshot-request", ".snapshot-legacy.json")
CONTROL_SUFFIXES = (".slug", ".transport.json", ".snapshot-request")


@dataclass
class SessionFile:
    signature: tuple[int, ...]
    text: str | None = None
    handled: bool = False
    changed: bool = False

    @cached_property
    def record(self):
        return json.loads(self.text)


def optional_json(path: Path, empty):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return empty


def json_lines(path: Path):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines]


def open_obligations(root: Path) -> set[str]:
    admission = idle_wake.AdmissionStore(root / "idle-wake-admission.json")
    protected = set()
    for record in admission.records.values():
        if not record["closed"]:
            protected.update(record["recipients"])
            if record.get("owner"):
                protected.add(record["owner"])
    return protected


class DeliverySeen:
    """Receipt names observed once per pass; timestamps remain fresh filesystem reads."""

    def __init__(self, directory: Path):
        self.prefixes: dict[str, dict[str, Path]] = {}
        self.shown: dict[tuple[str, str], dict[str, set[Path]]] = {}
        try:
            paths = tuple(directory.iterdir())
        except FileNotFoundError:
            paths = ()
        for path in paths:
            self.add(path)

    def add(self, path: Path) -> None:
        # SID and epoch both allow dots. Index every boundary rather than guessing
        # their split; this preserves the old prefix and cross-epoch glob semantics.
        name = path.name
        boundaries = [i for i, char in enumerate(name) if char == "."]
        for boundary in boundaries:
            prefix, suffix = name[:boundary], name[boundary + 1 :]
            self.prefixes.setdefault(prefix, {})[suffix] = path
            if not suffix.startswith("shown."):
                continue
            receipt, dot, key = suffix.removeprefix("shown.").partition(".")
            if not dot:
                continue
            for split in boundaries:
                if split >= boundary:
                    break
                self.shown.setdefault((name[:split], receipt), {}).setdefault(
                    key, set()
                ).add(path)

    def receipts(self, sid: str, receipt: str, key: str | None = None):
        matches = self.shown.get((sid, receipt), {})
        if key is not None:
            return matches.get(key, ())
        return tuple(path for paths in matches.values() for path in paths)


@dataclass
class SessionScan:
    root: Path
    cache: dict
    excluded: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.entries = tuple((self.root / "sessions").iterdir())
        self.pointers = {}
        self.uncertain = False
        self.changed = set()
        self.requests = {}
        # Per-pass parse of each slug's retained snapshot generations (ISSUE-REF).
        self.snapshot_generations = {}
        self._matching = {}
        self._cold = not self.cache
        self._building = True
        changed_paths = self._observe_controls()
        self.changed.update(filter(None, (self.owner(p.name) for p in changed_paths)))
        self._observe_requests(cold=self._cold)
        present = set(self.entries)
        for path in set(self.cache) - present:
            self.cache.pop(path)
        self._building = False

    def _observe_controls(self):
        changed_paths = []
        # Keep metadata for dormant controls too: an in-place write does not
        # change the parent directory. Content stays lazy except for pointers.
        for path in self.entries:
            if not path.name.endswith(CONTROL_SUFFIXES):
                continue
            prior = self.cache.get(path)
            try:
                current = self._file(path)
            except OSError:
                self.uncertain = True
                continue
            if current.changed:
                changed_paths.append(path)
                current.changed = False
                if path.name.endswith(".slug") and prior and prior.text:
                    self.changed.add(prior.text.strip())
            if not path.name.endswith(".slug"):
                continue
            try:
                text = self._text(path, current).strip()
                if not SAFE.fullmatch(text):
                    self.uncertain = True
                    continue
                self.pointers[path.name.removesuffix(".slug")] = text
            except (OSError, UnicodeError):
                # An unreadable pointer could own any file; never retire this pass.
                self.uncertain = True
        return changed_paths

    def _observe_requests(self, *, cold: bool):
        for path in self.entries:
            if not path.name.endswith(".snapshot-request"):
                continue
            sid = path.name.removesuffix(".snapshot-request")
            try:
                request = self.cache.get(path)
                if request is None:
                    continue  # stat failed in _observe_controls; already uncertain
                fields = self._text(path, request).split()
                slug = self.pointers.get(sid)
                if fields != ["2", slug, sid]:
                    continue
                self.requests[sid] = request
                if cold:
                    # Only a fence strictly newer than the request proves it was
                    # handled: file timestamps are coarse, so an equal mtime cannot
                    # order the two writes, and guessing "handled" drops a request.
                    try:
                        fence = path.with_name(sid + ".protocol").stat()
                        request.handled = (
                            fence.st_mtime_ns > request.signature[2]
                            and self.text(path.with_name(sid + ".protocol")).strip()
                            == "2"
                        )
                    except FileNotFoundError:
                        pass
                if not request.handled:
                    self.changed.add(slug)
            except (OSError, UnicodeError):
                self.uncertain = True

    def _file(self, path: Path) -> SessionFile:
        """Recheck each lookup, including within a pass: hooks do not take our lock."""
        try:
            stamp = path.stat()
        except FileNotFoundError:
            self.cache.pop(path, None)
            raise
        signature = (
            stamp.st_dev,
            stamp.st_ino,
            stamp.st_mtime_ns,
            stamp.st_ctime_ns,
            stamp.st_size,
        )
        prior = self.cache.get(path)
        if prior is None or prior.signature != signature:
            # An addressed lookup can refresh text after the inventory observation.
            # Leave that change pending for the next scan rather than swallowing it.
            changed = prior is not None or not self._cold or not self._building
            prior = self.cache[path] = SessionFile(signature, changed=changed)
        return prior

    def text(self, path: Path) -> str:
        return self._text(path, self._file(path))

    @staticmethod
    def _text(path: Path, entry: SessionFile) -> str:
        if entry.text is None:
            entry.text = path.read_text()
        return entry.text

    def json(self, path: Path):
        self.text(path)
        return self.cache[path].record

    def handled_request(self, sid: str) -> None:
        # Mark only the request observed before publication, never a racing rewrite.
        if sid in self.requests:
            self.requests[sid].handled = True

    @property
    def delivery_seen(self) -> DeliverySeen:
        from ai_chassis.notifier_pass import current

        scope = current.get()
        if scope is None or scope is not getattr(self, "_receipt_scope", None):
            self._delivery_seen = DeliverySeen(self.root / "delivery-seen")
            self._receipt_scope = scope
        return self._delivery_seen

    def owner(self, name: str) -> str | None:
        for suffix in SID_SUFFIXES:
            if name.endswith(suffix):
                return self.pointers.get(name.removesuffix(suffix))
        for suffix in SLUG_SUFFIXES:
            if name.endswith(suffix):
                return name.removesuffix(suffix)
        return None

    def paths(self, pattern: str):
        if pattern not in self._matching:
            self._matching[pattern] = tuple(
                (path, self.owner(path.name))
                for path in self.entries
                if fnmatch.fnmatchcase(path.name, pattern)
            )
        return [
            path
            for path, owner in self._matching[pattern]
            if owner not in self.excluded
        ]

    def claims(self) -> set[str]:
        """Conservatively protect all receipt/claim evidence: no invented expiry."""
        protected = set()
        for directory in ("seen", "delivery-seen"):
            if directory == "delivery-seen":
                prefixes = self.delivery_seen.prefixes
                protected.update(
                    slug for sid, slug in self.pointers.items() if sid in prefixes
                )
                continue
            path = self.root / directory
            if not path.exists():
                continue
            names = [p.name for p in path.iterdir()]
            for sid, slug in self.pointers.items():
                if any(name.startswith(sid + ".") for name in names):
                    protected.add(slug)
        return protected

    def pending(self, names: dict[str, str]) -> set[str]:
        protected = self.claims()
        protected.update(open_obligations(self.root))
        unclaimed = optional_json(self.root / "unclaimed.json", {"lines": []})
        for line in unclaimed.get("lines", []):
            protected.add(line["slug"])
        bodies = optional_json(self.root / "buzz-bodies.json", {"messages": []})
        for message in bodies.get("messages", []):
            if not message.get("handled_at"):
                protected.add(message["session"])
        # A local receipt may still be propagating. Retain it even if its TTL
        # elapsed: the existing source reconciliation owns receipt pruning.
        marked = optional_json(self.root / "marked.json", {"marks": []})
        reverse = {name: slug for slug, name in names.items()}
        for mark in marked.get("marks", []):
            if mark["agent"] in reverse:
                protected.add(reverse[mark["agent"]])
            protected.add(mark["agent"].partition("--")[2])
        # Pending Git receipts are JSONL, not the notifier's marked JSON schema.
        for record in json_lines(self.root / "git-inbox" / "receipts-pending.jsonl"):
            protected.add(record["recipient"].partition("@")[0])
        for record in json_lines(self.root / "held" / "records.jsonl"):
            protected.add(record["session"])
        return protected
