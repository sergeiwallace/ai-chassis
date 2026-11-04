#!/usr/bin/env python3
# ruff: noqa: E402
"""Run the staged Gitleaks scan and narrowly filter proven local Beads citations.

Gitleaks remains the detector of record.  This wrapper only changes the exit status
for ``aih-fleet-task-id`` findings for which the prospective commit proves that the
referenced legacy numeric ID belongs to this repository's staged Beads store.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

# The cross-store proof has to locate a sibling checkout, and `~/projects` is the wrong
# answer on machines whose repos live elsewhere (ISSUE-REF), so reuse the shipped
# call-time resolver instead of restating the root here. Imported the way every other
# scripts/ sibling does it. A copy of this file without its siblings dies loudly on this
# import, which for a gate is the fail-closed direction.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_manifest import projects_root  # noqa: E402

TASK_RULE_ID = "aih-fleet-task-id"
FLEET_PREFIX_MANIFEST = "config/bd_fleet_prefixes.yaml"
MISSING_GITLEAKS_REFUSAL = (
    "[gitleaks] REFUSED: {binary} was not found on PATH, so the staged secret and PII "
    "scan did not run and this commit would be scanned by nobody.\n"
    "[gitleaks] This gate no longer fails open (ISSUE-REF). Install the detector and "
    "commit again:\n"
    "[gitleaks]   /workspace/projects   (Step 1e installs a release build "
    "into ~/.local/bin, no sudo)\n"
    "[gitleaks]   or see https://github.com/gitleaks/gitleaks#installing\n"
)
TASK_ID = re.compile(r"^(?P<prefix>[A-Z][A-Z0-9-]*)-(?P<number>[0-9]+)$")
REMOTE_PATH_PART_COUNT = 2


@dataclass(frozen=True)
class RemoteIdentity:
    """A remote's exact, canonical host/owner/repository identity."""

    host: str
    owner: str
    repository: str


@dataclass(frozen=True)
class VisibilityRefresh:
    """The audience classification recorded independently of suppression."""

    classification: str
    source: str


def run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a text command without turning a failed evidence lookup into an exception."""
    overlay = {**os.environ, **env} if env else None
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        input=stdin,
        env=overlay,
    )


def normalize_remote(url: str) -> RemoteIdentity | None:
    """Return an exact normalized remote triple, or ``None`` for unsupported URLs."""
    value = url.strip()
    if not value:
        return None

    # Accept standard scheme URLs and SCP-like git@host:owner/repository URLs.
    scheme = re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://(?P<rest>.+)", value)
    if scheme:
        remainder = scheme["rest"].rsplit("@", 1)[-1]
        host, separator, path = remainder.partition("/")
        if not separator:
            return None
    else:
        scp = re.fullmatch(r"(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>.+)", value)
        if not scp:
            return None
        host = scp["host"]
        path = scp["path"]

    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if (
        not host
        or len(parts) != REMOTE_PATH_PART_COUNT
        or any(not part for part in parts)
    ):
        return None
    owner, repository = parts
    return RemoteIdentity(host.lower(), owner.lower(), repository.lower())


def staged_blob(repository: Path, path: str) -> str | None:
    """Read exactly the staged version of a text file, never the working tree."""
    result = run_command(["git", "show", f":{path}"], cwd=repository)
    return result.stdout if result.returncode == 0 else None


def resolve_host_alias(host: str, *, cwd: Path) -> str:
    """Resolve an ssh_config ``Host`` alias to the real ``HostName`` it points at.

    Some machines give every repository its own SSH alias, so ``origin`` reads
    ``gh-<owner>-<repo>:owner/repo.git`` while ``.beads/config.yaml`` records the
    canonical ``github.com`` URL. Comparing those raw makes the hosts differ even
    though owner and repository match exactly, so no task-id prefix is ever provably
    local, every finding is retained, and no commit touching a file that merely
    mentions a task id can pass. Measured on corp 2026-09-05: normalized
    ``gh-example-user-ai-harness`` vs ``github.com`` blocked a 58-file commit, and
    the failing comparison was reached only after both staged Beads blobs were
    confirmed present, so this is the operative cause rather than missing evidence.

    ``ssh -G`` is the authoritative resolver rather than a guess at the alias naming
    pattern, and it fails safe: it echoes an unrecognized host back unchanged, so a
    missing or unreadable ssh_config degrades to exactly the previous behaviour
    instead of inventing a host. This narrows nothing about foreign prefixes -- the
    owner/repository halves of the comparison are untouched.
    """
    result = run_command(["ssh", "-G", host], cwd=cwd)
    if result.returncode != 0:
        return host
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(" ")
        if separator and key == "hostname" and value:
            return value.lower()
    return host


def resolved_remote_identity(url: str, repository: Path) -> RemoteIdentity | None:
    """Normalize a remote URL, then resolve its host through the machine's ssh_config.

    The single authority for turning a remote URL into a comparable identity, because
    BOTH sides of the store-proof comparison have to be resolved the same way. The
    2026-09-05 alias fix resolved only git ``origin``, on the assumption that the alias
    appears there while ``.beads/config.yaml`` records the canonical ``github.com``
    URL. Measured in ai-harness on 2026-09-16, this machine is the other shape -- both
    sides name the alias -- so resolving one side broke the comparison in the opposite
    direction: ``github.com`` against ``gh-example-user-ai-harness``, hosts differ, no
    prefix ever provably local, every commit citing a task id refused.

    Resolution stays out of ``normalize_remote`` so that function remains pure, and it
    fails safe: ``resolve_host_alias`` echoes an unrecognized host back unchanged, so
    a machine with no ssh_config compares literal hosts exactly as before. Two
    alias-shaped hosts that resolve to different forges still differ -- what decides is
    the resolution, never the shape.
    """
    identity = normalize_remote(url)
    if identity is None:
        return None
    resolved = resolve_host_alias(identity.host, cwd=repository)
    if resolved == identity.host:
        return identity
    return RemoteIdentity(resolved, identity.owner, identity.repository)


CANONICAL_FORGE_HOST = "github.com"


def hosts_name_the_same_forge(store: RemoteIdentity, origin: RemoteIdentity) -> bool:
    """Decide the HOST half of the store proof, after resolution has had its say.

    Resolution decides whenever it produces anything: equal hosts agree, and two hosts
    that resolve apart disagree. This function only adds the case where resolution
    produced NOTHING on one side, which `ssh -G` reports by echoing the host back
    unchanged -- so "an SSH alias this machine has no Host block for" and "some other
    real hostname" are literally the same observation to it. Comparing the strings then
    reads an absence of evidence as proof of a different forge.

    That is not a conservative gate, it is an unusable one. Measured on example-client-host-mac
    2026-10-01: git `origin` is HTTPS `github.com/example-user/ai-harness` while the
    shared `.beads/config.yaml` records the per-repo SSH alias a different machine
    writes, the alias does not resolve here, and so NO staged file citing a legacy
    numeric fleet id could be committed at all -- including this file, which cites one.
    The two shapes already covered were symmetric (both sides the alias, or both the
    canonical host); this is the mixed one, and no test asserted it either way.

    What remains as evidence is the fleet's own per-repo alias SPELLING, and it is used
    by construction rather than by parsing: the alias is REBUILT from the owner and
    repository that must already match exactly, so `gh-<owner>-<repo>` cannot be
    pattern-matched into agreeing about a repository it does not name. Three things this
    deliberately does not admit, each pinned by a test: a different forge sharing the
    owner and repository (`gitlab.com/...`, equally unresolvable, refused because the
    spelling is not the alias), an alias well-formed for another repository (refused on
    owner/repository before reaching here), and an alias that DOES resolve elsewhere
    (refused, because real resolution still wins).

    Naming the convention is a step the 2026-09-05 fix deliberately avoided in favour of
    `ssh -G`, and that ordering is preserved: this is a fallback for when `ssh -G`
    yielded nothing, never a substitute for it. Writing that alias into a store config
    is no escalation either -- the spelling asserts the same owner and repository the
    comparison already requires.
    """
    if store.host == origin.host:
        return True
    if (store.owner, store.repository) != (origin.owner, origin.repository):
        return False
    alias = f"gh-{origin.owner}-{origin.repository}"
    return {store.host, origin.host} == {CANONICAL_FORGE_HOST, alias}


def origin_identity(repository: Path) -> RemoteIdentity | None:
    result = run_command(
        ["git", "config", "--get", "remote.origin.url"], cwd=repository
    )
    if result.returncode != 0:
        return None
    return resolved_remote_identity(result.stdout, repository)


def _beads_config(text: str) -> tuple[str | None, str | None] | None:
    """Parse only the two Beads config fields that authorize local-store proof.

    The pre-commit hook must run with system Python, so this deliberately accepts
    the simple YAML shapes emitted by Beads without adding a YAML runtime dependency.
    Unsupported or malformed shapes are unavailable evidence, not permissive input.
    """
    issue_prefix: str | None = None
    remote: str | None = None
    in_sync = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if re.fullmatch(r"sync\s*:\s*", line):
            in_sync = True
            continue
        if not raw_line[:1].isspace():
            in_sync = False
        prefix = re.fullmatch(
            r"issue-prefix\s*:\s*[\"']?([A-Z][A-Z0-9-]*)[\"']?\s*", line
        )
        if prefix:
            if issue_prefix is not None:
                return None
            issue_prefix = prefix[1]
            continue
        remote_match = re.fullmatch(
            r"\s+remote\s*:\s*[\"']?([^\"']+?)[\"']?\s*", raw_line
        )
        if in_sync and remote_match:
            if remote is not None:
                return None
            remote = remote_match[1]
    return issue_prefix, remote


def _manifest_repository(text: str, prefix: str) -> str | None:
    """Read the fleet's intentionally-simple list-shaped YAML manifest strictly.

    Returns the single repository the manifest registers for ``prefix``, or ``None``
    when the manifest is unreadable in this shape, registers the prefix nowhere, or
    registers it more than once -- all three are unavailable evidence, never a default.
    The name is a bare path segment by the regex's own character class, which is what
    makes it safe to join onto the projects root in ``_fleet_store_declares``.
    """
    matches = re.findall(
        r"^\s*-\s*\{\s*prefix\s*:\s*([A-Z][A-Z0-9-]*)\s*,\s*repo\s*:\s*([A-Za-z0-9._-]+)\s*\}\s*(?:#.*)?$",
        text,
        flags=re.MULTILINE,
    )
    registered = [repo for item_prefix, repo in matches if item_prefix == prefix]
    if len(registered) != 1:
        return None
    return registered[0]


def committed_blob(repository: Path, path: str) -> str | None:
    """Read exactly the COMMITTED version of a text file in another checkout.

    The foreign half of the cross-store proof must not be satisfiable by dropping an
    untracked file into a directory, so this reads ``HEAD:<path>`` -- which also proves
    the directory is a git checkout -- rather than the working tree.
    """
    result = run_command(["git", "show", f"HEAD:{path}"], cwd=repository)
    return result.stdout if result.returncode == 0 else None


def _fleet_store_declares(prefix: str, repository: Path, own_repository: str) -> bool:
    """Whether the registered fleet store that OWNS ``prefix`` declares it itself.

    This is the cross-store route (ISSUE-REF). It does not weaken the authenticity
    argument below; it satisfies the same argument against a different store. The claim
    being proven is narrower than "this looks like a task id": *this prefix is declared
    by a registered fleet store whose checkout exists on this machine*. Three
    independent pieces of evidence, none of them the finding's own text:

    1. the prefix -> repo mapping comes from THIS repository's staged, tracked, closed
       fleet manifest, so adding a mapping is a reviewable diff in the same commit;
    2. the named checkout's own ``origin`` remote must name that repository, so a
       directory of the right name proves nothing by itself;
    3. the prefix must be declared by that store's COMMITTED ``.beads/config.yaml``.

    (1) alone would be spoofable by an author editing the manifest, and that is exactly
    why (3) decides: a forged mapping still has to be corroborated by a store the author
    did not write, so ``{prefix: ZZQ, repo: example-client-host-workspace}`` proves nothing because
    that store declares ``KG``. A prefix no registered store declares stays retained.

    Unlike the manifest route for this repository's OWN prefixes, this one does not
    require the staged store to have authenticated itself against ``origin``: the
    evidence here is wholly external to the staged store, so that store's remote claim
    is not what is being relied upon. The same-repository case is deliberately excluded
    -- it is decided above, and routing it here would let the no-remote arm prove a
    prefix registered HERE that its own store does not declare (ISSUE-REF).
    """
    manifest = staged_blob(repository, FLEET_PREFIX_MANIFEST)
    if manifest is None:
        return False
    registered = _manifest_repository(manifest, prefix)
    if registered is None or registered.lower() == own_repository.lower():
        return False
    checkout = projects_root() / registered
    if not checkout.is_dir():
        # Most machines hold only some of the fleet's checkouts, so this is the common
        # answer, not an edge case -- and it has to be checked rather than left to the
        # subprocess: `run_command` with a non-existent cwd raises, which would turn a
        # routine unproven prefix into a crashed gate.
        return False
    foreign_origin = run_command(
        ["git", "config", "--get", "remote.origin.url"], cwd=checkout
    )
    if foreign_origin.returncode != 0:
        return False
    identity = normalize_remote(foreign_origin.stdout)
    if identity is None or identity.repository != registered.lower():
        return False
    config = committed_blob(checkout, ".beads/config.yaml")
    if config is None:
        return False
    parsed = _beads_config(config)
    return parsed is not None and parsed[0] == prefix


def _store_remote_names_this_repository(
    store_remote: str, own_origin: RemoteIdentity, repository: Path
) -> bool:
    """Whether the store's DECLARED remote resolves to this repository.

    Extracted so ``is_proven_local_task_id`` can distinguish a store that declares no
    remote from one whose declared remote fails this check (ISSUE-REF) without the
    branch pushing the predicate past its complexity ceiling. Behaviour is unchanged:
    both sides resolve through the same resolver, and a host, owner or repository
    mismatch is a refusal.
    """
    store_identity = resolved_remote_identity(store_remote, repository)
    if store_identity is None:
        return False
    if not hosts_name_the_same_forge(store_identity, own_origin):
        return False
    return (store_identity.owner, store_identity.repository) == (
        own_origin.owner,
        own_origin.repository,
    )


def is_proven_local_task_id(repository: Path, prefix: str) -> bool:
    """Implement the complete fail-closed staged store-proof predicate."""
    metadata = staged_blob(repository, ".beads/metadata.json")
    config = staged_blob(repository, ".beads/config.yaml")
    if metadata is None or config is None:
        return False
    try:
        parsed_metadata = json.loads(metadata)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed_metadata, dict):
        return False
    parsed_config = _beads_config(config)
    own_origin = origin_identity(repository)
    if parsed_config is None or own_origin is None:
        return False
    own_prefix, store_remote = parsed_config
    if store_remote is None:
        # A store that declares NO remote is not a store whose remote claim FAILED
        # (ISSUE-REF). `store_remote or ""` used to collapse both into one `None` from
        # `resolved_remote_identity`, and the guard below read that as a failed
        # authenticity claim -- so since the JSONL mirror retirement left every store
        # here declaring `issue-prefix` and no remote, this predicate had never once
        # returned True in this repository, and no commit could add a line citing a
        # legacy numeric task id at all.
        #
        # Deliberately the NARROWEST reading: a store making no remote claim proves only
        # the prefix it declares itself, and the fleet-manifest route below stays behind
        # the remote-authenticated comparison, because without a remote there is no
        # evidence the staged store belongs to this repository. A DECLARED remote that
        # does not resolve, or resolves elsewhere, is still refused -- that arm is the
        # actual anti-spoofing control and nothing here touches it.
        #
        # A prefix this store does not declare can still be proven by the store that
        # DOES declare it, in another registered fleet checkout -- see
        # _fleet_store_declares, whose evidence is external to this store and therefore
        # independent of the missing remote claim (ISSUE-REF).
        if own_prefix == prefix:
            return True
        return _fleet_store_declares(prefix, repository, own_origin.repository)
    if not _store_remote_names_this_repository(store_remote, own_origin, repository):
        return False
    if own_prefix == prefix:
        return True
    manifest = staged_blob(repository, FLEET_PREFIX_MANIFEST)
    registered = (
        _manifest_repository(manifest, prefix) if manifest is not None else None
    )
    if registered is not None and registered.lower() == own_origin.repository.lower():
        return True
    return _fleet_store_declares(prefix, repository, own_origin.repository)


def task_rule_regex(repository: Path) -> re.Pattern[str] | None:
    """Compile the task-ID rule's own regex out of the Gitleaks config.

    Driving recovery from the same config Gitleaks was pointed at keeps one source of
    truth for which prefixes are even task IDs, instead of restating that alternation
    here where it could drift. Broadening the config cannot widen suppression: a
    recovered prefix still has to satisfy ``is_proven_local_task_id``, whose evidence is
    the staged Beads store, so a wider regex only ever produces more prefixes that fail
    that predicate. An unreadable or uncompilable config yields ``None``, which retains
    the finding.
    """
    try:
        parsed = tomllib.loads(
            (repository / ".gitleaks.toml").read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError):
        return None
    rules = parsed.get("rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("id") != TASK_RULE_ID:
            continue
        pattern = rule.get("regex")
        if not isinstance(pattern, str):
            return None
        try:
            return re.compile(pattern)
        except re.error:
            return None
    return None


def _staged_line_prefix(finding: Mapping[str, object], repository: Path) -> str | None:
    """Recover a redacted match's prefix from the staged LINE, never from columns.

    ``StartColumn``/``EndColumn`` are unusable for this. Measured against gitleaks on
    corp 2026-09-05 with a positive control, they are 1-based BYTE offsets carrying an
    extra +1 on every added line of a diff fragment after the first, so slicing a decoded
    ``str`` by them returns a shifted fragment -- ``'IH-857'`` for ``ISSUE-REF``. The shift
    is not a bytes-versus-characters artifact: it reproduces on pure-ASCII lines with no
    multi-byte character anywhere before the match, and it is absent on the first added
    line of a fragment, so no fixed adjustment recovers it either. Because
    ``task_prefix`` returned ``None`` whenever that slice failed to parse, every finding
    was retained and ``is_proven_local_task_id`` was never consulted -- which made the
    store-proof predicate unreachable in practice under ``--redact``.

    ``StartLine`` is sound (verified against the real file line for a hunk starting at
    line 9), so scan that one line with the rule's own regex instead. A line carrying
    more than one distinct prefix yields ``None``: the finding cannot be attributed to a
    single prefix, so retaining it is the fail-closed answer.
    """
    filename = finding.get("File")
    start_line = finding.get("StartLine")
    if not (
        isinstance(filename, str) and isinstance(start_line, int) and start_line > 0
    ):
        return None
    path = PurePosixPath(filename)
    if path.is_absolute() or ".." in path.parts:
        return None
    pattern = task_rule_regex(repository)
    if pattern is None:
        return None
    content = staged_blob(repository, filename)
    if content is None:
        return None
    lines = content.splitlines()
    if start_line > len(lines):
        return None
    prefixes = set()
    for candidate in pattern.findall(lines[start_line - 1]):
        # A capturing group in the rule would make findall yield tuples; only a whole
        # match can be parsed as a task ID, so anything else is unavailable evidence.
        if not isinstance(candidate, str):
            return None
        matched = TASK_ID.fullmatch(candidate)
        if matched:
            prefixes.add(matched["prefix"])
    if len(prefixes) != 1:
        return None
    return prefixes.pop()


def task_prefix(finding: Mapping[str, object], repository: Path) -> str | None:
    """Extract only an existing legacy numeric task-ID match from a Gitleaks finding."""
    if finding.get("RuleID") != TASK_RULE_ID:
        return None
    value = finding.get("Match")
    if isinstance(value, str) and value != "REDACTED":
        matched = TASK_ID.fullmatch(value)
        return matched["prefix"] if matched else None
    return _staged_line_prefix(finding, repository)


def filter_findings(
    findings: Sequence[Mapping[str, object]], repository: Path
) -> tuple[list[Mapping[str, object]], int]:
    """Return retained Gitleaks findings and the number narrowly filtered."""
    retained: list[Mapping[str, object]] = []
    filtered = 0
    for finding in findings:
        prefix = task_prefix(finding, repository)
        if prefix is not None and is_proven_local_task_id(repository, prefix):
            filtered += 1
        else:
            retained.append(finding)
    return retained, filtered


def redacted_findings(
    findings: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Keep retained findings auditable without writing a task ID to hook output."""
    rendered: list[dict[str, object]] = []
    for finding in findings:
        redacted = dict(finding)
        for field in ("Match", "Secret"):
            if field in redacted:
                redacted[field] = "REDACTED"
        rendered.append(redacted)
    return rendered


def _github_classification(repository: Path) -> str | None:
    """Classify a GitHub origin for reporting only; never for finding suppression."""
    origin = origin_identity(repository)
    if origin is None or origin.host != "github.com":
        return None
    info = run_command(
        ["gh", "api", f"repos/{origin.owner}/{origin.repository}"], cwd=repository
    )
    if info.returncode != 0:
        return None
    try:
        payload = json.loads(info.stdout)
        visibility = payload["visibility"]
        owner = payload["owner"]["login"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if visibility == "public":
        return "public"
    collaborators = run_command(
        [
            "gh",
            "api",
            f"repos/{origin.owner}/{origin.repository}/collaborators?per_page=100",
        ],
        cwd=repository,
    )
    if collaborators.returncode != 0:
        return None
    try:
        people = json.loads(collaborators.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(people, list) or not isinstance(owner, str):
        return None
    non_owners = [person for person in people if person.get("login") != owner]
    return "collaborative" if non_owners else "personal"


def _write_derived_visibility(path: Path, classification: str, derived_at: str) -> None:
    """Append or replace only the machine-managed TOML table, preserving hand input."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original:
        tomllib.loads(original)
    without_derived = re.sub(r"(?ms)^\[derived\]\n.*?(?=^\[|\Z)", "", original)
    content = (
        without_derived.rstrip()
        + "\n\n[derived]\n"
        + f'classification = "{classification}"\n'
        + f'derived_at = "{derived_at}"\n'
        + 'source = "github-api"\n'
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def refresh_repository_visibility(repository: Path) -> VisibilityRefresh | None:
    """Refresh the independent D-1 fallback, swallowing all write/API failures.

    Idempotent by design: every commit re-runs this in the pre-commit path, so a
    write-back that always stamped a fresh timestamp would never converge --
    each attempt would re-dirty the file and pre-commit would report "files
    were modified by this hook" forever. Only write when the classification
    itself has actually changed (or no derived entry exists yet); an unchanged
    classification is a no-op, leaving the existing derived_at untouched.
    """
    classification = _github_classification(repository)
    if classification is None:
        return None
    path = repository / ".repo-visibility.toml"
    try:
        existing = (
            tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )
    except (OSError, tomllib.TOMLDecodeError):
        return None
    hand_value = existing.get("classification")
    hand_updated = existing.get("updated_at")
    derived = existing.get("derived")
    derived_classification = (
        derived.get("classification") if isinstance(derived, dict) else None
    )
    derived_at = derived.get("derived_at") if isinstance(derived, dict) else None
    if (
        isinstance(hand_value, str)
        and isinstance(hand_updated, str)
        and isinstance(derived_at, str)
        and hand_updated > derived_at
    ):
        return VisibilityRefresh(hand_value, "hand-authored")
    if derived_classification == classification:
        return VisibilityRefresh(classification, "derived")
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        _write_derived_visibility(path, classification, now)
    except OSError:
        return None
    return VisibilityRefresh(classification, "derived")


# --------------------------------------------------------------------------------------
# Merge scoping (ISSUE-REF)
#
# `gitleaks protect --staged` scans the whole index-vs-HEAD diff. On a non-merge commit
# that diff IS the author's new content, so nothing below applies. On a MERGE commit HEAD
# is only the first parent, so the diff is every byte the other side brings in -- measured
# on 2026-10-06 as all 89 incoming commits -- and a finding anywhere in that already-landed
# history refuses the merge with no remedy the merging session is allowed to use
# (`--no-verify` is forbidden fleet-wide, and editing a peer's trunk doc is not a remedy).
#
# What is scoped away is defined by EVIDENCE, not by the merge's shape: a staged blob is
# skipped only when it is byte-identical to the blob at the same path on the published
# trunk ref, i.e. that exact content is already on trunk and is immutable to this commit.
# Everything else is scanned, which deliberately includes every incoming commit that is
# NOT yet on trunk -- a leaf branch folded into a coordinator branch, an unpushed local
# branch -- because nothing proves those were scanned.
#
# Content already on trunk is not left uncovered: it is in scope for
# `scripts/run_publication_security_scan.py`, whose `history_all_refs` arm runs
# `gitleaks git --log-opts=--all`. That is the scan that can actually act on a finding in
# history; this one cannot.
#
# Every failure in here falls back to the UNSCOPED index, so a broken scope widens the
# scan rather than narrowing it.
# --------------------------------------------------------------------------------------

TRUNK_FALLBACK_BRANCH = "main"
RAW_FIELD_COUNT = 5


@dataclass(frozen=True)
class StagedChange:
    """One path's staged state relative to HEAD, as `git diff --cached --raw` reports it."""

    path: str
    mode: str
    oid: str
    status: str


@dataclass(frozen=True)
class MergeScope:
    """The decision about which staged paths a merge commit's scan covers."""

    scanned: tuple[StagedChange, ...]
    skipped: tuple[str, ...]
    trunk_ref: str


def merge_in_progress(repository: Path) -> bool:
    """Whether this commit is authoring a merge, read from git's own MERGE_HEAD path.

    Deliberately a file-existence check rather than `git rev-parse --verify MERGE_HEAD`:
    an octopus merge writes several revisions into that file and `--verify` refuses a
    multi-revision argument, which would report "not a merge" for the case with the most
    incoming content. `--git-path` resolves correctly inside a linked worktree, where
    `.git` is a file and MERGE_HEAD lives under the worktree's own admin directory.
    """
    result = run_command(
        ["git", "rev-parse", "--git-path", "MERGE_HEAD"], cwd=repository
    )
    if result.returncode != 0:
        return False
    candidate = Path(result.stdout.strip())
    if not candidate.is_absolute():
        candidate = repository / candidate
    return candidate.is_file()


def published_trunk_ref(repository: Path) -> str | None:
    """Resolve the remote-tracking ref that names published trunk, or ``None``.

    Never hardcoded to `origin/main`: the remote's own published default branch is read
    from `refs/remotes/origin/HEAD` first, and the literal fallback is used only when it
    actually resolves. ``None`` means no trunk evidence is available, which switches the
    scan back to the unscoped index.
    """
    symbolic = run_command(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repository
    )
    candidates = []
    if symbolic.returncode == 0 and symbolic.stdout.strip():
        candidates.append(symbolic.stdout.strip())
    candidates.append(f"refs/remotes/origin/{TRUNK_FALLBACK_BRANCH}")
    for candidate in candidates:
        resolved = run_command(
            ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            cwd=repository,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            return candidate
    return None


def staged_changes(repository: Path) -> list[StagedChange] | None:
    """Parse the staged diff against HEAD into per-path mode/oid/status records.

    ``--abbrev=40`` is load-bearing twice over, not cosmetic. `--raw` abbreviates object
    names by default (measured: `7cc57e6`), and an abbreviated name both fails to compare
    equal to the full name `git cat-file --batch-check` reports -- so nothing would ever
    be recognised as already on trunk -- and is rejected by `git update-index
    --index-info`, which made `build_scoped_index` return False and the whole scope fall
    back to the unscoped diff. The symptom was a correct-looking refusal, so only an
    inspection of the intermediate values showed it.
    """
    result = run_command(
        [
            "git",
            "diff",
            "--cached",
            "--raw",
            "-z",
            "--no-renames",
            "--abbrev=40",
            "HEAD",
        ],
        cwd=repository,
    )
    if result.returncode != 0:
        return None
    fields = result.stdout.split("\0")
    changes: list[StagedChange] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index]
        if not header.startswith(":") or index + 1 >= len(fields):
            return None
        parts = header[1:].split()
        if len(parts) != RAW_FIELD_COUNT:
            return None
        _, dst_mode, _, dst_oid, status = parts
        changes.append(StagedChange(fields[index + 1], dst_mode, dst_oid, status))
        index += 2
    return changes


def trunk_blob_oids(
    repository: Path, trunk_ref: str, paths: Sequence[str]
) -> dict[str, str]:
    """Look up each path's blob OID on trunk in one `git cat-file --batch-check` pass.

    A path containing a newline cannot be asked about on this line-oriented protocol, so
    it is simply absent from the result and therefore scanned.
    """
    askable = [path for path in paths if "\n" not in path]
    if not askable:
        return {}
    result = run_command(
        ["git", "cat-file", "--batch-check"],
        cwd=repository,
        stdin="".join(f"{trunk_ref}:{path}\n" for path in askable),
    )
    if result.returncode != 0:
        return {}
    lines = result.stdout.splitlines()
    if len(lines) != len(askable):
        return {}
    found: dict[str, str] = {}
    for path, line in zip(askable, lines, strict=True):
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":  # noqa: PLR2004
            found[path] = parts[0]
    return found


def merge_scope(repository: Path) -> MergeScope | None:
    """Decide the merge scan scope, or ``None`` to scan the unscoped staged index."""
    trunk_ref = published_trunk_ref(repository)
    if trunk_ref is None:
        return None
    changes = staged_changes(repository)
    if changes is None:
        return None
    # A staged deletion adds no content, and the scoped index below is built from HEAD,
    # so a deleted path simply keeps HEAD's bytes and contributes no diff either way.
    candidates = [change for change in changes if change.status != "D"]
    on_trunk = trunk_blob_oids(repository, trunk_ref, [c.path for c in candidates])
    scanned = tuple(c for c in candidates if on_trunk.get(c.path) != c.oid)
    skipped = tuple(c.path for c in candidates if on_trunk.get(c.path) == c.oid)
    return MergeScope(scanned, skipped, trunk_ref)


def build_scoped_index(
    repository: Path, index_path: Path, scanned: Sequence[StagedChange]
) -> bool:
    """Materialise an index holding HEAD plus only the paths this scan covers.

    Returning False means the scoped index is not trustworthy, and the caller must scan
    the real staged index instead. The narrowing is unreachable unless this returns True.
    """
    env = {"GIT_INDEX_FILE": str(index_path)}
    if run_command(["git", "read-tree", "HEAD"], cwd=repository, env=env).returncode:
        return False
    if not scanned:
        return True
    entries = "".join(f"{c.mode} {c.oid}\t{c.path}\n" for c in scanned)
    written = run_command(
        ["git", "update-index", "--add", "--index-info"],
        cwd=repository,
        stdin=entries,
        env=env,
    )
    return written.returncode == 0


def _report_scope(scope: MergeScope, *, applied: bool) -> None:
    """State what the merge scan covers, so a narrowed gate is never a silent one."""
    if not applied:
        print(
            "[gitleaks] merge commit: scope evidence was unavailable, so the FULL "
            "index-vs-HEAD diff is being scanned (ISSUE-REF)",
            file=sys.stderr,
        )
        return
    print(
        "[gitleaks] merge commit: scanning the "
        f"{len(scope.scanned)} staged path(s) this merge authors; "
        f"{len(scope.skipped)} skipped as byte-identical to {scope.trunk_ref}, "
        "where that content already is and where this commit cannot change it "
        "(ISSUE-REF / ISSUE-REF D-3; already-published content is in scope for "
        "scripts/run_publication_security_scan.py, which can act on it)",
        file=sys.stderr,
    )


def scan_environment(repository: Path, workspace: Path) -> Mapping[str, str] | None:
    """Return the env overlay that points the scan at a merge-scoped index, if any.

    `None` means "scan the staged index exactly as before", which is both the non-merge
    answer and the answer for every way the scoping evidence can come up short.
    """
    if not merge_in_progress(repository):
        return None
    scope = merge_scope(repository)
    if scope is None:
        _report_scope(MergeScope((), (), ""), applied=False)
        return None
    index_path = workspace / "scoped.index"
    if not build_scoped_index(repository, index_path, scope.scanned):
        _report_scope(scope, applied=False)
        return None
    _report_scope(scope, applied=True)
    return {"GIT_INDEX_FILE": str(index_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--gitleaks", default="gitleaks")
    args = parser.parse_args(argv)
    repository = args.repository.resolve()

    # The gate REFUSES when its detector is missing, instead of skipping (ISSUE-REF).
    # The previous fail-open lived in the hook's shell `else` branch, where a machine
    # without the binary printed a skip and committed anyway -- so content that this
    # gate exists to stop landed on trunk, and every later merge-based sync performed on
    # a machine that DOES have gitleaks was then blocked by it. A control that is
    # silently absent reads identically to a control that passed, so the only honest
    # answers are "scanned" and "refused".
    if shutil.which(args.gitleaks) is None:
        sys.stderr.write(MISSING_GITLEAKS_REFUSAL.format(binary=args.gitleaks))
        return 1

    # D-1 reporting is deliberately separate from the predicate below.
    refresh_repository_visibility(repository)
    with tempfile.TemporaryDirectory(prefix="aih-gitleaks-staged-") as temporary:
        report = Path(temporary) / "report.json"
        # On a merge this narrows the scan to the content the merge actually authors, by
        # pointing gitleaks' own `git diff --staged` at a scoped index. Measured on
        # gitleaks 8.30.1: GIT_INDEX_FILE is honoured (79 bytes scanned against the real
        # index, 0 against an index read straight from HEAD). Any shortfall in the
        # evidence returns None here, which scans the real staged index unchanged.
        scoped = scan_environment(repository, Path(temporary))
        scan = run_command(
            [
                args.gitleaks,
                "protect",
                "--staged",
                "--config",
                ".gitleaks.toml",
                # Bare, never --redact=100. Older gitleaks builds type --redact as
                # a strict bool and abort at flag parsing on any value
                # ("invalid argument \"100\" for \"--redact\" flag"), which fails
                # the whole hook instead of scanning. The bare form means full
                # redaction on those builds and on newer ones, where the flag
                # carries an optional percent defaulting to 100 -- so this is the
                # one spelling that is portable, and it never redacts less.
                "--redact",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            cwd=repository,
            env=scoped,
        )
        if scan.returncode not in (0, 1) or not report.is_file():
            sys.stderr.write(scan.stderr)
            return scan.returncode or 1
        try:
            findings = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) for item in findings
    ):
        return 1
    retained, filtered = filter_findings(findings, repository)
    if filtered:
        print(
            f"[gitleaks] filtered {filtered} proven on-repository staged task-ID finding(s)",
            file=sys.stderr,
        )
    if retained:
        print(json.dumps(redacted_findings(retained), indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
