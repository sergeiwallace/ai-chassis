#!/usr/bin/env python3
# ruff: noqa: E402
"""Capture and commit a Founding Ask Record entry from stdin."""

from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

_bootstrap_directory = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
while not _bootstrap_os.path.isfile(
    _bootstrap_os.path.join(_bootstrap_directory, "pyproject.toml")
):
    _bootstrap_parent = _bootstrap_os.path.dirname(_bootstrap_directory)
    if _bootstrap_parent == _bootstrap_directory:
        break
    _bootstrap_directory = _bootstrap_parent
_bootstrap_sys.path.insert(0, _bootstrap_os.path.join(_bootstrap_directory, "scripts"))
try:
    from py_interpreter_bootstrap import ensure_supported_python  # noqa: E402
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):  # noqa: E402
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bd_git_safety import strip_git_env

MODULE = Path(__file__).with_name("ask_fidelity.py")
SPEC = importlib.util.spec_from_file_location("ask_fidelity", MODULE)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - installed-file invariant
    raise RuntimeError("could not load ask-fidelity primitives")
ask_fidelity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ask_fidelity
SPEC.loader.exec_module(ask_fidelity)

ENTRY_METADATA = (
    b"entry_id: ",
    b"kind: ",
    b"recorded_at: ",
    b"speaker: ",
    b"supersedes:",
    b"body_sha256: ",
)

# The documented floor for a body fence; a body with no backtick run of its own is
# framed at exactly this width, which is what every record written so far uses.
MIN_BODY_FENCE = 3

# Where a record declares that its canonical-form chain restarts at a named commit.
BASELINES_PATH = Path("docs") / "asks" / "far-migration-baselines.json"

BASELINE_REGISTRY_FIELDS = {"schema_version", "baselines"}
BASELINE_FIELDS = {
    "baseline_content_sha256",
    "reason",
    "superseded_content_sha256",
}

_BACKTICK_RUN = re.compile(rb"`+")
_ENTRY_OPENING = re.compile(rb"(`{4,})text\n")
_TITLE_DASH = "—"


def fence_widths(body: bytes) -> tuple[int, int]:
    """Return the minimal ``(outer, body)`` fence widths that can frame this body.

    A fence has to be longer than the longest backtick run it encloses, so the body
    fence is one longer than the longest run in the captured body and the outer fence
    is one longer than the body fence -- the body fence being the longest run in the
    payload the outer fence encloses. Deriving both from the content is what makes an
    ask that contains its own fence representable at all (ISSUE-REF): a fixed width
    cannot hold one, and `body_sha256` binds the body bytes, so there is no option of
    editing the ask to fit the frame.

    Minimal, not merely sufficient, so that one body has exactly one rendering and
    "canonical" stays a property of the file rather than of whichever writer produced
    it. Measured across all 23 records under `docs/asks/`: this reproduces the
    committed width of all 35 entries, including `ISSUE-REF`, which mixes a 4/3 entry
    with two 5/4 entries in one file.
    """
    longest = max((len(run) for run in _BACKTICK_RUN.findall(body)), default=0)
    inner = max(MIN_BODY_FENCE, longest + 1)
    return inner + 1, inner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True, help="Beads display ID")
    parser.add_argument(
        "--repo",
        help="Repository identity in the FAR (defaults to this session's route)",
    )
    parser.add_argument(
        "--session",
        help="Session name, used to resolve the configured artifact route",
    )
    parser.add_argument(
        "--kind",
        choices=sorted(ask_fidelity.ENTRY_KINDS),
        default="ORIGINAL-ASK",
        help="Entry kind (defaults to ORIGINAL-ASK)",
    )
    parser.add_argument(
        "--supersedes",
        help="Earlier entry ID superseded by a CORRECTION",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help=(
            "Checkout to commit the record into. Defaults to the caller's own linked "
            "worktree when the cwd is one of the routed repository; otherwise the "
            "record is committed off that repository's origin in a throwaway worktree "
            "and pushed, so its shared main tree is never written"
        ),
    )
    return parser.parse_args()


def git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stderr=subprocess.PIPE,
        env=strip_git_env(),
    ).strip()


@dataclass(frozen=True)
class Destination:
    """Which repository this record is written to, and where inside it."""

    repository: str
    root: Path
    record_dir: str
    # True when the routed default is a repository's shared MAIN working tree: the
    # record is then committed off fresh origin and pushed, never onto that tree.
    lands_on_origin: bool = False


def _git_common_dir(root: Path) -> Path:
    return Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()


def _is_shared_main_tree(root: Path) -> bool:
    """A main working tree peers sync from origin: the one a capture must not write.

    Without an `origin` there is nothing to fall behind, so committing in place
    cannot strand a peer's fast-forward and stays the behaviour.
    """
    git_dir = Path(git(root, "rev-parse", "--path-format=absolute", "--git-dir"))
    if git_dir.resolve() != _git_common_dir(root):
        return False
    return "origin" in git(root, "remote").split()


def _caller_worktree_of(registered: Path) -> Path | None:
    """The caller's own linked worktree of `registered`, when the cwd is one (ISSUE-REF).

    A repository's registered root is its MAIN working tree, which the fleet keeps
    read-only, so defaulting to it committed a session's record onto the shared tree's
    local `main` (measured 2026-10-08). A cwd in a linked worktree of the SAME
    repository (same git common dir) is the record's home instead. Any other cwd --
    another repository, or no checkout at all -- leaves the registered root as before,
    because the cwd only ever narrows WHICH checkout of the routed repository holds the
    record; it never chooses the repository.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    caller = Path(probe.stdout.strip()).resolve()
    if caller == registered.resolve():
        return None
    if _git_common_dir(caller) != _git_common_dir(registered):
        return None
    return caller


def resolve_destination(args: argparse.Namespace) -> Destination:
    """Resolve the record's home from configuration, never from the cwd.

    The cwd used to decide this, which is how a record about teammate-visible work
    would have been committed into a teammate-visible repository.  The order is
    explicit flag, then the configured route for this session, then the fleet
    registry entry for the named repository -- and a refusal naming both config
    files when none of those answers.

    When both the repository name and a root are known, they must agree: a record
    minted under a repository identity other than the one holding it produces a
    carrier that no launch can validate, and saying so here beats discovering it
    at the next delegation.

    Two more refusals keep a record out of a teammate-visible repository: a `--repo`
    naming another repository than the session's route, and a destination checkout
    with any example-client-host-corp remote, however it was reached.
    """
    route = ask_fidelity.resolve_artifact_route(args.session) if args.session else None
    if route is not None and args.repo and args.repo != route.repository:
        raise ValueError(
            f"--repo {args.repo!r} contradicts the route for session {args.session!r}, "
            f"which sends its records to {route.repository!r} "
            f"({ask_fidelity.load_artifact_routing()['source']}); drop --repo"
        )
    repository = args.repo or (route.repository if route else None)
    if repository is None:
        raise ValueError(
            "no repository for this record: pass --repo, or add a [[routes]] entry "
            f"matching session {args.session!r} to "
            f"{ask_fidelity.load_artifact_routing()['source']}"
        )
    record_dir = route.ask_record_dir if route else ask_fidelity.ask_record_dir()
    # A route's directory reaches here without the check ask_record_dir() applies.
    if (
        not isinstance(record_dir, str)
        or not record_dir
        or Path(record_dir).is_absolute()
        or ".." in Path(record_dir).parts
    ):
        raise ValueError(
            f"configured ask_record_dir {record_dir!r} is not a repository-relative path"
        )
    registered: Path | None
    unregistered_reason = ""
    lands_on_origin = False
    try:
        registered = ask_fidelity.resolve_repository_root(repository)
    except ask_fidelity.FidelityError as exc:
        registered = None
        unregistered_reason = str(exc)
    if args.repo_root is None:
        if registered is None:
            raise ValueError(
                f"cannot resolve a root for repository {repository!r}: "
                f"{unregistered_reason}. Pass --repo-root, or register the "
                "repository in config/fleet-projects.toml"
            )
        caller = _caller_worktree_of(registered)
        root = caller or registered
        # ISSUE-REF: a default that falls back to the registered MAIN tree (a cwd in
        # another repository, or in that tree itself) must not commit onto its local
        # branch. Measured 2026-10-29: five such captures stranded the example-client-host-workspace
        # main tree ahead of origin, blocking every peer's fast-forward sync.
        lands_on_origin = caller is None and _is_shared_main_tree(registered)
    else:
        root = args.repo_root.resolve()
        if registered is not None and _git_common_dir(registered) != _git_common_dir(
            root
        ):
            raise ValueError(
                f"--repo {repository!r} resolves to {registered}, which is not the "
                f"repository at --repo-root {root}; the carrier would name a "
                "repository that does not hold the record"
            )
        if registered is None:
            print(
                f"note: repository {repository!r} is not in the fleet project "
                "registry, so a Tier 2/3 launch cannot resolve this record; add it "
                "to config/fleet-projects.toml to make the carrier usable",
                file=sys.stderr,
            )
    try:
        ask_fidelity.refuse_teammate_visible_record_home(repository, root)
    except ask_fidelity.FidelityError as exc:
        raise ValueError(str(exc)) from exc
    return Destination(repository, root, record_dir, lands_on_origin)


def record_title(task_id: str) -> bytes:
    """Render the one accepted record title for this task."""
    return f"# Founding Ask Record {_TITLE_DASH} {task_id}\n\n".encode()


def now_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_bytes(
    task_id: str, capture: bytes, *, recorded_at: str | None = None
) -> bytes:
    """Render a generation-1 record without altering the captured body bytes."""
    return record_title(task_id) + entry_bytes(
        "A001", "ORIGINAL-ASK", "", capture, recorded_at=recorded_at
    )


@dataclass(frozen=True)
class RecordEntry:
    entry_id: str
    kind: str
    supersedes: str
    body_sha256: str
    recorded_at: str
    body: bytes


def entry_bytes(
    entry_id: str,
    kind: str,
    supersedes: str,
    capture: bytes,
    *,
    recorded_at: str | None = None,
) -> bytes:
    """Render one entry in the documented append-only form.

    The fence widths come from the captured body rather than a constant, so a capture
    containing its own fence round-trips instead of being unrepresentable. The caller
    may supply `recorded_at` to re-render an already-recorded entry byte-for-byte;
    a live capture leaves it unset and is stamped now.
    """
    outer_width, inner_width = fence_widths(capture)
    outer = b"`" * outer_width
    inner = b"`" * inner_width
    separator = b"" if capture.endswith(b"\n") else b"\n"
    metadata = (
        f"{outer.decode()}text\n"
        f"entry_id: {entry_id}\n"
        f"kind: {kind}\n"
        f"recorded_at: {recorded_at or now_stamp()}\n"
        "speaker: human\n"
        f"supersedes{': ' + supersedes if supersedes else ':'}\n"
        f"body_sha256: {ask_fidelity.sha256_bytes(capture)}\n\n"
        f"{inner.decode()}text\n"
    ).encode()
    return metadata + capture + separator + inner + b"\n" + outer + b"\n"


def parse_record(content: bytes, task_id: str) -> list[RecordEntry]:
    """Parse the documented FAR format and reject anything ambiguous or altered."""
    prefix = record_title(task_id)
    if not content.startswith(prefix):
        raise ValueError("existing FAR has an invalid title")

    entries: list[RecordEntry] = []
    remaining = content[len(prefix) :]
    while remaining:
        entry, remaining = parse_entry(remaining)
        entries.append(entry)
    validate_entries(entries)
    return entries


def parse_entry(remaining: bytes) -> tuple[RecordEntry, bytes]:
    """Parse one fenced FAR entry and return the still-unconsumed record bytes.

    The fence width is read from the entry's own opening fence rather than assumed,
    and the body fence is required to be exactly one backtick narrower. That pairing
    is what keeps the closing marker unambiguous: the body's longest run is shorter
    than the body fence, so the marker cannot occur inside the body it terminates.
    """
    opening = _ENTRY_OPENING.match(remaining)
    if opening is None:
        raise ValueError("existing FAR has invalid entry framing")
    outer = opening.group(1)
    inner = outer[:-1]
    closing = b"\n" + inner + b"\n" + outer + b"\n"
    body_marker = b"\n\n" + inner + b"text\n"
    end = remaining.find(closing, opening.end())
    if end < 0:
        raise ValueError("existing FAR has an unterminated entry")
    payload = remaining[opening.end() : end]
    if payload.count(body_marker) != 1:
        raise ValueError("existing FAR entry has invalid body framing")
    metadata, body = payload.split(body_marker)
    lines = metadata.splitlines()
    if len(lines) != len(ENTRY_METADATA) or any(
        not line.startswith(label)
        for line, label in zip(lines, ENTRY_METADATA, strict=True)
    ):
        raise ValueError("existing FAR entry has invalid metadata")
    entry_id = lines[0].removeprefix(ENTRY_METADATA[0]).decode("ascii")
    kind = lines[1].removeprefix(ENTRY_METADATA[1]).decode("ascii")
    speaker = lines[3].removeprefix(ENTRY_METADATA[3])
    supersedes_bytes = lines[4].removeprefix(ENTRY_METADATA[4])
    if supersedes_bytes.startswith(b" "):
        supersedes_bytes = supersedes_bytes[1:]
    supersedes = supersedes_bytes.decode("ascii")
    body_sha256 = lines[5].removeprefix(ENTRY_METADATA[5]).decode("ascii")
    if (
        not ask_fidelity.HEX64.fullmatch(body_sha256)
        or speaker != b"human"
        or kind not in ask_fidelity.ENTRY_KINDS
    ):
        raise ValueError("existing FAR entry has invalid metadata")
    # The closing fence's leading newline is either the captured final newline or
    # the renderer's separator. The recorded digest resolves that ambiguity, and the
    # candidate it picks is the captured body itself, which is what a re-render needs.
    captured = next(
        (
            item
            for item in (body, body + b"\n")
            if ask_fidelity.sha256_bytes(item) == body_sha256
        ),
        None,
    )
    if captured is None:
        raise ValueError("existing FAR entry body_sha256 mismatch")
    recorded_at = lines[2].removeprefix(ENTRY_METADATA[2]).decode("ascii")
    entry = RecordEntry(entry_id, kind, supersedes, body_sha256, recorded_at, captured)
    # Canonical is a property of the FILE, not of whichever writer produced it: the
    # entry must be exactly what the renderer would emit for these parsed fields. One
    # check rather than a growing list of per-field strictnesses, and it is the
    # definition rather than a proxy for it -- it pins the fence width to the minimal
    # one, the `supersedes:` spacing, the metadata order and the body separator
    # together. Legacy records deviating in any of those are migrated, not tolerated
    # (ISSUE-REF D-6(b)); `scripts/migrate_far_records.py` is the one-shot tool and
    # holds the only reader that accepts the superseded forms.
    consumed = end + len(closing)
    if (
        entry_bytes(entry_id, kind, supersedes, captured, recorded_at=recorded_at)
        != remaining[:consumed]
    ):
        raise ValueError("existing FAR entry is not in canonical form")
    return entry, remaining[consumed:]


def validate_entries(entries: list[RecordEntry]) -> None:
    if not entries:
        raise ValueError("existing FAR has no entries")
    for index, entry in enumerate(entries, start=1):
        expected_id = f"A{index:03d}"
        if entry.entry_id != expected_id:
            raise ValueError("existing FAR entry IDs are not sequential")
        if index == 1:
            if entry.kind != "ORIGINAL-ASK" or entry.supersedes:
                raise ValueError("existing FAR has an invalid original entry")
        elif not valid_later_entry(entry, entries[: index - 1]):
            raise ValueError("existing FAR has an invalid later entry")


def valid_later_entry(entry: RecordEntry, prior_entries: list[RecordEntry]) -> bool:
    if entry.kind == "FOLLOW-UP":
        return not entry.supersedes
    if entry.kind == "CORRECTION":
        return entry.supersedes in {prior.entry_id for prior in prior_entries}
    return False


def git_bytes(repo_root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), *args],
        stderr=subprocess.PIPE,
        env=strip_git_env(),
    )


def migration_baseline(repo_root: Path, task_id: str) -> dict[str, Any] | None:
    """Return this record's declared chain restart, or None if it declares none.

    A record that has declared no baseline is replayed over its whole history under
    every original check, so the relaxation is the declaration itself and cannot leak
    to a record that never made one. The registry is a closed schema and every field
    is checked, because an unchecked declaration would be a bypass rather than a
    boundary: `superseded_content_sha256` names the exact byte image the chain
    abandons, so a restart has to say in the repository what it is giving up.

    Read from HEAD, not from the working tree, and that is the point rather than an
    implementation detail. This replay is a statement about COMMITTED history, so the
    one input that can relax it has to be committed too. Everything else the replay
    trusts is digest-bound -- a record's bytes are pinned by `content_sha256` and each
    body by `body_sha256`, so editing one is self-detecting -- which leaves the
    registry as the only soft surface. Reading the working tree would let an
    UNCOMMITTED edit mint a root claiming generation 1 with no predecessor while the
    real chain is longer. A staged-but-uncommitted registry therefore reads as absent
    and the record gets the full strict replay, which is the fail-closed direction.
    """
    try:
        raw = git_bytes(repo_root, "show", f"HEAD:{BASELINES_PATH.as_posix()}")
    except (OSError, subprocess.CalledProcessError):
        # No registry in HEAD: no record has declared a baseline, so nothing is relaxed.
        return None
    try:
        registry = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FAR baseline registry is not valid JSON") from exc
    if (
        not isinstance(registry, dict)
        or set(registry) != BASELINE_REGISTRY_FIELDS
        or registry["schema_version"] != 1
        or not isinstance(registry["baselines"], dict)
    ):
        raise ValueError("FAR baseline registry has a closed-schema violation")
    baseline = registry["baselines"].get(task_id)
    if baseline is None:
        return None
    if not isinstance(baseline, dict) or set(baseline) != BASELINE_FIELDS:
        raise ValueError("FAR baseline has a closed-schema violation")
    if not isinstance(
        baseline["baseline_content_sha256"], str
    ) or not ask_fidelity.HEX64.fullmatch(baseline["baseline_content_sha256"]):
        raise ValueError("FAR baseline baseline_content_sha256 is invalid")
    if not isinstance(baseline["reason"], str) or not baseline["reason"].strip():
        raise ValueError("FAR baseline requires a non-empty reason")
    superseded = baseline["superseded_content_sha256"]
    if superseded is not None and (
        not isinstance(superseded, str) or not ask_fidelity.HEX64.fullmatch(superseded)
    ):
        raise ValueError("FAR baseline superseded_content_sha256 is invalid")
    return baseline


def apply_baseline(
    repo_root: Path,
    relative_path: Path,
    commits: list[str],
    baseline: dict[str, Any],
) -> list[str]:
    """Trim a replay to the commits at and after a declared chain restart.

    The commits a restart excludes are, by construction, in the very form the
    migration removed, so a strict parser cannot read them at all -- measured on all
    four migrated records, each refuses at its first historical commit, before any
    growth or byte-prefix comparison is reached. A restart therefore has to stop the
    replay from WALKING that history rather than relax a comparison inside it.

    What it costs is stated rather than hidden: a root after the boundary can no
    longer prove it did not alter an entry recorded before it. The abandoned bytes
    stay in git history and the digest named here pins which image was abandoned, so
    the boundary is reviewable; and the records that needed one carried no integrity
    at all beforehand, because they could produce no root.

    The restart is named by the migrated record's CONTENT digest rather than by the
    migrating commit's hash, so the declaration can land in the same commit as the
    bytes it describes -- a commit cannot name its own hash -- and so a squash or
    rebase of the migrating branch cannot invalidate it. The earliest matching commit
    wins, which is the conservative choice: a re-application later in history
    lengthens the replayed chain rather than shortening it.
    """
    target = baseline["baseline_content_sha256"]
    index = next(
        (
            position
            for position, commit in enumerate(commits)
            if ask_fidelity.sha256_bytes(
                git_bytes(repo_root, "show", f"{commit}:{relative_path.as_posix()}")
            )
            == target
        ),
        None,
    )
    if index is None:
        raise ValueError(
            "FAR baseline names content that is in no commit of the record"
        )
    superseded = baseline["superseded_content_sha256"]
    if index == 0:
        if superseded is not None:
            raise ValueError("FAR baseline supersedes nothing yet names a digest")
    elif superseded is None:
        raise ValueError("FAR baseline must name the content digest it supersedes")
    else:
        previous = git_bytes(
            repo_root, "show", f"{commits[index - 1]}:{relative_path.as_posix()}"
        )
        if ask_fidelity.sha256_bytes(previous) != superseded:
            raise ValueError("FAR baseline superseded_content_sha256 mismatch")
    return commits[index:]


def previous_root_digest(
    repo_root: Path,
    task_id: str,
    repository: str,
    relative_path: Path,
    *,
    upto_generation: int | None = None,
) -> tuple[str, list[RecordEntry], int]:
    """Recreate the committed canonical root of the current FAR generation.

    A generation is one COMMITTED root, not one entry. Those counts coincide only
    while every capture keeps its own commit, and the fleet's own sanctioned
    `gh pr merge --squash` collapses several captures into one — which used to brick
    the record permanently (ISSUE-REF), because generation 1's blob then exists in no
    commit and cannot be chained to. Append-only is proved on CONTENT instead: each
    committed root must add at least one entry and must extend the previous bytes
    exactly, which git history granularity cannot weaken.

    A record may declare that its chain RESTARTS at a named commit, which is what
    makes a one-time migration to canonical form possible at all; see
    `apply_baseline`. Absent that declaration this replays the record's whole history
    under every check.

    ``upto_generation`` stops the replay once that many committed roots have been
    reproduced and returns THAT root, so a published root can be re-derived at its own
    generation after later appends have moved the head (ISSUE-REF). A chain shorter
    than the request returns its last root; the caller reads the returned generation.
    """
    history = git(
        repo_root, "log", "--format=%H", "--reverse", "--", relative_path.as_posix()
    )
    if not history:
        raise ValueError("existing FAR is not committed")
    commits = history.splitlines()
    baseline = migration_baseline(repo_root, task_id)
    if baseline is not None:
        commits = apply_baseline(repo_root, relative_path, commits, baseline)
    prior_digest: str | None = None
    prior_entry_count = 0
    previous_content = b""
    final_entries: list[RecordEntry] = []
    generation = 0
    for commit in commits:
        content = git_bytes(repo_root, "show", f"{commit}:{relative_path.as_posix()}")
        entries = parse_record(content, task_id)
        if len(entries) <= prior_entry_count:
            raise ValueError("existing FAR history is not append-only")
        if prior_entry_count and content[: len(previous_content)] != previous_content:
            raise ValueError("existing FAR history rewrites an earlier entry")
        generation += 1
        blob_oid = git(repo_root, "rev-parse", f"{commit}:{relative_path.as_posix()}")
        carrier, digest = ask_fidelity.make_root(
            task_id=task_id,
            repository=repository,
            path=relative_path.as_posix(),
            capture_commit=commit,
            blob_oid=blob_oid,
            content=content,
            entry_ids=[entry.entry_id for entry in entries],
            generation=generation,
            previous_ref_sha256=prior_digest,
        )
        ask_fidelity.validate_root(carrier, repo_root, digest)
        prior_digest = digest
        prior_entry_count = len(entries)
        previous_content = content
        final_entries = entries
        if upto_generation is not None and generation >= upto_generation:
            break
    return prior_digest or "", final_entries, generation


def prepare_later_generation(  # noqa: PLR0913
    args: argparse.Namespace,
    repo_root: Path,
    relative_path: Path,
    destination: Path,
    repository: str,
) -> tuple[list[RecordEntry], str, bytes, int]:
    if not destination.is_file():
        raise ValueError(f"existing FAR not found: {relative_path}")
    existing = destination.read_bytes()
    entries = parse_record(existing, args.task_id)
    prior_digest, historical_entries, prior_generation = previous_root_digest(
        repo_root, args.task_id, repository, relative_path
    )
    committed_existing = git_bytes(
        repo_root, "show", f"HEAD:{relative_path.as_posix()}"
    )
    if existing != committed_existing or [entry.entry_id for entry in entries] != [
        entry.entry_id for entry in historical_entries
    ]:
        raise ValueError("existing FAR has uncommitted changes")
    if args.kind == "CORRECTION" and args.supersedes not in {
        entry.entry_id for entry in entries
    }:
        raise ValueError("--supersedes must name an existing FAR entry")
    return entries, prior_digest, existing, prior_generation


def capture_commit_message(task_id: str, kind: str) -> str:
    if kind == "ORIGINAL-ASK":
        return f"docs({task_id}): capture founding ask\n\nDelegated-to: none"
    return f"docs({task_id}): capture {kind.lower()} founding ask\n\nDelegated-to: none"


def validate_entry_options(args: argparse.Namespace) -> None:
    if args.kind == "CORRECTION" and not args.supersedes:
        raise ValueError("--kind CORRECTION requires --supersedes")
    if args.kind != "CORRECTION" and args.supersedes:
        raise ValueError("--supersedes is only valid with --kind CORRECTION")


def generation_context(  # noqa: PLR0913
    args: argparse.Namespace,
    repo_root: Path,
    relative_path: Path,
    destination: Path,
    repository: str,
) -> tuple[list[RecordEntry], str | None, bytes, int] | None:
    if args.kind != "ORIGINAL-ASK":
        return prepare_later_generation(
            args, repo_root, relative_path, destination, repository
        )
    if destination.exists():
        return None
    return [], None, b"", 0


def render_generation(
    args: argparse.Namespace,
    entries: list[RecordEntry],
    prefix: bytes,
    capture: bytes,
) -> bytes:
    if args.kind == "ORIGINAL-ASK":
        return record_bytes(args.task_id, capture)
    return prefix + entry_bytes(
        f"A{len(entries) + 1:03d}", args.kind, args.supersedes or "", capture
    )


def entry_ids(entries: list[RecordEntry], kind: str) -> list[str]:
    ids = [entry.entry_id for entry in entries]
    if kind != "ORIGINAL-ASK":
        ids.append(f"A{len(entries) + 1:03d}")
    return ids or ["A001"]


def validate_task_id(task_id: str) -> None:
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise ValueError("task ID must be a non-empty filename, not a path")


LANDING_ATTEMPTS = 3


def _push_target_moved(stderr: str) -> bool:
    """A push refused only because origin moved on, which a fresh attempt can fix."""
    return "fetch first" in stderr or "non-fast-forward" in stderr


def land_on_origin(
    args: argparse.Namespace, destination_repo: Destination, capture: bytes
) -> int:
    """Commit the record off fresh origin in a throwaway worktree and push it.

    The shared main tree is only read (its object store and remote); its branch and
    files are never written, so peers' `merge --ff-only` syncs keep working.

    Two-phase gap: the commit is made, then pushed. A kill between them leaves a
    detached commit in a temporary worktree that no branch references, so nothing
    reaches the shared tree; `git worktree prune` drops the stale registration once
    the temporary directory is gone. A push origin refuses for any reason other than
    a moved target fails the capture closed, with no carrier printed.
    """
    main_tree = destination_repo.root
    branch = git(main_tree, "symbolic-ref", "--short", "HEAD")
    tracking = f"refs/remotes/origin/{branch}"
    for attempt in range(1, LANDING_ATTEMPTS + 1):
        git(main_tree, "fetch", "-q", "origin", f"refs/heads/{branch}:{tracking}")
        with tempfile.TemporaryDirectory(prefix="far-capture-") as scratch:
            pad = Path(scratch) / "tree"
            git(main_tree, "worktree", "add", "-q", "--detach", str(pad), tracking)
            pushed = False
            refusal = ""
            try:
                status, payload = capture_into(args, destination_repo, pad, capture)
                if status != 0:
                    return status
                git(pad, "push", "-q", "origin", f"HEAD:{branch}")
                pushed = True
            except subprocess.CalledProcessError as exc:
                if "push" not in exc.cmd:
                    raise
                refusal = (exc.stderr or "").strip()
            finally:
                git(main_tree, "worktree", "remove", "--force", str(pad))
        if pushed:
            print(payload)
            return 0
        if attempt == LANDING_ATTEMPTS or not _push_target_moved(refusal):
            print(
                f"{refusal}\norigin refused the record for {args.task_id}; "
                f"nothing was written to {main_tree}",
                file=sys.stderr,
            )
            return 1
    return 1


def main() -> int:
    args = parse_args()
    try:
        validate_task_id(args.task_id)
        destination_repo = resolve_destination(args)
        validate_entry_options(args)
        capture = sys.stdin.buffer.read()
        if destination_repo.lands_on_origin:
            return land_on_origin(args, destination_repo, capture)
        status, payload = capture_into(
            args, destination_repo, destination_repo.root, capture
        )
        if status == 0:
            print(payload)
        return status
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def capture_into(
    args: argparse.Namespace,
    destination_repo: Destination,
    repo_root: Path,
    capture: bytes,
) -> tuple[int, str]:
    """Write and commit the record in `repo_root`; return (status, carrier JSON)."""
    relative_path = Path(destination_repo.record_dir) / f"{args.task_id}.md"
    destination = repo_root / relative_path
    context = generation_context(
        args, repo_root, relative_path, destination, destination_repo.repository
    )
    if context is None:
        print(
            "existing FAR requires caller-selected --kind FOLLOW-UP or CORRECTION; "
            "refusing to infer entry kind",
            file=sys.stderr,
        )
        return 1, ""
    entries, prior_digest, rendered_prefix, prior_generation = context

    decision = ask_fidelity.authorize_capture(capture, repo_root, confirmed_safe=True)
    if not decision.launch_allowed:
        print(decision.reason, file=sys.stderr)
        return 1, ""

    rendered = render_generation(
        args,
        entries,
        rendered_prefix,
        decision.bytes_to_persist or b"",
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(rendered)
    git(repo_root, "add", relative_path.as_posix())
    git(
        repo_root,
        "commit",
        "-m",
        capture_commit_message(args.task_id, args.kind),
    )
    capture_commit = git(repo_root, "rev-parse", "HEAD")
    blob_oid = git(repo_root, "rev-parse", f"{capture_commit}:{relative_path}")

    all_entry_ids = entry_ids(entries, args.kind)
    # The root primitive intentionally binds the entire committed record, while
    # body_sha256 in each entry binds just the unedited stdin body.
    carrier, digest = ask_fidelity.make_root(
        task_id=args.task_id,
        repository=destination_repo.repository,
        path=relative_path.as_posix(),
        capture_commit=capture_commit,
        blob_oid=blob_oid,
        content=rendered,
        entry_ids=all_entry_ids,
        generation=prior_generation + 1,
        previous_ref_sha256=prior_digest,
    )
    ask_fidelity.parse_canonical_root(carrier)
    ask_fidelity.validate_root(carrier, repo_root, digest)
    return 0, json.dumps(
        {
            "canonical_root_json": carrier.decode("utf-8"),
            "founding_ask_ref_sha256": digest,
            "docs_asks_path": relative_path.as_posix(),
            "capture_commit": capture_commit,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
