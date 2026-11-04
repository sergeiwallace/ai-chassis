"""Immutable projections of the admission result; the notifier holds the writer lock.

I-2's local revision is the view identity. Event claims live beside its claims, never
in the pruned legacy namespace. No receipt here closes a source obligation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Callable

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PROTOCOL = 2
PASSIVE_BYTES = 2048
# Include a fixed overflow pointer inside the passive budget, not as an unreceipted item.
OVERFLOW = "18446744073709551615 more pending; read with: agent-mail-notifier status"
ITEM_BYTES = PASSIVE_BYTES - len(OVERFLOW.encode()) - 3
UNCERTAIN_AFTER_S = 300


def _signature(path: Path) -> tuple[int, ...]:
    stamp = path.stat()
    return (stamp.st_mtime_ns, stamp.st_size, stamp.st_ino, stamp.st_ctime_ns)


@dataclass(frozen=True)
class Generation:
    header: str
    keys: str
    fields: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


class SnapshotCache:
    """Immutable generations, shared by publication, status and uncertain recovery.

    Parent directory metadata detects new/removed generations without opening dormant
    files. Active readers also stat the two files: in-place damage must still be
    repaired, even though generation writers normally use new directory names.
    Mutable receipts, epochs, revisions and recovery budgets are never cached here.
    """

    def __init__(self):
        self.parents: dict[Path, tuple[tuple[int, ...], tuple[Path, ...]]] = {}
        self.parsed: dict[Path, tuple[tuple, Generation]] = {}
        self.observed: dict[Path, tuple[int, ...]] = {}
        self.roots: set[Path] = set()

    def forget(self, parent: Path) -> None:
        self.parents.pop(parent, None)
        self.observed.pop(parent, None)
        for directory in tuple(self.parsed):
            if directory.parent != parent:
                continue
            self.parsed.pop(directory, None)

    def directories(self, parent: Path) -> tuple[bool, tuple[Path, ...]]:
        try:
            signature = _signature(parent)
        except FileNotFoundError:
            self.forget(parent)
            raise
        prior = self.parents.get(parent)
        if prior and prior[0] == signature:
            return False, prior[1]
        paths = tuple(p for p in parent.iterdir() if p.is_dir())
        present = set(paths)
        for removed in tuple(self.parsed):
            if removed.parent == parent and removed not in present:
                self.parsed.pop(removed, None)
        self.parents[parent] = (signature, paths)
        return True, paths

    def generation(self, directory: Path) -> Generation:
        # The directory is the identity; file stamps retain existing damage repair.
        signature = tuple(
            _signature(p) for p in (directory, directory / "header", directory / "keys")
        )
        prior = self.parsed.get(directory)
        if prior and prior[0] == signature:
            return prior[1]
        header = (directory / "header").read_text()
        keys = (directory / "keys").read_text()
        result = Generation(
            header,
            keys,
            tuple(header.split()),
            tuple(tuple(row.split("\t")[0].split()) for row in keys.splitlines()),
        )
        self.parsed[directory] = (signature, result)
        return result

    def changed_slugs(self, root: Path, entries: tuple[Path, ...]) -> set[str]:
        """One cheap change check per retained slug; only changes read generations."""
        parents = {p for p in entries if p.name.endswith(".snapshots")}
        for removed in (self.parents.keys() | self.observed.keys()) - parents:
            if removed.parent == root / "sessions":
                self.forget(removed)
        first_pass = root not in self.roots
        self.roots.add(root)
        changed = set()
        for parent in parents:
            slug = parent.name.removesuffix(".snapshots")
            if not SAFE.fullmatch(slug):
                continue
            try:
                signature = _signature(parent)
            except FileNotFoundError:
                self.forget(parent)
                changed.add(slug)
                continue
            prior = self.observed.get(parent)
            self.observed[parent] = signature
            # Cold dormant state has no authority to deliver by itself. Establish
            # its cheap baseline; live/source/claim protections decide who to read.
            if prior == signature or first_pass and prior is None:
                continue
            changed.add(slug)
            _, directories = self.directories(parent)
            for directory in directories:
                try:
                    self.generation(directory)
                except (OSError, UnicodeError):
                    # An interrupted generation must be retried on the next pass.
                    self.observed.pop(parent, None)
        return changed


_CACHE = SnapshotCache()


def changed_slugs(root: Path, entries: tuple[Path, ...]) -> set[str]:
    return _CACHE.changed_slugs(root, entries)


@dataclass
class _PassGenerations:
    """One slug's retained generations, parsed and stamped once for one pass."""

    directories: tuple[Path, ...]
    generations: tuple[tuple[Path, Generation | Exception], ...]
    #: receipt -> [(keys file, disposition, key, keys mtime_ns)] across the generations.
    receipts: dict[str, list[tuple[Path, str, str, int]]] | None = None
    #: mtime_ns per keys file and receipt, read once per pass.
    stamps: dict[Path, int] = field(default_factory=dict)


def _parsed_generations(parent: Path, scan):
    """Each retained generation of ``parent`` with its parse, or the error that stopped it.

    With a pass scan this is the pass's shared view; without one each generation is
    stamped and parsed now, so a direct caller still sees an in-place repair at once.
    """
    view = _pass_generations(parent, scan)
    if view is not None:
        yield from view.generations
        return
    _, directories = _CACHE.directories(parent)
    for directory in directories:
        try:
            yield directory, _CACHE.generation(directory)
        except (OSError, UnicodeError) as exc:
            yield directory, exc


def _pass_generations(parent: Path, scan) -> _PassGenerations | None:
    """The pass scan's view of ``parent``'s generations, or None without a pass scan.

    A pass observes every open delivery of a slug against the same retained generations,
    and re-stamping each generation on every observation cost seconds per pass on a host
    whose busiest slug retained thousands of them (ISSUE-REF). The view is keyed on the
    cached listing, so a generation published later in the same pass still invalidates
    it; a damaged generation is stamped and repaired once per pass rather than once per
    observation.
    """
    memo = getattr(scan, "snapshot_generations", None)
    if memo is None:
        return None
    _, directories = _CACHE.directories(parent)
    view = memo.get(parent)
    if view is None or view.directories is not directories:
        parsed, stamps = [], {}
        for directory in directories:
            try:
                parsed.append((directory, _CACHE.generation(directory)))
            except (OSError, UnicodeError) as exc:
                parsed.append((directory, exc))
                continue
            # generation() just stamped (directory, header, keys); reuse the keys stamp.
            stamps[directory / "keys"] = _CACHE.parsed[directory][0][2][0]
        view = _PassGenerations(directories, tuple(parsed), stamps=stamps)
        memo[parent] = view
    return view


def _stamp(view: _PassGenerations | None, path: Path) -> int:
    """``path``'s mtime_ns: read once per pass with a pass view, else read now."""
    if view is None:
        return path.stat().st_mtime_ns
    stamp = view.stamps.get(path)
    if stamp is None:
        stamp = view.stamps[path] = path.stat().st_mtime_ns
    return stamp


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fragment(text: str) -> str:
    """The same JSON-safe one-line alphabet as legacy notices; bound a single item."""
    text = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)  # noqa: PLR2004 - ASCII controls
    text = " ".join(text.replace('"', "'").replace("\\", "/").split())
    return text.encode()[:ITEM_BYTES].decode("utf-8", errors="ignore")


def registered(root: Path, *, slug: str | None = None, scan=None) -> dict[str, str]:
    """Only positively registered protocol-2 sessions migrate; no guessed identity.

    Addressed receipt checks need only their recipient's transport. Recheck its
    signature and rewatch proof without scanning every other session's view.
    """
    if slug is not None and not SAFE.fullmatch(slug):
        return {}
    sessions = root / "sessions"
    paths = (
        (
            scan.paths("*.transport.json")
            if scan is not None
            else sessions.glob("*.transport.json")
        )
        if slug is None
        else [sessions / f"{slug}.transport.json"]
    )
    result = {}
    for path in paths:
        slug = path.name.removesuffix(".transport.json")
        try:
            record = (
                scan.json(path) if scan is not None else json.loads(path.read_text())
            )
        except (OSError, ValueError):
            continue
        if (
            isinstance(record, dict)
            and SAFE.fullmatch(slug)
            and record.get("slug") == slug
            and isinstance(record.get("session_id"), str)
            and SAFE.fullmatch(record["session_id"])
        ):
            sid = record["session_id"]
            if record.get("snapshot_protocol") == PROTOCOL or _rewatch_requested(
                root, slug, sid, scan=scan
            ):
                result[slug] = sid
    return result


def _rewatch_requested(root: Path, slug: str, sid: str, *, scan=None) -> bool:
    """A builtins-only CwdChanged registration upgrades an existing transport identity."""
    try:
        sessions = root / "sessions"
        read = scan.text if scan is not None else Path.read_text
        return (
            read(sessions / f"{sid}.snapshot-request").split()
            == [
                "2",
                slug,
                sid,
            ]
            and read(sessions / f"{sid}.slug").strip() == slug
        )
    except (OSError, UnicodeError):
        return False


def rows(items: list[tuple[str, str, str]], pokes: tuple[int, int]) -> list[str]:
    """Fixed passive chunks let natural boundaries drain overflow without reopening a batch."""
    output = []
    for disposition in ("wake", "passive"):
        chunk, used = 0, 0
        for token, kind, text in sorted(set(items)):
            if kind != disposition:
                continue
            text = fragment(text)
            if not text:
                continue
            size = len(text.encode()) + (3 if used else 0)
            if disposition == "passive" and used + size > ITEM_BYTES:
                chunk, used = chunk + 1, 0
                size = len(text.encode())
            key = digest([token, *pokes])
            category = "poke" if token.startswith("poke:") else "event"
            output.append(f"{kind} {chunk} {key} {digest(token)} {category}\t{text}")
            used += size
    return output


def prepare_migration(  # noqa: PLR0913, PLR0917 - capture the legacy transaction before replacement
    root: Path,
    slug: str,
    sid: str,
    view: tuple[str, str],
    items: list[tuple[str, str, str]],
    pokes: tuple[int, int],
    write: Callable[[Path, str], bool],
) -> None:
    """Retain evidence for the unchanged, complete I-2 view before its revision changes.

    The descriptor also lets a legacy consumer already in flight finish receipting
    after the protocol fence. It never credits a changed view or a changed poke.
    """
    sessions = root / "sessions"
    target = sessions / f"{sid}.snapshot-legacy.json"
    if (sessions / f"{sid}.protocol").exists() or target.exists():
        return
    try:
        publication = (root / "delivery.generation").read_text().strip()
        fields = (sessions / f"{slug}.delivery.id").read_text().split()
        old = (
            (sessions / f"{slug}.notice.txt").read_text(),
            (root / "notice.txt").read_text(),
        )
        alias = (root / "notice.id").read_text().strip()
        epoch_path = root / "delivery-seen" / f"{sid}.epoch"
        epoch = epoch_path.read_text().strip() if epoch_path.exists() else "initial"
    except (OSError, UnicodeError):
        return
    if (
        not re.fullmatch(r"[a-f0-9]+", publication)
        or len(fields) != 6  # noqa: PLR2004 - fixed I-2 sidecar schema
        or fields[0] != "1"
        or not all(SAFE.fullmatch(v) for v in (fields[2], fields[3], alias, epoch))
        or fields[4:] != list(map(str, pokes))
        or " | ".join(text.rstrip("\n") for text in old if text.rstrip("\n"))
        != " | ".join(text.rstrip("\n") for text in view if text.rstrip("\n"))
    ):
        return
    write(
        target,
        json.dumps(
            {
                "epoch": epoch,
                "revision": fields[2],
                "aliases": [fields[3], alias],
                "keys": [row.split("\t")[0].split()[2:4] for row in rows(items, pokes)],
            },
            sort_keys=True,
        )
        + "\n",
    )


def _epoch(seen: Path, sid: str, scan=None) -> str:
    path = seen / f"{sid}.epoch"
    if not path.exists():
        return "initial"
    return (scan.text(path) if scan is not None else path.read_text()).strip()


def _inherit_legacy(root: Path, sid: str, *, scan=None) -> None:
    """Translate proven complete views; receiptless claims retain uncertainty and age."""
    target = root / "sessions" / f"{sid}.snapshot-legacy.json"
    if not target.exists():
        return
    record = json.loads(target.read_text())
    if (
        not isinstance(record, dict)
        or not all(
            isinstance(record.get(k), str) and SAFE.fullmatch(record[k])
            for k in ("epoch", "revision")
        )
        or not isinstance(record.get("aliases"), list)
        or not all(isinstance(v, str) and SAFE.fullmatch(v) for v in record["aliases"])
        or not isinstance(record.get("keys"), list)
        or not all(
            isinstance(pair, list)
            and len(pair) == 2  # noqa: PLR2004 - event key and token receipt pair
            and all(
                isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in pair
            )
            for pair in record["keys"]
        )
    ):
        return
    prefix = f"{sid}.{record['epoch']}"
    seen = root / "delivery-seen"
    legacy = seen / f"{prefix}.{record['revision']}"
    try:
        stamp = legacy.stat()
        printed = legacy.read_text() == "shown\n" or any(
            path.exists() and path.stat().st_mtime_ns >= stamp.st_mtime_ns
            for alias in record["aliases"]
            for path in [root / "seen" / f"{sid}.{alias}"]
        )
    except OSError:
        return
    for key, receipt in record["keys"]:
        claim = seen / f"{prefix}.event.{key}"
        try:
            with claim.open("x"):
                pass
            os.utime(claim, (stamp.st_mtime, stamp.st_mtime))
        except FileExistsError:
            pass
        if scan is not None:
            scan.delivery_seen.add(claim)
        if printed:
            shown = seen / f"{prefix}.shown.{receipt}.{key}"
            try:
                shown.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                pass
            if scan is not None:
                scan.delivery_seen.add(shown)


def _uncertain(root: Path, slug: str, prefix: str, *, scan=None) -> dict[str, float]:
    """Find event and batch-only interruptions, including retained old generations.

    A row the hook skipped on purpose (an observed poke, an emptied text) carries a
    skip marker, so its batch-only claim is not an interruption.
    """
    seen = root / "delivery-seen"
    events = {}
    batches = {}
    skipped = set()
    paths = (
        scan.delivery_seen.prefixes.get(prefix, {}).values()
        if scan is not None
        else seen.glob(f"{prefix}.*")
    )
    for path in paths:
        suffix = path.name.removeprefix(prefix + ".")
        if suffix.startswith("event."):
            events[suffix.removeprefix("event.")] = path.stat().st_mtime
        elif suffix.startswith("batch."):
            batches[suffix.removeprefix("batch.")] = path.stat().st_mtime
        elif suffix.startswith("skip."):
            skipped.add(suffix.removeprefix("skip."))
    if batches:
        parent = root / "sessions" / f"{slug}.snapshots"
        readable = (
            generation
            for _, generation in _parsed_generations(parent, scan)
            if not isinstance(generation, Exception)
        )
        for generation in readable:
            try:
                header = generation.fields
                for row in generation.rows:
                    kind, chunk, key, *_ = row
                    batch = f"{kind}.{header[5 if kind == 'wake' else 6]}.{chunk}"
                    if batch in batches and key not in skipped:
                        events.setdefault(key, batches[batch])
            except (ValueError, IndexError):
                continue
    return events


def recover(  # noqa: PLR0913 - retain recovery inputs plus the optional shared pass scan
    root: Path,
    slug: str,
    sid: str,
    records: list[str],
    write: Callable[[Path, str], bool],
    *,
    scan=None,
) -> tuple[list[str], list[dict[str, str]]]:
    """One append-only retry after uncertain delivery; exhaustion stays durably blocked.

    Never delete/reuse a claim: an original consumer might still finish. A distinct
    retry revision bounds that unavoidable uncertain duplicate to one recovery attempt.
    """
    seen = root / "delivery-seen"
    epoch = _epoch(seen, sid, scan)
    if not SAFE.fullmatch(epoch):
        return records, []
    prefix = f"{sid}.{epoch}"
    state_path = seen / f"{prefix}.recovery.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    # A compact/new session first consumes the existing snapshot. Keep its event
    # revisions stable afterward; only the retry budget belongs to the epoch.
    versions_path = root / "sessions" / f"{slug}.snapshots" / "revisions.json"
    versions = json.loads(versions_path.read_text()) if versions_path.exists() else {}
    interrupted = (
        _uncertain(root, slug, prefix, scan=scan)
        if (root / "sessions" / f"{slug}.snapshots").exists()
        else {}
    )
    now = time()
    output = []
    blocked = []
    for row in records:
        meta, text = row.split("\t", 1)
        kind, chunk, key, receipt, category = meta.split()
        base = key
        key = state.get(base, {}).get("key", versions.get(base, key))
        shown = seen / f"{prefix}.shown.{receipt}.{key}"
        failed_at = interrupted.get(key)
        if base in state or (
            not shown.exists()
            and failed_at is not None
            and now - failed_at >= UNCERTAIN_AFTER_S
        ):
            retry = state.setdefault(
                base,
                {"key": digest([key, "uncertain-recovery", 1]), "state": "pending"},
            )
            key = retry["key"]
            versions[base] = key
            retry_shown = seen / f"{prefix}.shown.{receipt}.{key}"
            retry_at = interrupted.get(key)
            retry["state"] = (
                "shown"
                if retry_shown.exists()
                else "blocked"
                if retry_at is not None and now - retry_at >= UNCERTAIN_AFTER_S
                else "pending"
            )
            if retry["state"] == "blocked":
                # Only current pending rows are projected. Digests locate the
                # retained snapshot/claims without publishing any message text.
                blocked.append(
                    {
                        "state": "blocked",
                        "reason": "uncertain-delivery-exhausted",
                        "slug": slug,
                        "session_id": sid,
                        "context_epoch": epoch,
                        "event_key": base,
                        "delivery_key": key,
                        "receipt": receipt,
                    }
                )
        output.append(f"{kind} {chunk} {key} {receipt} {category}\t{text}")
    if state:
        write(state_path, json.dumps(state, sort_keys=True) + "\n")
        if scan is not None:
            scan.delivery_seen.add(state_path)
    if versions:
        write(versions_path, json.dumps(versions, sort_keys=True) + "\n")
    return output, blocked


def publish(  # noqa: C901, PLR0913, PLR0915, PLR0917 - ordered immutable publication and reconciliation
    root: Path,
    slug: str,
    session_id: str,
    revision: str,
    items: list[tuple[str, str, str]],
    pokes: tuple[int, int],
    write: Callable[[Path, str], bool],
    *,
    wake_gains: set[str] | None = None,
    scan=None,
) -> list[dict[str, str]]:
    """Snapshot -> permanent protocol fence -> atomic pointer -> wake readiness.

    Interrupted unreferenced generations are retained. An unchanged projection is not
    rewritten. Readiness identifies the admitted set, independent of passive churn.
    """
    if not all(SAFE.fullmatch(v) for v in (slug, session_id, revision)):
        raise ValueError("snapshot-identity")
    _inherit_legacy(root, session_id, scan=scan)
    originals = rows(items, pokes)
    records, blocked = recover(root, slug, session_id, originals, write, scan=scan)
    wake_keys = [r.split("\t")[0].split()[2] for r in records if r.startswith("wake ")]
    passive_keys = [
        r.split("\t")[0].split()[2] for r in records if r.startswith("passive ")
    ]
    sessions = root / "sessions"
    pointer = sessions / f"{slug}.current"
    # Persist a chained wake identity, so A -> B -> A remains a new admitted gain.
    previous = {}
    try:
        previous = json.loads((sessions / f"{slug}.snapshot.json").read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(previous, dict):
        raise ValueError("snapshot-state")
    prior_keys = previous.get("wake_keys", [])
    wake_id = previous.get("wake_id", "")
    new_keys = set(wake_keys) - set(prior_keys)
    if wake_gains is not None:
        # An issued token returning after absence is still an outstanding
        # delivery, not a fresh initial wake. Explicit pokes and the existing
        # bounded uncertain-delivery retry keep their independent authority.
        admitted = {digest([token, *pokes]) for token in wake_gains}
        base_keys = {r.split("\t")[0].split()[2] for r in originals}
        admitted.update(
            meta[2]
            for row in records
            if (meta := row.split("\t")[0].split())[4] == "poke"
            or meta[2] not in base_keys
        )
        new_keys &= admitted
    if not wake_id or new_keys:
        wake_id = digest([wake_id, wake_keys])
    fingerprint = digest([records, pokes, revision, wake_id])
    generation = previous.get("generation", "")

    def contents(generation: str) -> dict[str, str]:
        files = {
            "header": (
                f"2 {generation} {slug} {revision} {wake_id} {digest(wake_keys)} {digest(passive_keys)} "
                f"{pokes[0]} {pokes[1]} {len(records)}\n"
            ),
            "keys": "\n".join(records) + ("\n" if records else ""),
        }
        for disposition in ("wake", "passive"):
            text = "\n".join(
                r.split("\t", 1)[1] for r in records if r.startswith(disposition + " ")
            )
            files[f"{disposition}.txt"] = text + ("\n" if text else "")
        return files

    intact = False
    if isinstance(generation, str) and SAFE.fullmatch(generation):
        try:
            directory = sessions / f"{slug}.snapshots" / generation
            cached = _CACHE.generation(directory)
            intact = all(
                (
                    cached.header
                    if name == "header"
                    else cached.keys
                    if name == "keys"
                    else (directory / name).read_text()
                )
                == text
                for name, text in contents(generation).items()
            )
        except (OSError, UnicodeError):
            pass
    if previous.get("fingerprint") != fingerprint or not intact:
        generation = uuid.uuid4().hex
        directory = sessions / f"{slug}.snapshots" / generation
        directory.mkdir(parents=True, mode=0o700)
        for name, text in contents(generation).items():
            write(directory / name, text)
        write(
            sessions / f"{slug}.snapshot.json",
            json.dumps(
                {
                    "generation": generation,
                    "fingerprint": fingerprint,
                    "wake_keys": wake_keys,
                    "wake_id": wake_id,
                },
                sort_keys=True,
            )
            + "\n",
        )
    # Reconcile a crash after state or pointer commit without mutating the generation.
    write(sessions / f"{session_id}.protocol", "2\n")
    write(pointer, generation + "\n")
    for path in readiness_paths(root, slug):
        write(path, wake_id + "\n")
    # This writer's completed publication is already observed work. Do not treat
    # its directory/readiness changes as an external delivery on the next pass.
    _CACHE.observed[sessions / f"{slug}.snapshots"] = _signature(
        sessions / f"{slug}.snapshots"
    )
    return blocked


def readiness(root: Path, slug: str) -> Path:
    """Keep readiness outside the legacy `<slug>.notice.txt` filename namespace."""
    return readiness_paths(root, slug)[0]


def readiness_paths(root: Path, slug: str) -> list[Path]:
    from ai_chassis.wake import sessions

    return sessions.delivery_watch_paths(root, slug, "notice")


def shown(
    root: Path, token: str, slug: str, *, machine: bool = False, scan=None
) -> bool:
    """Per-item receipts settle only that carried line, including partial passive batches."""
    for recipient, sid in registered(
        root, slug=None if machine else slug, scan=scan
    ).items():
        if not machine and recipient != slug:
            continue
        # Receipts are immutable evidence across compact epochs and poke revisions.
        receipts = (
            scan.delivery_seen.receipts(sid, digest(token))
            if scan is not None
            else (root / "delivery-seen").glob(f"{sid}.*.shown.{digest(token)}.*")
        )
        if any(receipts):
            return True
    return False


def delivery_state(
    root: Path, slug: str, token: str, *, with_kind: bool = False, scan=None
) -> str:
    """Observe Phase 2's receipts/uncertain claims without creating another ledger."""
    printed = shown(root, token, slug, scan=scan)
    if printed and not with_kind:
        return "shown"
    sid = registered(root, slug=slug, scan=scan).get(slug)
    if sid is None:
        return "unregistered"
    seen = root / "delivery-seen"
    epoch = _epoch(seen, sid, scan)
    if not SAFE.fullmatch(epoch):
        return "unregistered"
    receipt = digest(token)
    matches = _receipt_matches(root, slug, receipt, scan=scan)
    if not printed:
        interrupted = _uncertain(root, slug, f"{sid}.{epoch}", scan=scan)
        uncertain = any(key in interrupted for _, _, key, _ in matches)
        return "uncertain" if uncertain else "pending"
    # One receipt can match the same key in several retained generations with
    # different dispositions. Generation names are random, so listing order must
    # not decide: the newest generation already published when the item was first
    # shown is the one its hook read through the pointer.
    candidates = []
    view = _pass_generations(root / "sessions" / f"{slug}.snapshots", scan)
    # The receipts, and so the first-shown time, depend on the key alone, while one
    # key repeats in every retained generation that carried it (ISSUE-REF).
    first_shown: dict[str, int | None] = {}
    for keys, kind, key, stamp in matches:
        if key not in first_shown:
            receipts = (
                scan.delivery_seen.receipts(sid, receipt, key)
                if scan is not None
                else list(seen.glob(f"{sid}.*.shown.{receipt}.{key}"))
            )
            first_shown[key] = (
                min(_stamp(view, path) for path in receipts) if receipts else None
            )
        at = first_shown[key]
        if at is not None:
            published = stamp if stamp is not None else _stamp(view, keys)
            candidates.append((published <= at, published, keys.parent.name, kind))
    return max(candidates)[3] + "-shown" if candidates else "shown"


def _receipt_matches(
    root: Path, slug: str, receipt: str, *, scan=None
) -> list[tuple[Path, str, str, int | None]]:
    """Every retained generation's (keys file, disposition, key, stamp) rows for one receipt.

    The stamp is the keys file's mtime_ns from this pass's view, or None without a pass
    scan, in which case the caller reads it now.
    """
    parent = root / "sessions" / f"{slug}.snapshots"
    pass_view = _pass_generations(parent, scan)
    if pass_view is not None:
        if pass_view.receipts is None:
            index: dict[str, list[tuple[Path, str, str, int]]] = {}
            for directory, generation in pass_view.generations:
                if isinstance(generation, Exception):
                    raise generation
                keys = directory / "keys"
                stamp = pass_view.stamps[keys]
                for kind, _, key, target, _ in generation.rows:
                    index.setdefault(target, []).append((keys, kind, key, stamp))
            pass_view.receipts = index
        return pass_view.receipts.get(receipt, [])
    _, directories = _CACHE.directories(parent)
    matches = []
    for directory in directories:
        for kind, _, key, target, _ in _CACHE.generation(directory).rows:
            if target == receipt:
                matches.append((directory / "keys", kind, key, None))
    return matches
