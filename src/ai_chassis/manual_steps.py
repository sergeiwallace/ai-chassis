"""Manual steps for the session that ran the install (ISSUE-REF D-5).

An install step that cannot finish a job itself, or must leave one for a session to do by hand,
registers a ``ManualStep`` in a JSONL ledger. The ``manual-steps-report`` apply step prints the
whole ledger as one standardized section at the end of the install, and ``/install-harness`` § 6
performs every step in it. Python steps call ``register``; install.sh calls
``ai-harness manual-step add``.

Ledger: the explicit path in ``AI_HARNESS_MANUAL_STEPS_FILE``. install.sh creates one run-unique
file with ``mktemp`` before its first producer, exports it to every apply subprocess and shell
caller, and removes it after its terminal report. A process not under install.sh creates its own
on first use and owns its report and cleanup (``finish_owned_run``).

Every text field passes through ``sanitize`` before it is persisted or printed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import MutableMapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

LEDGER_ENV = "AI_HARNESS_MANUAL_STEPS_FILE"
#: The basename prefix of every run ledger; only a file carrying it is ever removed.
LEDGER_PREFIX = "ai-harness-manual-steps."
SECTION_OPEN = "== MANUAL STEPS FOR THIS SESSION ({count}) =="
SECTION_CLOSE = "== END MANUAL STEPS =="
REDACTED = "<redacted>"
REMOTE = "<remote>"

_owned: Path | None = None
_reported: Path | None = None

# A git remote in any of its absolute forms: ssh/git/file URLs, `user@host:path`, and an http(s)
# URL that names a repository (`.git`). A plain web page link is left alone.
_REMOTE_PATTERNS = (
    re.compile(r"\b(?:ssh|git|file)://\S+"),
    re.compile(r"\bhttps?://\S+?\.git\b/?\S*"),
    re.compile(r"(?<![\w/@.-])[\w.-]+@[\w.-]+:[\w./~-]+"),
)
_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE)
_SECRET_QUERY = re.compile(
    r"([?&](?:access_token|token|auth|key|api_key|apikey|secret|password|passwd|pwd|sig|"
    r"signature|x-amz-[a-z-]+)=)[^&\s]+",
    re.IGNORECASE,
)
_SECRET_ARGUMENT = re.compile(
    r"(--?(?:token|password|passwd|secret|api-key|apikey|auth|key|private-key)(?:=|\s+))"
    r"(\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY)[A-Z0-9_]*=)\S+"
)
#: The AWS CLI's own credential-file keys: lower case and ini-spaced (`aws_session_token = ...`),
#: which the upper-case assignment rule above cannot see.
_AWS_CREDENTIAL_KEY = re.compile(
    r"\b(aws_(?:access_key_id|secret_access_key|session_token|security_token)[ \t]*=[ \t]*)\S+",
    re.IGNORECASE,
)
#: `Bearer <value>`, in an Authorization header or alone, when the value is TOKEN-SHAPED: 8+
#: non-space characters holding a digit, one of `+/=_-` or an interior `.`, or 20+ characters of
#: any kind. Every documented shape qualifies (base64 bodies, `ABSK...`, JWTs); prose such as
#: "Bearer authentication is required" does not. A `.` counts only with a character after it, so
#: a word ending a sentence stays.
_BEARER = re.compile(
    r"\b(Bearer[ \t]+)(?=\S{8,})(?=\S*(?:[0-9+/=_-]|\.\S)|\S{20,})\S+", re.IGNORECASE
)
#: Amazon Bedrock API keys, base64 bodies included (`/`, `+`, `=`), which the generic rule below
#: stops at. Short-term keys start `bedrock-api-key-`: measured here, `.claude/bin/bedrock_auth.py`
#: asserts it of every key it mints. Long-term keys start `ABSK` (base64 of "BedrockAPIKey-"). Both
#: shapes are documented: the AWS Security Blog post "Securing Amazon Bedrock API keys: best
#: practices for implementation and management"
#: (https://aws.amazon.com/blogs/security/securing-amazon-bedrock-api-keys-best-practices-for-implementation-and-management/)
#: gives the long-term pattern as `ABSKQmVkcm9ja0FQSUtleS[A-Za-z0-9+\/]+={0,2}` and the short-term
#: key as `bedrock-api-key-` plus base64; the token generator's README
#: (https://github.com/aws/aws-bedrock-token-generator-python) has the short-term form as
#: `bedrock-api-key-` plus a base64 presigned URL plus `&Version=1`. The short-term arm takes a
#: token-shaped body only (20+ characters holding a digit or one of `+/=`), so a word such as
#: `bedrock-api-key-documentation` in a URL stays; `&` is allowed so a `&Version=1` tail goes
#: with the key. The long-term arm stays a superset of the documented `ABSKQmVkcm9ja0FQSUtleS`
#: pattern on purpose: a redactor that over-matches redacts, one that under-matches leaks.
_BEDROCK_API_KEY = re.compile(
    r"\b(?:bedrock-api-key-(?=[A-Za-z0-9+/=&_-]{20,})(?=[A-Za-z_&-]*[0-9+/=])"
    r"[A-Za-z0-9+/=&_-]+|ABSK[A-Za-z0-9+/]{16,}={0,2})"
)
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{64,})\b"
    # An unbroken run of 32+ letters and digits mixing upper case, lower case and digits: a
    # base64 or random secret. Paths, slugs and git SHAs carry separators or one case.
    r"|(?<![A-Za-z0-9+=])(?=[A-Za-z0-9+=]*\d)(?=[A-Za-z0-9+=]*[A-Z])(?=[A-Za-z0-9+=]*[a-z])"
    r"[A-Za-z0-9+]{32,}={0,2}(?![A-Za-z0-9+=])"
)


def sanitize(text: str) -> str:
    """Strip credentials and remote strings from a diagnostic or a rendered command.

    Order matters: userinfo and secret values go first, so a remote replaced afterwards cannot
    have carried one past the remote pattern.
    """
    text = _USERINFO.sub(r"\1", text)
    text = _SECRET_QUERY.sub(rf"\1{REDACTED}", text)
    text = _SECRET_ARGUMENT.sub(rf"\1{REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(rf"\1{REDACTED}", text)
    text = _AWS_CREDENTIAL_KEY.sub(rf"\1{REDACTED}", text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _BEDROCK_API_KEY.sub(REDACTED, text)
    for pattern in _REMOTE_PATTERNS:
        text = pattern.sub(REMOTE, text)
    return _TOKEN_SHAPES.sub(REDACTED, text)


def redact_secrets(text: str) -> str:
    """Replace every credential shape in ``text`` and leave its remotes readable.

    For a tool's own output printed verbatim, where a remote is the diagnosis (uv names the
    repository it could not read) and only a credential must not reach the terminal.
    """
    text = _USERINFO.sub(r"\1", text)
    text = _SECRET_QUERY.sub(rf"\1{REDACTED}", text)
    text = _SECRET_ARGUMENT.sub(rf"\1{REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(rf"\1{REDACTED}", text)
    text = _AWS_CREDENTIAL_KEY.sub(rf"\1{REDACTED}", text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _BEDROCK_API_KEY.sub(REDACTED, text)
    return _TOKEN_SHAPES.sub(REDACTED, text)


@dataclass(frozen=True)
class ManualStep:
    id: str
    title: str
    why: str
    commands: list[str] = field(default_factory=list)
    follow: str = ""
    source_step: str = ""
    registered_at: str = ""


def _sanitized(step: ManualStep) -> ManualStep:
    return replace(
        step,
        id=sanitize(step.id),
        title=sanitize(step.title),
        why=sanitize(step.why),
        commands=[sanitize(command) for command in step.commands],
        follow=sanitize(step.follow),
        source_step=sanitize(step.source_step),
        registered_at=sanitize(step.registered_at),
    )


def _valid_timestamp(value: str) -> bool:
    """Whether a caller-supplied stamp is the canonical UTC form stored by this module."""
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def ledger_path(environ: MutableMapping[str, str] = os.environ) -> Path:
    """This run's ledger; created and exported here when no caller passed one."""
    global _owned
    value = environ.get(LEDGER_ENV, "").strip()
    if value:
        return Path(value)
    handle, name = tempfile.mkstemp(prefix=LEDGER_PREFIX, suffix=".jsonl")
    os.close(handle)
    environ[LEDGER_ENV] = name
    _owned = Path(name)
    return _owned


def load(path: Path) -> list[ManualStep]:
    if not path.is_file():
        return []
    return [
        ManualStep(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def register(step: ManualStep, *, now: datetime | None = None) -> bool:
    """Append ``step`` to this run's ledger; False when a step with its id is already there."""
    path = ledger_path()
    stamped = _sanitized(step)
    if any(_sanitized(existing).id == stamped.id for existing in load(path)):
        return False
    if not _valid_timestamp(stamped.registered_at):
        stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stamped = replace(stamped, registered_at=stamp)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(stamped), sort_keys=True) + "\n")
    return True


def notice(step: ManualStep, path: Path) -> str:
    """The one line a registering step prints at once, so the step shows mid-run too."""
    step = _sanitized(step)
    return f"  [manual] {step.id}: {step.title} (listed under MANUAL STEPS at the end; {path})"


def render_report(steps: Sequence[ManualStep], ledger: Path) -> str:
    lines = [SECTION_OPEN.format(count=len(steps))]
    for number, step in enumerate(steps, start=1):
        step = _sanitized(step)
        lines.append(f"{number}. {step.id}: {step.title}")
        lines.append(f"   Why: {step.why}")
        for index, command in enumerate(step.commands):
            lines.append(("   Do:  " if index == 0 else "        ") + command)
        if step.follow:
            lines.append(f"   Then: {step.follow}")
    lines.append(SECTION_CLOSE)
    lines.append(f"ledger: {ledger}")
    return "\n".join(lines) + "\n"


def report(out: TextIO) -> None:
    """Print this run's section; the ``manual-steps-report`` step's whole behaviour."""
    global _reported
    path = ledger_path()
    out.write(render_report(load(path), path))
    _reported = path


def remove_run_ledger(path: Path) -> bool:
    """Remove ``path`` only when it is a regular file named like a run ledger."""
    if (
        not path.name.startswith(LEDGER_PREFIX)
        or path.is_symlink()
        or not path.is_file()
    ):
        return False
    path.unlink()
    return True


def finish_owned_run(
    out: TextIO, environ: MutableMapping[str, str] = os.environ
) -> None:
    """For a process not under install.sh: report what no report step printed, then clean up."""
    global _owned, _reported
    path, _owned = _owned, None
    if path is None:
        return
    if _reported != path and load(path):
        out.write(render_report(load(path), path))
    _reported = None
    remove_run_ledger(path)
    if environ.get(LEDGER_ENV) == str(path):
        del environ[LEDGER_ENV]
