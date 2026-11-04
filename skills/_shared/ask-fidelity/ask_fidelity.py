#!/usr/bin/env python3
"""Deterministic ask-fidelity carriers and gates.

Semantic alignment remains reviewer-owned.  This module validates only bytes,
closed schemas, repository identity, and promotion state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Prompt producers load this module by file path from arbitrary working directories.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.bd_git_safety import strip_git_env  # noqa: E402

ROOT_FIELDS = {
    "schema_version",
    "task_id",
    "repository",
    "path",
    "capture_commit",
    "blob_oid",
    "content_sha256",
    "entry_ids",
    "generation",
    "previous_ref_sha256",
}
RECEIPT_FIELDS = {
    "schema_version",
    "run_id",
    "stage",
    "transition",
    "founding_ask_ref_sha256",
    "subject",
    "reviewer",
    "requirements",
    "scope_changes",
    "verdict",
    "confidence",
    "blocking",
    "evidence",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ENTRY_KINDS = {"ORIGINAL-ASK", "FOLLOW-UP", "CORRECTION"}
DISPOSITIONS = {
    "SATISFIED",
    "PARTIAL",
    "DROPPED",
    "NARROWED",
    "BROADENED",
    "REJECTED",
    "DEFERRED",
    "UNVERIFIED",
}
APPLICABILITY = {"DIRECT", "DEPENDENCY", "DEFERRED", "N/A"}

# The fleet's destructive-operation rules live in context files -- CLAUDE.md, the
# path-scoped `.claude/rules/`, machine overlays -- that a sub-agent launched into a
# fresh worktree does not reliably inherit.  A rule that exists only where the
# launcher can see it is not a control on the launchee, so every brief that omitted
# them was a fresh chance to repeat the measured incident behind ISSUE-REF: a
# delegated agent ran `pkill -f` on a pattern, killed processes beyond its intent,
# caused a host-wide `run_lost` incident, and blocked its own push.  It had not been
# told not to.  The omission belonged to the brief template, not to the agent.
#
# The floor therefore travels in the physical carrier rather than in prose an author
# must remember: `render_brief_carrier` always emits it, `.claude/bin/cx` supplies it
# to any prompt that lacks one, and `validate_launch_prompt` refuses a launch without
# it.  The heading is deliberately pure ASCII -- no em dash.  ISSUE-REF cost ISSUE-REF
# three windows because the FOUNDING ASK heading requires U+2014 and an ASCII hyphen
# looks identical in every editor; a new required heading should not inherit that trap.
DESTRUCTIVE_OPERATION_FLOOR_HEADING = "## DESTRUCTIVE-OPERATION FLOOR (binding)"

DESTRUCTIVE_OPERATION_FLOOR = f"""{DESTRUCTIVE_OPERATION_FLOOR_HEADING}

These bounds hold for every step of this task. They are not advisory, and nothing in the
stage framing above relaxes them. If a bound blocks the task, stop and report rather than
working around it.

- **Never `pkill` or `killall` on a pattern.** Kill one specific PID, or a whole process
  group by its pgid. A pattern kill has already taken out processes beyond its intent and
  caused a host-wide incident.
- **No `rm -rf`. No destructive or irreversible operation.**
- **Confirm every deletion against the actual target before it runs**, and look at what you
  are about to overwrite before you overwrite it.
- **Keep your scratch in your own `mktemp -d` directory or your worktree**, never under your
  launcher's session-name prefix in a shared directory such as `/tmp`; remove it with
  `rm -r <that dir>`. A glob over such a prefix once deleted a launcher's own files.
- **No `sysctl`, and no edit anywhere under `/etc`.**
- **Never print secrets, ever, including indirectly** -- a file redirect, a throwaway
  subshell or shell tracing (`-x`, `set -x`, `setopt xtrace`) is not a loophole. Safe: `[[ -n "${{KEY:-}}" ]] && echo set`.
- **Never set `HTTP_PROXY` or `HTTPS_PROXY`.**
- **`uv` only; never `pip install` anywhere.**
- **Never `--no-verify`.**
"""

# One token per AC clause above, so a gutted or hollowed-out block refuses as loudly as a
# missing one: a required heading with nothing under it is the same forgettable-prose
# failure one layer down.  Matched case-insensitively -- "never set http_proxy" carries the
# same instruction as the upper-case spelling, and refusing over letter case would teach
# authors that the floor is a shibboleth rather than a rule.
DESTRUCTIVE_OPERATION_FLOOR_TOKENS = (
    "pkill",
    "killall",
    "rm -rf",
    "every deletion",
    "sysctl",
    "/etc",
    "secret",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "pip install",
    "--no-verify",
)


# A gitleaks older than the pinned invocation rejects the subcommand outright, so
# this is the tool's own words about itself, not anything derived from the payload.
_MISSING_STDIN_SUBCOMMAND = re.compile(rb'unknown command\s+"stdin"')

# Terminal styling gitleaks writes around its own log lines; stripped so a diagnostic
# stays readable when it is quoted back to an operator.
_ANSI_ESCAPE = re.compile(rb"\x1b\[[0-9;]*m")

# An operator only needs the tool's complaint, not a whole banner, and an unbounded
# diagnostic is how an opaque failure becomes an unreadable one.
_MAX_DIAGNOSTIC_CHARS = 600

# Below this length a capture line is common prose that would censor the tool's own
# words, so payload suppression only considers lines long enough to carry a secret.
_MIN_PAYLOAD_LINE_BYTES = 8


class FidelityError(ValueError):
    """The fidelity contract cannot safely authorize the requested operation."""


def _as_bytes(value: object) -> bytes:
    """Normalize a captured stream, which a runner may hand back as str or None."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return b""


def _detector_diagnostic(returncode: int | None, stderr: object, capture: bytes) -> str:
    """Render the detector's own exit code and complaint for an operator to act on.

    A verdict outside ``{0, 2}`` means nothing was scanned, so gitleaks reached no
    findings to report and its stderr describes the tool. Findings would go to stdout
    and are redacted there anyway, but any stderr line sharing a line with the capture
    is dropped regardless: naming the cause must never be a route for payload bytes to
    reach a log.
    """
    text = _ANSI_ESCAPE.sub(b"", _as_bytes(stderr))
    payload_lines = {
        line.strip()
        for line in capture.splitlines()
        if len(line.strip()) >= _MIN_PAYLOAD_LINE_BYTES
    }
    kept = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not any(payload in line for payload in payload_lines)
    ]
    detail = " | ".join(item.decode("utf-8", "replace") for item in kept)
    if len(detail) > _MAX_DIAGNOSTIC_CHARS:
        detail = detail[:_MAX_DIAGNOSTIC_CHARS] + "..."
    code = "no exit code" if returncode is None else f"exit code {returncode}"
    return f"detector/configuration failure: gitleaks {code}; stderr: {detail or '(empty)'}"


@dataclass(frozen=True)
class CaptureDecision:
    state: str
    launch_allowed: bool
    bytes_to_persist: bytes | None = None
    reason: str = ""


def canonical_json(value: Any) -> bytes:
    """Return RFC-8785 bytes for this contract's closed string/int/list objects."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def scan_capture(
    capture: bytes,
    repo_root: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> tuple[str, str]:
    """Run the pinned in-memory detector.

    Returns the verdict -- ``clear``, ``match``, ``capability-absent``, or ``failure``
    -- paired with an operator-facing diagnostic that is empty unless the verdict is
    ``failure``.  Only exit ``0`` clears and only exit ``2`` is a match; every other
    outcome blocks, and a blocking outcome names the detector's own exit code and
    stderr so a broken toolchain is distinguishable from a clean scan that never ran.
    A repository-local configuration takes precedence when present; otherwise gitleaks'
    built-in rules still scan the capture rather than turning a missing local policy
    into a scanner bypass.
    """
    config = repo_root / ".gitleaks.toml"
    command = ["gitleaks", "stdin"]
    if config.is_file():
        command.extend(["--config", str(config)])
    command.extend(["--exit-code", "2", "--redact=100"])
    try:
        result = runner(
            command,
            input=capture,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The detector never ran, so there is no exit code to report -- only why the
        # invocation itself could not be made.
        return "failure", _detector_diagnostic(None, str(exc), capture)
    verdict = {0: "clear", 2: "match"}.get(result.returncode)
    if verdict is not None:
        return verdict, ""
    # Outside {0, 2} the detector reached no verdict, so nothing was scanned and its
    # diagnostics describe the tool rather than the payload.
    if _MISSING_STDIN_SUBCOMMAND.search(_as_bytes(result.stderr)):
        return "capability-absent", ""
    return "failure", _detector_diagnostic(result.returncode, result.stderr, capture)


def authorize_capture(
    capture: bytes,
    repo_root: Path,
    *,
    confirmed_safe: bool,  # noqa: ARG001 - retained for caller compatibility
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> CaptureDecision:
    """Apply the identical initial/follow-up/correction pre-persistence gate.

    ``confirmed_safe`` remains accepted for caller compatibility; a clear pinned
    detector result is sufficient to authorize persistence.
    """
    detector, diagnostic = scan_capture(capture, repo_root, runner=runner)
    if detector == "match":
        return CaptureDecision(
            "detector-match",
            False,
            reason=(
                "detector match: gitleaks exit code 2 reported a secret in the "
                "capture, so a human-supplied sanitized restatement is required"
            ),
        )
    if detector == "capability-absent":
        return CaptureDecision(
            "detector-capability-absent",
            False,
            reason=(
                "the installed gitleaks has no `stdin` subcommand, so the pinned "
                "detector invocation cannot run; install a build that provides it, "
                "then retry the capture"
            ),
        )
    if detector == "failure":
        return CaptureDecision("detector-failure", False, reason=diagnostic)
    return CaptureDecision("authorized", True, capture)


def parse_canonical_root(carrier: bytes) -> dict[str, Any]:
    try:
        root = json.loads(carrier)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FidelityError("root carrier is not UTF-8 canonical JSON") from exc
    if not isinstance(root, dict) or set(root) != ROOT_FIELDS:
        raise FidelityError("root carrier has a closed-schema field mismatch")
    if canonical_json(root) != carrier:
        raise FidelityError("root carrier is not RFC 8785 canonical JSON")
    validate_root_fields(root)
    return root


def validate_root_fields(root: dict[str, Any]) -> None:  # noqa: C901
    for field in ("task_id", "repository", "path"):
        if not isinstance(root[field], str) or not root[field]:
            raise FidelityError(f"root {field} must be a non-empty string")
    if root["schema_version"] != 1:
        raise FidelityError("root schema_version is unsupported")
    if (
        not isinstance(root["generation"], int)
        or isinstance(root["generation"], bool)
        or root["generation"] < 1
    ):
        raise FidelityError("root generation must be a positive integer")
    if not HEX_OID.fullmatch(root["capture_commit"]):
        raise FidelityError("root capture_commit is invalid")
    if not HEX_OID.fullmatch(root["blob_oid"]):
        raise FidelityError("root blob_oid is invalid")
    if not HEX64.fullmatch(root["content_sha256"]):
        raise FidelityError("root content_sha256 is invalid")
    if (
        not isinstance(root["entry_ids"], list)
        or not root["entry_ids"]
        or not all(
            isinstance(item, str) and re.fullmatch(r"A[0-9]{3,}", item)
            for item in root["entry_ids"]
        )
    ):
        raise FidelityError("root entry_ids is invalid")
    previous = root["previous_ref_sha256"]
    if previous is not None and not HEX64.fullmatch(previous):
        raise FidelityError("root previous_ref_sha256 is invalid")
    if root["generation"] == 1 and previous is not None:
        raise FidelityError("generation 1 cannot have a previous_ref_sha256")
    if root["generation"] > 1 and previous is None:
        raise FidelityError("later generation requires previous_ref_sha256")


def make_root(  # noqa: PLR0913
    *,
    task_id: str,
    repository: str,
    path: str,
    capture_commit: str,
    blob_oid: str,
    content: bytes,
    entry_ids: list[str],
    generation: int = 1,
    previous_ref_sha256: str | None = None,
) -> tuple[bytes, str]:
    root = {
        "schema_version": 1,
        "task_id": task_id,
        "repository": repository,
        "path": path,
        "capture_commit": capture_commit,
        "blob_oid": blob_oid,
        "content_sha256": sha256_bytes(content),
        "entry_ids": entry_ids,
        "generation": generation,
        "previous_ref_sha256": previous_ref_sha256,
    }
    validate_root_fields(root)
    carrier = canonical_json(root)
    return carrier, sha256_bytes(carrier)


def _git(repo: Path, *args: str, strip: bool = True) -> bytes:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(repo), *args], env=strip_git_env()
        )
        return output.strip() if strip else output
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FidelityError(f"git validation failed for {' '.join(args)}") from exc


# ---------------------------------------------------------------------------
# Artifact routing and validation-root resolution.
#
# A founding ask record is committed wherever the fleet ROUTES it, which is not
# always the repository the delegating session happens to be standing in.  On a
# machine whose work repo is teammate-visible, the record has to live in a
# separate workspace repo, because the record carries internal dev-tracking ids.
# The launcher's cwd therefore cannot decide where to look for the record: doing
# so refused every correctly-committed Tier 2/3 carrier whose record was routed
# elsewhere ("root path/capture_commit mismatch"), which is a guard blocking
# correct work rather than a guard catching anything.
#
# Resolution reads the carrier's own `repository` field, mapped through the
# fleet project registry as a CLOSED allowlist.  A field inside the carrier
# choosing the validation root is only safe while the choice is closed: a bare
# registered name and nothing else, resolved to that repository's own checkout.
# Free text, a path, a traversal, or an unregistered name is refused, so the
# guard cannot be aimed at a repository where a same-named path happens to
# exist.  Every unresolvable case raises, so the launch still fails CLOSED.
# ---------------------------------------------------------------------------

#: Overrides for the two config files and the projects root, in that order.  They
#: exist so a test can exercise resolution against a fixture fleet; a value that
#: names nothing readable refuses rather than falling back.
PROJECT_REGISTRY_ENV = "ASK_FIDELITY_PROJECT_REGISTRY"
ARTIFACT_ROUTING_ENV = "ASK_FIDELITY_ARTIFACT_ROUTING"
PROJECTS_DIR_ENV = "ASK_FIDELITY_PROJECTS_DIR"

#: The fleet's machine identity marker.  `AI_HOST` is the documented interface but
#: a non-interactive hook subprocess never sources a login profile, so the marker
#: is the channel that actually carries identity in the contexts this runs in.
MACHINE_MARKER_PATH = Path("/etc/ai-harness/machine")

#: Where a founding ask record may live inside its repository.  Shipped default,
#: overridable per routing config, never inferred.  Bounding the location keeps a
#: carrier from nominating an arbitrary committed file as a founding ask record.
ASK_RECORD_DIR_DEFAULT = "docs/asks"

#: A repository identity is a bare registered name -- never a path or a pattern.
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ArtifactRoute:
    """Where one machine/session class's agent artifacts are git-tracked."""

    repository: str
    ask_record_dir: str
    machine: str
    session_prefix: str


def harness_root() -> Path:
    """The ai-harness checkout this module was loaded from.

    ``resolve()`` is deliberate: the installed path reaches this file through
    ``~/.claude/skills`` -> the checkout, and the config files sit beside the
    real file, not beside the symlink.
    """
    return Path(__file__).resolve().parents[3]


def _config_path(env_var: str, filename: str) -> Path:
    configured = os.environ.get(env_var, "").strip()
    if configured:
        return Path(configured)
    return harness_root() / "config" / filename


def _load_toml(path: Path, subject: str) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - 3.11+ is the floor
        raise FidelityError(
            f"{subject} cannot be read: this interpreter provides no tomllib, so no "
            "repository allowlist can be established"
        ) from exc
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FidelityError(f"{subject} at {path} is unreadable: {exc}") from exc


def _projects_dir() -> Path:
    """The directory holding the fleet's checkouts.

    Derived from the MAIN worktree of this harness checkout, because
    ``parents[...]`` of a linked worktree points at ``.worktrees`` and would make
    sibling agent worktrees look like fleet repositories.
    """
    configured = os.environ.get(PROJECTS_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    root = harness_root()
    try:
        common = _git(
            root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).decode()
    except FidelityError:
        return root.parent
    return Path(common).resolve().parent.parent


def known_project_roots() -> dict[str, Path]:
    """The allowlist: registered repository name -> absolute checkout root.

    Read from ``config/fleet-projects.toml``, the fleet's own registry, so the
    set of repositories a carrier may name is the set the fleet already declares
    rather than a second list to keep in step.  An absent or unparsable registry
    RAISES: with no allowlist there is no closed answer, and an open one is the
    aimable guard this resolution exists to avoid.
    """
    path = _config_path(PROJECT_REGISTRY_ENV, "fleet-projects.toml")
    if not path.is_file():
        raise FidelityError(
            f"fleet project registry is missing at {path}, so no carrier "
            "repository can be resolved to a validation root"
        )
    data = _load_toml(path, "fleet project registry")
    entries = data.get("projects")
    if not isinstance(entries, list) or not entries:
        raise FidelityError(f"fleet project registry at {path} declares no projects")
    projects = _projects_dir()
    roots: dict[str, Path] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _REPOSITORY_NAME.fullmatch(name):
            continue
        declared = entry.get("path")
        roots[name] = (
            Path(declared).expanduser()
            if isinstance(declared, str) and declared
            else projects / name
        )
    if not roots:
        raise FidelityError(f"fleet project registry at {path} names no usable project")
    return roots


def session_repository(slug: str) -> str | None:
    """The registered repository whose ``task_prefix`` opens the session ``slug``, or None.

    ``example-session`` -> ``example-project``: the prefix is the `ai` CLI's session
    prefix from ``config/fleet-projects.toml``, matched whole (``<prefix>`` or
    ``<prefix>-...``), longest first, so ``ai-cli-ISSUE-REF`` is never read as a shorter prefix.
    """
    path = _config_path(PROJECT_REGISTRY_ENV, "fleet-projects.toml")
    entries = _load_toml(path, "fleet project registry").get("projects")
    if not isinstance(entries, list):
        return None
    prefixes = {
        entry["task_prefix"]: entry.get("name")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("task_prefix"), str)
    }
    for prefix in sorted(prefixes, key=len, reverse=True):
        if slug == prefix or slug.startswith(f"{prefix}-"):
            name = prefixes[prefix]
            return name if isinstance(name, str) else None
    return None


def beads_store_repository(repository: str) -> str:
    """The registered repository whose Beads store holds ``repository``'s work.

    ``example-project`` -> ``example-client-host-workspace``: a code repo that must never get a
    ``.beads/`` declares ``beads_store`` in ``config/fleet-projects.toml``; every other repo is its
    own store. Raises `FidelityError` when the registry cannot be read (ISSUE-REF).
    """
    path = _config_path(PROJECT_REGISTRY_ENV, "fleet-projects.toml")
    entries = _load_toml(path, "fleet project registry").get("projects")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") == repository:
            store = entry.get("beads_store")
            return store if isinstance(store, str) and store else repository
    return repository


def resolve_repository_root(repository: str) -> Path:
    """Resolve a carrier's ``repository`` field to its checkout via the allowlist."""
    if not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(repository):
        raise FidelityError(
            f"carrier repository {repository!r} is not a bare registered "
            "repository name"
        )
    root = known_project_roots().get(repository)
    if root is None:
        raise FidelityError(
            f"carrier repository {repository!r} is not named in the fleet project "
            "registry, so no validation root is permitted for it"
        )
    if not (root / ".git").exists():
        raise FidelityError(
            f"carrier repository {repository!r} has no checkout at {root} on this "
            "machine"
        )
    toplevel = _git(root, "rev-parse", "--show-toplevel").decode()
    if Path(toplevel).resolve() != root.resolve():
        raise FidelityError(
            f"{root} is not the top level of a git checkout for {repository!r}"
        )
    return root


def load_artifact_routing() -> dict[str, Any]:
    """The agent-artifact routing table, or an empty one when none is configured.

    ABSENT and UNREADABLE are different answers.  Absent means this machine
    declares no routes, so a caller asking "where do my artifacts go" is told
    explicitly that nothing is configured -- it never falls back to a cwd.  A
    file that exists but cannot be read raises.
    """
    path = _config_path(ARTIFACT_ROUTING_ENV, "agent_artifact_routing.toml")
    if not path.is_file():
        return {
            "version": 1,
            "routes": [],
            "held_routes": [],
            "source": str(path),
            "configured": False,
        }
    data = _load_toml(path, "agent artifact routing")
    routes = data.get("routes", [])
    if not isinstance(routes, list):
        raise FidelityError(f"agent artifact routing at {path} has a non-list `routes`")
    held_routes = data.get("held_routes", [])
    if not isinstance(held_routes, list):
        raise FidelityError(
            f"agent artifact routing at {path} has a non-list `held_routes`"
        )
    return {
        "version": data.get("version"),
        "ask_record_dir": data.get("ask_record_dir"),
        "routes": routes,
        "held_routes": held_routes,
        "source": str(path),
        "configured": True,
    }


def _repository_relative_dir(declared: Any, owner: str) -> str:
    if (
        not isinstance(declared, str)
        or not declared
        or Path(declared).is_absolute()
        or ".." in Path(declared).parts
    ):
        raise FidelityError(
            f"{owner} ask_record_dir {declared!r} is not a repository-relative path"
        )
    return Path(declared).as_posix()


def ask_record_dir() -> str:
    """The repository-relative directory a founding ask record must live in."""
    declared = load_artifact_routing().get("ask_record_dir")
    if declared is None:
        return ASK_RECORD_DIR_DEFAULT
    return _repository_relative_dir(declared, "configured")


def _route_record_dir(entry: dict[str, Any], routing: dict[str, Any]) -> str:
    """A route's own ask_record_dir, or the top-level one it inherits, checked."""
    owner = (
        f"route (machine {entry.get('machine', '*')!r}, session_prefix "
        f"{entry.get('session_prefix', '')!r}, repository {entry.get('repository')!r}) "
        f"in {routing['source']}"
    )
    own = entry.get("ask_record_dir")
    if own:
        return _repository_relative_dir(own, owner)
    inherited = routing.get("ask_record_dir")
    if inherited is None:
        return ASK_RECORD_DIR_DEFAULT
    return _repository_relative_dir(inherited, f"top-level, inherited by {owner},")


def ask_record_dirs_for(repository: str) -> list[str]:
    """Every directory a record held in ``repository`` may live in.

    The top-level directory, plus the directory of each route that names this
    repository, so a record captured through a route that sets its own
    ``ask_record_dir`` validates against that route's directory.
    """
    routing = load_artifact_routing()
    allowed = [ask_record_dir()]
    for entry in routing["routes"]:
        if isinstance(entry, dict) and entry.get("repository") == repository:
            routed = _route_record_dir(entry, routing)
            if routed not in allowed:
                allowed.append(routed)
    return allowed


def machine_identity() -> str | None:
    """This host's fleet identity, or None when nothing establishes it."""
    from_env = os.environ.get("AI_HOST", "").strip()
    if from_env:
        return from_env
    try:
        marker = MACHINE_MARKER_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return marker or None


def resolve_artifact_route(
    session: str, *, machine: str | None = None
) -> ArtifactRoute | None:
    """The configured artifact destination for one session on this machine.

    First matching route in file order wins, so ordering in the config is the
    precedence.  ``None`` means nothing is configured for this session, which the
    caller must report rather than guess around.
    """
    routing = load_artifact_routing()
    host = machine if machine is not None else machine_identity()
    for entry in routing["routes"]:
        if not isinstance(entry, dict):
            raise FidelityError(
                f"agent artifact routing at {routing['source']} has a non-table route"
            )
        wanted_machine = entry.get("machine", "*")
        prefix = entry.get("session_prefix", "")
        repository = entry.get("repository")
        if not isinstance(repository, str) or not _REPOSITORY_NAME.fullmatch(
            repository
        ):
            raise FidelityError(
                f"agent artifact routing at {routing['source']} declares "
                f"repository {repository!r}, which is not a bare repository name"
            )
        if wanted_machine not in ("*", host):
            continue
        if not isinstance(prefix, str) or not session.startswith(prefix):
            continue
        return ArtifactRoute(
            repository=repository,
            ask_record_dir=_route_record_dir(entry, routing),
            machine=str(wanted_machine),
            session_prefix=prefix,
        )
    return None


# ---------------------------------------------------------------------------
# Held-output routing (issue-ref D-11, the maintainer's ask A003).
#
# A held item carries internal ids, so its store is ROUTED by the repo the held
# work belongs to, through the same table, the same allowlist and the same
# refusal shape as the founding ask record.  The `"*"` source row routes
# personal repos only: the source's class is measured from its registered
# checkout's origin, exactly as a destination's is, so a `example-client-host-corp` source with
# no explicit row refuses instead of landing example-client-host content in a personal repo.
# ---------------------------------------------------------------------------

HELD_STORE_DIR = "docs/held"
HELD_LAND_MODES = ("pr", "push")
HELD_BRANCH_DEFAULT = "main"
_CLIENT_CORP_ORIGIN = re.compile(r"(?:^|[/:])example-client-host-corp/")
_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class HeldRouteUnconfigured(FidelityError):
    """No `[[held_routes]]` row routes this source repo on this machine."""


@dataclass(frozen=True)
class HeldStore:
    """One routed held store: a registered repo's `docs/held` on this host."""

    repository: str
    destination: str
    root: Path
    path: Path
    land: str
    branch: str


@dataclass(frozen=True)
class HeldRoute:
    """Where one source repo's held items are committed, and the row that said so."""

    source_repo: str
    matched_machine: str
    matched_source: str
    store: HeldStore


def _held_rows(routing: dict[str, Any]) -> list[dict[str, str]]:
    """The table's rows in file order, each field checked; a malformed row raises.

    A named ``source_repo`` must be registered in the fleet project registry (D-11), so a
    row can never widen the allowlist the destination is held to.
    """
    source = routing["source"]
    if not routing["configured"]:
        raise HeldRouteUnconfigured(
            f"no held route is configured: {source} is absent, so no [[held_routes]] "
            "row exists on this machine"
        )
    rows: list[dict[str, str]] = []
    for entry in routing["held_routes"]:
        if not isinstance(entry, dict):
            raise FidelityError(f"[[held_routes]] in {source} has a non-table row")
        machine = entry.get("machine", "*")
        source_repo = entry.get("source_repo")
        destination = entry.get("destination")
        land = entry.get("land")
        branch = entry.get("branch", HELD_BRANCH_DEFAULT)
        if not isinstance(machine, str) or not machine:
            raise FidelityError(
                f"[[held_routes]] in {source} declares machine {machine!r}"
            )
        if source_repo != "*" and not (
            isinstance(source_repo, str) and _REPOSITORY_NAME.fullmatch(source_repo)
        ):
            raise FidelityError(
                f"[[held_routes]] in {source} declares source_repo {source_repo!r}, "
                'which is neither "*" nor a bare repository name'
            )
        if land not in HELD_LAND_MODES:
            raise FidelityError(
                f"[[held_routes]] in {source} declares land {land!r} for "
                f"{destination!r}; it must be one of {', '.join(HELD_LAND_MODES)}"
            )
        if not isinstance(branch, str) or not _BRANCH_NAME.fullmatch(branch):
            raise FidelityError(
                f"[[held_routes]] in {source} declares branch {branch!r}"
            )
        if not isinstance(destination, str):
            raise FidelityError(
                f"[[held_routes]] in {source} declares destination {destination!r}"
            )
        rows.append(
            {
                "machine": machine,
                "source_repo": source_repo,
                "destination": destination,
                "land": land,
                "branch": branch,
            }
        )
    _check_registered_sources(rows, source)
    return rows


def _check_registered_sources(rows: list[dict[str, str]], source: str) -> None:
    """Refuse the table when a row names a ``source_repo`` the registry does not carry."""
    named = sorted({row["source_repo"] for row in rows} - {"*"})
    unregistered = [name for name in named if name not in known_project_roots()]
    if unregistered:
        raise FidelityError(
            f"[[held_routes]] in {source} declares source_repo {unregistered[0]!r}, "
            "which is not named in the fleet project registry, so it is not a "
            "permitted held source"
        )


def _origin_url(repository: str, root: Path, role: str) -> str:
    """``origin``'s URL for a registered checkout; anything unmeasurable raises."""
    if not (root / ".git").exists():
        raise FidelityError(
            f"{role} repo {repository!r} has no checkout at {root} on this machine, "
            "so its origin cannot be measured"
        )
    try:
        return _git(root, "remote", "get-url", "origin").decode()
    except FidelityError as exc:
        raise FidelityError(
            f"{role} repo {repository!r} at {root} has no readable origin, so its "
            "class cannot be measured"
        ) from exc


def refuse_teammate_visible_record_home(repository: str, root: Path) -> None:
    """Refuse a founding ask record home whose checkout has any example-client-host-corp remote.

    A record carries the owner's verbatim words and internal ids, so a teammate-visible
    repository never receives one, whatever name or root the caller passed. Every remote
    is read, not only ``origin``; a remote list that cannot be read refuses.
    """
    try:
        remotes = _git(root, "remote", "-v").decode()
    except FidelityError as exc:
        raise FidelityError(
            f"record home {repository!r} at {root} has no readable remote list, so "
            "its class cannot be measured"
        ) from exc
    for line in remotes.splitlines():
        if _CLIENT_CORP_ORIGIN.search(line):
            raise FidelityError(
                f"record home {repository!r} at {root} is a example-client-host-corp repository "
                f"({line.split()[1]}), which is teammate-visible; a founding ask record "
                "never lands there. Capture with --session so the route picks its home"
            )


def held_store(destination: str, *, land: str, branch: str) -> HeldStore:
    """A projects-relative destination resolved through the allowlist (no checkout read)."""
    parts = Path(destination).parts if isinstance(destination, str) else ()
    if (
        len(parts) != len(Path(HELD_STORE_DIR).parts) + 1
        or Path(*parts[1:]).as_posix() != HELD_STORE_DIR
        or not _REPOSITORY_NAME.fullmatch(parts[0])
    ):
        raise FidelityError(
            f"held destination {destination!r} is not a projects-relative "
            f"'<repo>/{HELD_STORE_DIR}' path"
        )
    repository = parts[0]
    root = known_project_roots().get(repository)
    if root is None:
        raise FidelityError(
            f"held destination repo {repository!r} is not named in the fleet project "
            "registry, so it is not a permitted held store"
        )
    return HeldStore(
        repository=repository,
        destination=destination,
        root=root,
        path=root / HELD_STORE_DIR,
        land=land,
        branch=branch,
    )


def check_held_store(store: HeldStore) -> HeldStore:
    """Refuse a store whose checkout is absent or whose origin is a example-client-host-corp repo."""
    url = _origin_url(store.repository, store.root, "held destination")
    if _CLIENT_CORP_ORIGIN.search(url):
        raise FidelityError(
            f"held destination repo {store.repository!r} is a example-client-host-corp repository "
            f"(origin {url}); held items name internal ids and never land there"
        )
    return store


def _describe_row(row: dict[str, str]) -> str:
    return (
        f"(machine {row['machine']!r}, source_repo {row['source_repo']!r}, "
        f"land {row['land']}->{row['branch']})"
    )


def _store_modes(
    rows: list[dict[str, str]], host: str | None, source: str
) -> dict[str, dict[str, str]]:
    """Destination -> the FIRST row on this host naming it, which sets its land and branch.

    A machine row above the `"*"` rows overrides how a destination lands on that host. Two
    rows with the same ``machine`` value that land one destination two ways cannot be
    ordered by intent, so they refuse naming both.
    """
    first: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["machine"] not in ("*", host):
            continue
        prior = first.setdefault(row["destination"], row)
        if (
            prior is not row
            and prior["machine"] == row["machine"]
            and (prior["land"], prior["branch"]) != (row["land"], row["branch"])
        ):
            raise FidelityError(
                f"[[held_routes]] in {source} land {row['destination']!r} two ways "
                f"for machine {row['machine']!r}: {_describe_row(prior)} and "
                f"{_describe_row(row)}"
            )
    return first


def held_stores(*, machine: str | None = None) -> list[dict[str, Any]]:
    """Every distinct destination this machine's rows route to, in file order.

    Each entry is ``{destination, land, branch, store, error}``: ``store`` is the
    `HeldStore` when the destination resolves through the allowlist and its checkout
    passes `check_held_store`, else ``error`` carries why, so one bad store never
    hides the others.  A destination's ``land`` and ``branch`` come from the first
    row on this machine naming it (`_store_modes`).  A table that cannot be read, or
    two same-machine rows landing one destination two ways, raises.
    """
    routing = load_artifact_routing()
    host = machine if machine is not None else machine_identity()
    seen: dict[str, dict[str, Any]] = {}
    for row in _store_modes(_held_rows(routing), host, routing["source"]).values():
        entry: dict[str, Any] = {
            "destination": row["destination"],
            "land": row["land"],
            "branch": row["branch"],
            "store": None,
            "path": None,
            "error": None,
        }
        try:
            store = held_store(
                row["destination"], land=row["land"], branch=row["branch"]
            )
            entry["path"] = store.path
            entry["store"] = check_held_store(store)
        except FidelityError as exc:
            entry["error"] = str(exc)
        seen[row["destination"]] = entry
    if not seen:
        raise HeldRouteUnconfigured(
            f"no [[held_routes]] row in {routing['source']} applies to machine {host!r}"
        )
    return list(seen.values())


def resolve_held_route(source_repo: str, *, machine: str | None = None) -> HeldRoute:
    """The held store for one source repo on this machine; every refusal raises.

    First matching row in file order wins, and the destination lands by the first row
    on this machine naming it (`_store_modes`).  `HeldRouteUnconfigured` means no row
    routes this source (including a example-client-host-corp source the `"*"` row does not take);
    any other `FidelityError` is a table, allowlist or destination refusal.
    """
    if not isinstance(source_repo, str) or not _REPOSITORY_NAME.fullmatch(source_repo):
        raise FidelityError(
            f"held source repo {source_repo!r} is not a bare repository name"
        )
    routing = load_artifact_routing()
    host = machine if machine is not None else machine_identity()
    client_corp_origin: str | None = None
    rows = _held_rows(routing)
    modes = _store_modes(rows, host, routing["source"])
    for row in rows:
        if row["machine"] not in ("*", host):
            continue
        if row["source_repo"] == "*":
            if client_corp_origin is not None:
                continue
            root = known_project_roots().get(source_repo)
            if root is None:
                raise FidelityError(
                    f"held source repo {source_repo!r} is not named in the fleet "
                    'project registry, so its class cannot be measured and the "*" '
                    f"row in {routing['source']} cannot route it"
                )
            url = _origin_url(source_repo, root, "held source")
            if _CLIENT_CORP_ORIGIN.search(url):
                client_corp_origin = url
                continue
        elif row["source_repo"] != source_repo:
            continue
        mode = modes[row["destination"]]
        store = held_store(row["destination"], land=mode["land"], branch=mode["branch"])
        return HeldRoute(
            source_repo=source_repo,
            matched_machine=row["machine"],
            matched_source=row["source_repo"],
            store=check_held_store(store),
        )
    if client_corp_origin is not None:
        raise HeldRouteUnconfigured(
            f"held source repo {source_repo!r} is a example-client-host-corp repository (origin "
            f"{client_corp_origin}) and no [[held_routes]] row in {routing['source']} "
            f'names it; the "*" row routes personal repos only'
        )
    raise HeldRouteUnconfigured(
        f"no [[held_routes]] row in {routing['source']} routes source repo "
        f"{source_repo!r} on machine {host!r}"
    )


def validate_root_from_carrier(carrier: bytes, expected_digest: str) -> dict[str, Any]:
    """Validate a carrier against the root its own ``repository`` field resolves to.

    The same byte-for-byte checks as ``validate_root`` -- content digest, blob oid
    and entry ids against the committed record -- plus two bounds the explicit-root
    form cannot apply: the repository must be allowlisted, and the record must live
    under the configured ask-record directory.
    """
    root = parse_canonical_root(carrier)
    repo = resolve_repository_root(root["repository"])
    relative = Path(root["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise FidelityError("root path is not repository-relative")
    allowed = [Path(d) for d in ask_record_dirs_for(root["repository"])]
    if not any(relative.parts[: len(d.parts)] == d.parts for d in allowed):
        raise FidelityError(
            f"root path {root['path']!r} is outside "
            f"{', '.join(d.as_posix() + '/' for d in allowed)} in "
            f"{root['repository']}"
        )
    return validate_root(carrier, repo, expected_digest)


def validate_root(carrier: bytes, repo: Path, expected_digest: str) -> dict[str, Any]:
    root = parse_canonical_root(carrier)
    if sha256_bytes(carrier) != expected_digest:
        raise FidelityError("root founding_ask_ref_sha256 mismatch")
    relative = Path(root["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise FidelityError("root path is not repository-relative")
    try:
        content = _git(
            repo, "show", f"{root['capture_commit']}:{relative.as_posix()}", strip=False
        )
    except FidelityError as exc:
        raise FidelityError("root path/capture_commit mismatch") from exc
    if sha256_bytes(content) != root["content_sha256"]:
        raise FidelityError("root content_sha256 mismatch")
    blob = _git(
        repo, "rev-parse", f"{root['capture_commit']}:{relative.as_posix()}"
    ).decode()
    if blob != root["blob_oid"]:
        raise FidelityError("root blob_oid mismatch")
    text = content.decode("utf-8")
    actual_entries = re.findall(r"^entry_id:\s*(A[0-9]{3,})\s*$", text, re.MULTILINE)
    if actual_entries != root["entry_ids"]:
        raise FidelityError("root entry_ids mismatch")
    return root


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise FidelityError(f"intent_alignment {name} has a closed-schema violation")
    return value


def validate_alignment_receipt(  # noqa: C901, PLR0912, PLR0915
    receipt: Any,
) -> dict[str, Any]:
    receipt = _closed(receipt, RECEIPT_FIELDS, "receipt")
    if receipt["schema_version"] != 1:
        raise FidelityError("intent_alignment schema_version is unsupported")
    for field in ("run_id", "stage", "transition"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise FidelityError(f"intent_alignment {field} must be non-empty")
    if not HEX64.fullmatch(receipt["founding_ask_ref_sha256"]):
        raise FidelityError("intent_alignment founding_ask_ref_sha256 is invalid")
    subject = _closed(receipt["subject"], {"artifact", "next_brief"}, "subject")
    artifact = subject["artifact"]
    if isinstance(artifact, dict) and artifact.get("state") == "present":
        _closed(artifact, {"state", "ref", "sha256"}, "artifact")
        if not artifact["ref"] or not HEX64.fullmatch(artifact["sha256"]):
            raise FidelityError("intent_alignment artifact is invalid")
    else:
        _closed(artifact, {"state"}, "artifact")
        if artifact["state"] != "absent":
            raise FidelityError("intent_alignment artifact state is invalid")
    brief = subject["next_brief"]
    if isinstance(brief, dict) and brief.get("state") == "present":
        _closed(brief, {"state", "sha256"}, "next_brief")
        if not HEX64.fullmatch(brief["sha256"]):
            raise FidelityError("intent_alignment next_brief sha256 is invalid")
    else:
        _closed(brief, {"state", "reason"}, "next_brief")
        if brief["state"] != "absent" or brief["reason"] != "no-successor":
            raise FidelityError("intent_alignment next_brief absent branch is invalid")
    reviewer = _closed(
        receipt["reviewer"], {"family", "model", "effort", "persona"}, "reviewer"
    )
    if not all(isinstance(value, str) and value for value in reviewer.values()):
        raise FidelityError("intent_alignment reviewer is invalid")
    if not isinstance(receipt["requirements"], list) or not receipt["requirements"]:
        raise FidelityError("intent_alignment requirements must be non-empty")
    for item in receipt["requirements"]:
        item = _closed(
            item,
            {
                "requirement_id",
                "source_entry",
                "disposition",
                "applicability",
                "rationale",
                "evidence",
                "authority_ref",
            },
            "requirement",
        )
        if (
            item["disposition"] not in DISPOSITIONS
            or item["applicability"] not in APPLICABILITY
        ):
            raise FidelityError("intent_alignment requirement enum is invalid")
        for field in ("requirement_id", "source_entry", "rationale"):
            if not isinstance(item[field], str) or not item[field]:
                raise FidelityError(f"intent_alignment requirement {field} is invalid")
        if item["authority_ref"] is not None and (
            not isinstance(item["authority_ref"], str) or not item["authority_ref"]
        ):
            raise FidelityError("intent_alignment requirement authority_ref is invalid")
        _validate_evidence(item["evidence"], "requirement evidence")
    if not isinstance(receipt["scope_changes"], list):
        raise FidelityError("intent_alignment scope_changes must be a list")
    for change in receipt["scope_changes"]:
        change = _closed(
            change, {"kind", "description", "evidence", "authority_ref"}, "scope_change"
        )
        if (
            change["kind"] not in {"NARROWING", "BROADENING", "DEFERRAL", "REJECTION"}
            or not isinstance(change["description"], str)
            or not change["description"]
            or not isinstance(change["authority_ref"], str)
            or not change["authority_ref"]
        ):
            raise FidelityError("intent_alignment scope_change is invalid")
        _validate_evidence(change["evidence"], "scope_change evidence")
    _validate_evidence(receipt["evidence"], "receipt evidence")
    if receipt["verdict"] not in {"ALIGNED", "MISMATCH", "UNVERIFIED"}:
        raise FidelityError("intent_alignment verdict is invalid")
    if receipt["confidence"] not in {"high", "medium", "low"}:
        raise FidelityError("intent_alignment confidence is invalid")
    if not isinstance(receipt["blocking"], bool):
        raise FidelityError("intent_alignment blocking must be boolean")
    if receipt["verdict"] == "ALIGNED" and receipt["blocking"]:
        raise FidelityError("ALIGNED intent_alignment cannot be blocking")
    if receipt["verdict"] != "ALIGNED" and not receipt["blocking"]:
        raise FidelityError("MISMATCH/UNVERIFIED intent_alignment must be blocking")
    return receipt


def _validate_evidence(items: Any, name: str) -> None:
    if not isinstance(items, list) or not items:
        raise FidelityError(f"intent_alignment {name} must be non-empty")
    for evidence in items:
        evidence = _closed(evidence, {"kind", "value", "source"}, "evidence")
        if evidence["kind"] not in {"citation", "command"} or not all(
            isinstance(evidence[key], str) and evidence[key]
            for key in ("value", "source")
        ):
            raise FidelityError("intent_alignment evidence is invalid")


# ISSUE-REF. An explicit founding-ask requirement is narrowed only by the human's own words,
# so an ALIGNED receipt that narrows one must cite the FAR entry that did it. Before this, any
# non-empty string passed, and the measured receipts cited AI decisions ("D-4", "OQ-6") as the
# authority for narrowing what the human had asked for.
NARROWING_DISPOSITIONS = frozenset({"DROPPED", "NARROWED", "REJECTED"})
NARROWING_SCOPE_KINDS = frozenset({"NARROWING", "REJECTION"})
FAR_AUTHORITY = re.compile(r"^far:(A[0-9]{3,})$")


def ai_authority_narrowings(
    receipt: dict[str, Any], human_entry_ids: Collection[str] | None = None
) -> list[str]:
    """Name every narrowing an ALIGNED receipt rests on something other than a human FAR entry."""
    if receipt["verdict"] != "ALIGNED":
        return []
    cited = [
        (
            f"requirement {item['requirement_id']} {item['disposition']}",
            item["authority_ref"],
        )
        for item in receipt["requirements"]
        if item["disposition"] in NARROWING_DISPOSITIONS
    ] + [
        (f"scope change {change['kind']}", change["authority_ref"])
        for change in receipt["scope_changes"]
        if change["kind"] in NARROWING_SCOPE_KINDS
    ]
    refused = []
    for subject, authority in cited:
        match = FAR_AUTHORITY.fullmatch(authority or "")
        if match is None or (
            human_entry_ids is not None and match.group(1) not in human_entry_ids
        ):
            refused.append(f"{subject} on authority {authority!r}")
    return refused


def _collapsed(text: str) -> str:
    return " ".join(text.split()).casefold()


def uncovered_excerpts(receipt: dict[str, Any], excerpts: Sequence[str]) -> list[str]:
    """Return each carried excerpt no requirement quotes in its rationale or evidence."""
    quoted = [
        _collapsed(
            " ".join([item["rationale"], *(e["value"] for e in item["evidence"])])
        )
        for item in receipt["requirements"]
    ]
    return [
        excerpt
        for excerpt in excerpts
        if not any(_collapsed(excerpt) in text for text in quoted)
    ]


def _founding_ask_refusal(
    receipt: dict[str, Any],
    human_entry_ids: Collection[str] | None,
    excerpts: Sequence[str] | None,
) -> str | None:
    narrowed = ai_authority_narrowings(receipt, human_entry_ids)
    if narrowed:
        return (
            "ALIGNED intent_alignment narrows an explicit founding-ask requirement without the "
            f"human's authority ({'; '.join(narrowed)}); only a human FAR entry, cited as "
            "far:<entry_id>, narrows one. Keep the requirement or escalate it"
        )
    missing = uncovered_excerpts(receipt, excerpts or ())
    if missing:
        return (
            "intent_alignment silently drops founding-ask excerpt(s): "
            + "; ".join(repr(excerpt[:120]) for excerpt in missing)
            + ". Quote each carried excerpt in a requirement's rationale or evidence"
        )
    return None


def carrier_excerpts(prompt: str) -> list[str]:
    """The verbatim excerpts a Tier-2/3 carrier binds, in order."""
    block = re.search(
        r"^relevant_verbatim_excerpts_incomplete:\n(.*?)(?=^full_verified_record:|^## |\Z)",
        prompt,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        return []
    return re.findall(
        r"^```text\n(.*?)\n```$", block.group(1), re.MULTILINE | re.DOTALL
    )


def carrier_founding_ask(prompt: str) -> tuple[str, list[str]] | None:
    """Return the bound FAR's committed text and entry ids, or None without a root.

    A validated Tier-2/3 carrier always carries a root, so None means the prompt carries no
    founding ask to compare against. A root that is present but cannot be read raises.
    """
    root_match = re.search(
        r"^canonical_root_json:\s*(\{.*\})\s*$", prompt, re.MULTILINE
    )
    digest_match = re.search(
        r"^founding_ask_ref_sha256:\s*([0-9a-f]{64})\s*$", prompt, re.MULTILINE
    )
    if root_match is None or digest_match is None:
        return None
    root = validate_root_from_carrier(
        root_match.group(1).encode(), digest_match.group(1)
    )
    repo = resolve_repository_root(root["repository"])
    content = _git(
        repo, "show", f"{root['capture_commit']}:{root['path']}", strip=False
    )
    return content.decode("utf-8"), list(root["entry_ids"])


def validate_promotion(  # noqa: PLR0913 - the two founding-ask inputs are optional
    receipt: Any,
    *,
    root_digest: str,
    transition: str,
    artifact_sha256: str | None,
    next_brief_sha256: str | None,
    human_entry_ids: Collection[str] | None = None,
    excerpts: Sequence[str] | None = None,
) -> None:
    receipt = validate_alignment_receipt(receipt)
    if receipt["founding_ask_ref_sha256"] != root_digest:
        raise FidelityError("intent_alignment root binding is stale")
    if receipt["transition"] != transition:
        raise FidelityError("intent_alignment transition binding is stale")
    artifact = receipt["subject"]["artifact"]
    expected_artifact = (
        artifact.get("sha256") if artifact["state"] == "present" else None
    )
    brief = receipt["subject"]["next_brief"]
    expected_brief = brief.get("sha256") if brief["state"] == "present" else None
    if expected_artifact != artifact_sha256:
        raise FidelityError("intent_alignment artifact binding is stale")
    if expected_brief != next_brief_sha256:
        raise FidelityError("intent_alignment next_brief binding is stale")
    if receipt["blocking"] or receipt["verdict"] != "ALIGNED":
        raise FidelityError("intent_alignment receipt blocks promotion")
    refusal = _founding_ask_refusal(receipt, human_entry_ids, excerpts)
    if refusal is not None:
        raise FidelityError(refusal)


def promotion_mismatch_subject(  # noqa: PLR0913 - mirrors validate_promotion
    receipt: Any,
    *,
    root_digest: str,
    transition: str,
    artifact_sha256: str | None,
    next_brief_sha256: str | None,
    human_entry_ids: Collection[str] | None = None,
    excerpts: Sequence[str] | None = None,  # noqa: ARG001 - an omission is unverified
) -> str:
    """Classify a rejected boundary result for its deterministic recovery path."""
    try:
        receipt = validate_alignment_receipt(receipt)
    except FidelityError:
        return "unverified"
    if (
        receipt["founding_ask_ref_sha256"] != root_digest
        or receipt["transition"] != transition
    ):
        return "unverified"
    artifact = receipt["subject"]["artifact"]
    brief = receipt["subject"]["next_brief"]
    artifact_mismatch = (
        artifact.get("sha256") if artifact["state"] == "present" else None
    ) != artifact_sha256
    brief_mismatch = (
        brief.get("sha256") if brief["state"] == "present" else None
    ) != next_brief_sha256
    if artifact_mismatch and brief_mismatch:
        return "both"
    if artifact_mismatch:
        return "artifact"
    if brief_mismatch:
        return "brief"
    actionable_mismatch = receipt["verdict"] == "MISMATCH" and (
        any(item["disposition"] != "SATISFIED" for item in receipt["requirements"])
        or receipt["scope_changes"]
    )
    # An ALIGNED receipt narrowing a requirement on AI authority is a semantic mismatch
    # dressed as alignment (ISSUE-REF), so it routes to the backlog the same way.
    ai_narrowing = bool(ai_authority_narrowings(receipt, human_entry_ids))
    return "semantic" if actionable_mismatch or ai_narrowing else "unverified"


def render_brief_carrier(  # noqa: C901, PLR0912, PLR0913
    *,
    tier: int,
    stage_framing: str,
    exact_ask: bytes | None = None,
    root_carrier: bytes | None = None,
    root_digest: str | None = None,
    resolved_path: Path | None = None,
    excerpts: list[bytes] | None = None,
    alignment_obligation: str = "",
    full_record_fallback: bytes | None = None,
    parent_root_carrier: bytes | None = None,
    tier_zero_reason: str | None = None,
    repo_root: Path | None = None,
) -> str:
    """Render the physical carrier blocks placed ahead of stage framing."""
    if tier not in {0, 1, 2, 3}:
        raise FidelityError("fidelity tier must be 0, 1, 2, or 3")
    if not stage_framing.strip():
        raise FidelityError("stage framing must be non-empty")
    if tier == 0:
        if not tier_zero_reason:
            raise FidelityError("Tier 0 requires an explicit no-originating-ask reason")
        ask_block = f"founding_ask: N/A\nreason: {tier_zero_reason}"
    elif tier == 1:
        if exact_ask is None:
            raise FidelityError("Tier 1 requires exact load-bearing ask bytes")
        ask_block = (
            "verbatim_exact_ask:\n```text\n" + exact_ask.decode("utf-8") + "\n```"
        )
    else:
        if root_carrier is None or root_digest is None:
            raise FidelityError("Tier 2/3 requires a canonical root carrier and digest")
        if repo_root is None:
            # Resolved from the carrier's own repository rather than refused: the
            # record lives where the fleet routes it, which an explicit caller may
            # not know. Resolution is allowlisted and raises when it cannot close.
            root = validate_root_from_carrier(root_carrier, root_digest)
            repo_root = resolve_repository_root(root["repository"])
        else:
            root = validate_root(root_carrier, repo_root, root_digest)
        if parent_root_carrier is not None and parent_root_carrier != root_carrier:
            raise FidelityError(
                "child root differs from parent without new human input"
            )
        committed_content = _git(
            repo_root,
            "show",
            f"{root['capture_commit']}:{root['path']}",
            strip=False,
        )
        readable = (
            resolved_path is not None
            and resolved_path.is_file()
            and sha256_bytes(resolved_path.read_bytes()) == root["content_sha256"]
        )
        if not readable:
            if full_record_fallback is None:
                raise FidelityError(
                    "founding ask record is unreadable and no full fallback exists"
                )
            if sha256_bytes(full_record_fallback) != root["content_sha256"]:
                raise FidelityError("full record fallback content_sha256 mismatch")
        if any(excerpt not in committed_content for excerpt in (excerpts or [])):
            raise FidelityError("verbatim excerpt is not present in the committed FAR")
        excerpt_text = "\n".join(
            "```text\n" + item.decode("utf-8") + "\n```" for item in (excerpts or [])
        )
        ask_block = (
            "canonical_root_json: " + root_carrier.decode("utf-8") + "\n"
            f"founding_ask_ref_sha256: {root_digest}\n"
            f"resolved_record_path: {resolved_path if readable else 'unreadable; verified inline fallback follows'}\n"
            "relevant_verbatim_excerpts_incomplete:\n" + excerpt_text
        )
        if not readable:
            ask_block += (
                "\nfull_verified_record:\n```text\n"
                + full_record_fallback.decode("utf-8")
                + "\n```"
            )
        if not alignment_obligation.strip():
            raise FidelityError("Tier 2/3 requires an alignment obligation")
    return (
        f"## FOUNDING ASK — fidelity tier {tier}\n\n{ask_block}\n\n"
        f"## STAGE FRAMING — non-authoritative\n\n{stage_framing}\n\n"
        "## ALIGNMENT OBLIGATION\n\n"
        + (
            alignment_obligation
            if tier >= 2  # noqa: PLR2004
            else "Preserve the exact ask and surface any conflict."
        )
        # Emitted unconditionally and last, so no caller of the builder can forget it and
        # no brief author has to remember it.  Last because the floor overrides the stage
        # framing it follows, and a bound stated before the work it bounds reads as
        # background rather than as a constraint.
        + f"\n\n{DESTRUCTIVE_OPERATION_FLOOR}"
    )


def validate_destructive_operation_floor(prompt: str) -> None:
    """Refuse a launch prompt that does not carry the destructive-operation floor.

    Checked for every fidelity tier, including Tier 0: whether an originating human
    ask exists has no bearing on whether the launchee may pattern-kill a process.
    """
    if DESTRUCTIVE_OPERATION_FLOOR_HEADING not in prompt:
        raise FidelityError(
            "prompt lacks the destructive-operation floor. Required heading "
            f"(pure ASCII, no em dash): '{DESTRUCTIVE_OPERATION_FLOOR_HEADING}'. "
            "render_brief_carrier() emits it automatically; a hand-written brief must "
            "paste it verbatim. The canonical block follows:\n\n"
            + DESTRUCTIVE_OPERATION_FLOOR
        )
    haystack = prompt.casefold()
    missing = [
        token
        for token in DESTRUCTIVE_OPERATION_FLOOR_TOKENS
        if token.casefold() not in haystack
    ]
    if missing:
        raise FidelityError(
            "destructive-operation floor is present but incomplete; missing "
            f"required bounds: {', '.join(missing)}. Paste the canonical block "
            "verbatim rather than summarising it:\n\n" + DESTRUCTIVE_OPERATION_FLOOR
        )


def validate_launch_prompt(  # noqa: C901, PLR0912
    prompt: str,
    *,
    repo_root: Path | None = None,  # noqa: ARG001 - retained for caller compatibility
) -> int:
    """Validate a live cx/Agent prompt's physical carrier and return its tier.

    ``repo_root`` is accepted for caller compatibility and is NO LONGER the
    validation root for a Tier 2/3 carrier: every live launcher passed its own
    cwd, which refused any carrier whose record the fleet routed to another
    repository.  The root is resolved from the carrier's allowlisted
    ``repository`` field instead -- see ``validate_root_from_carrier``.
    """
    matches = re.findall(
        r"^## FOUNDING ASK — fidelity tier ([0-3])\s*$", prompt, re.MULTILINE
    )
    if not matches:
        raise FidelityError(
            "prompt lacks canonical FOUNDING ASK fidelity-tier block. "
            "Required heading format: '## FOUNDING ASK — fidelity tier N' where the dash "
            "is U+2014 EM DASH (—), not an ASCII hyphen (-). "
            "See render_brief_carrier() in this module for correct formatting."
        )
    if len(matches) != 1:
        raise FidelityError(
            "prompt must contain exactly one FOUNDING ASK fidelity-tier block"
        )
    tier = int(matches[0])
    if tier == 0:
        if not re.search(
            r"^founding_ask:\s*N/A\s*$", prompt, re.MULTILINE
        ) or not re.search(r"^reason:\s*\S.*$", prompt, re.MULTILINE):
            raise FidelityError(
                "Tier 0 prompt requires founding_ask: N/A and an explicit reason"
            )
    elif tier == 1:
        exact = re.search(
            r"^verbatim_exact_ask:\s*\n```text\n(.+?)\n```\s*$",
            prompt,
            re.MULTILINE | re.DOTALL,
        )
        if not exact or not exact.group(1):
            raise FidelityError(
                "Tier 1 prompt requires exact load-bearing wording. "
                "Expected format: 'verbatim_exact_ask:' followed by a ```text fence "
                "containing non-empty content. See render_brief_carrier() for correct formatting."
            )
        for label in (
            "## STAGE FRAMING — non-authoritative",
            "## ALIGNMENT OBLIGATION",
        ):
            if label not in prompt:
                raise FidelityError(f"Tier 1 prompt lacks {label}")
    else:
        root_match = re.search(
            r"^canonical_root_json:\s*(\{.*\})\s*$", prompt, re.MULTILINE
        )
        digest_match = re.search(
            r"^founding_ask_ref_sha256:\s*([0-9a-f]{64})\s*$", prompt, re.MULTILINE
        )
        if not root_match or not digest_match:
            raise FidelityError(
                "Tier 2/3 prompt lacks canonical root JSON or digest. "
                "Required fields: 'canonical_root_json: {json object}' and "
                "'founding_ask_ref_sha256: <64 hex characters>'. "
                "See render_brief_carrier() for correct formatting."
            )
        carrier = root_match.group(1).encode("utf-8")
        # The launcher's cwd is NOT the validation root. A delegating session
        # stands in the repo it is changing, while the founding ask record is
        # committed wherever the fleet routes records for that session -- so the
        # root comes from the carrier's own allowlisted repository field.
        validate_root_from_carrier(carrier, digest_match.group(1))
        for label in (
            "## STAGE FRAMING — non-authoritative",
            "## ALIGNMENT OBLIGATION",
        ):
            if label not in prompt:
                raise FidelityError(f"Tier 2/3 prompt lacks {label}")
    # Checked last, after every tier-specific branch, so a carrier defect still reports
    # as the carrier defect it is.  Checking the floor first would have replaced the
    # actionable "the dash must be U+2014" message from ISSUE-REF with a floor
    # complaint, which diagnoses the wrong fault.
    validate_destructive_operation_floor(prompt)
    return tier
