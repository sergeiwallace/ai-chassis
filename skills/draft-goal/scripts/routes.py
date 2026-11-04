"""Read-only model routes for the structured draft-goal worker contract."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from pathlib import Path
from typing import Any

# These consumers are also loaded directly by file path outside the CLI.
# Resolve their sibling helper without relying on import order in the caller.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from line_endings import cm_lines

PROMPT_ARG_LIMIT = 24_000

# Loaded by path, matching skills/auto-flow/scripts/flow_resolve.py: the carrier module
# lives in a hyphenated directory that is not importable as a package.  The
# destructive-operation floor comes from that one definition rather than a second copy,
# so the text this builder emits cannot drift from the text the launch gate requires.
_ASK_FIDELITY_PATH = (
    Path(__file__).resolve().parents[2] / "_shared" / "ask-fidelity" / "ask_fidelity.py"
)
_ASK_SPEC = importlib.util.spec_from_file_location(
    "draft_goal_ask_fidelity", _ASK_FIDELITY_PATH
)
if _ASK_SPEC is None or _ASK_SPEC.loader is None:
    raise RuntimeError("ask-fidelity validator is unavailable")
ask_fidelity = importlib.util.module_from_spec(_ASK_SPEC)
sys.modules[_ASK_SPEC.name] = ask_fidelity
_ASK_SPEC.loader.exec_module(ask_fidelity)


class RouteError(ValueError):
    """A routed worker failed or returned a non-JSON result."""


def _worker_prompt(  # noqa: PLR0913
    ask: str,
    *,
    kind: str,
    cadence_seconds: int | None,
    brief_path: Path,
    schema_path: Path,
    retry_diagnostic: str | None,
) -> str:
    brief = brief_path.read_text(encoding="utf-8")
    retry = (
        "\nThe prior response failed caller-side validation. Correct every issue below, then "
        f"return a complete replacement object:\n{retry_diagnostic}\n"
        if retry_diagnostic
        else ""
    )
    return f"""## FOUNDING ASK — fidelity tier 1

verbatim_exact_ask:
```text
{ask}
```

## STAGE FRAMING — non-authoritative

Compile the exact ask through capture, fit classification, one-outcome decomposition, evidence
binding, autonomy preflight, and coverage self-audit. Requested kind: {kind}. Resolved cadence
seconds: {json.dumps(cadence_seconds)}. Preserve exclusions and authority limits. Reject one-shot
or judgment-dependent work. Ask at most one focused question rather than inventing authority,
credentials, or a verifier.

## ALIGNMENT OBLIGATION

The verbatim ask governs. Return JSON only, conforming exactly to {schema_path}. Do not write files,
invoke a standing goal/loop, or delegate. Every member is one coherent outcome; every success item
has a non-self verifier, evidence, and authority; every raw scope element has an explicit coverage
placement or `unplaced`.

Keep every captured exclusion in `scope_authority.exclusions`. Never copy an exclusion into a
member outcome, in-scope text, or success predicate; those fields contain affirmative elements
only.

## WORKER BRIEF

{brief}
{retry}
{ask_fidelity.DESTRUCTIVE_OPERATION_FLOOR}"""


def worker_argv(  # noqa: PLR0913
    *,
    route: str,
    prompt: str,
    schema_path: Path,
    repo_root: Path,
    model: str,
    effort: str,
) -> list[str]:
    """Return a closed argv for a read-only structured-output worker."""
    if route == "codex":
        return [
            str(Path.home() / ".claude/bin/cx"),
            "architect",
            "-C",
            str(repo_root),
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--json",
            "-m",
            model,
            "--effort",
            effort,
            prompt,
        ]
    if route == "claude":
        schema = schema_path.read_text(encoding="utf-8")
        return [
            "claude",
            "-p",
            "--model",
            model,
            "--effort",
            effort,
            "--permission-mode",
            "plan",
            "--permission-prompts",
            "none",
            "--tools",
            "",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            prompt,
        ]
    raise RouteError(f"unsupported delegated route: {route}")


def _parse_codex_jsonl(stdout: str) -> dict[str, Any]:
    for line in reversed(cm_lines(stdout)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {}) if isinstance(event, dict) else {}
        if event.get("type") != "item.completed" or item.get("type") != "agent_message":
            continue
        try:
            payload = json.loads(item["text"])
        except (KeyError, json.JSONDecodeError, TypeError) as exc:
            raise RouteError(
                "Codex agent message did not contain structured JSON"
            ) from exc
        if isinstance(payload, dict):
            return payload
        raise RouteError("Codex structured agent message must be one JSON object")
    raise RouteError("Codex JSONL contained no structured agent message")


def _parse_response(route: str, stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        if route == "codex":
            return _parse_codex_jsonl(stdout)
        raise RouteError(f"{route} worker returned invalid JSON: {exc.msg}") from exc
    if route == "claude" and isinstance(payload, dict):
        if isinstance(payload.get("structured_output"), dict):
            payload = payload["structured_output"]
        elif isinstance(payload.get("result"), str):
            try:
                payload = json.loads(payload["result"])
            except json.JSONDecodeError as exc:
                raise RouteError(
                    "Claude result did not contain structured JSON"
                ) from exc
    if not isinstance(payload, dict):
        raise RouteError(f"{route} worker output must be one JSON object")
    return payload


# The model API's strict structured-output mode rejects these JSON Schema keywords, so the
# Codex route sends a copy without them. The caller-side validator keeps the original
# schema, which is where uniqueness is actually enforced.
STRICT_UNSUPPORTED_KEYWORDS = frozenset({"uniqueItems"})


def _without_unsupported(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _without_unsupported(value)
            for key, value in node.items()
            if key not in STRICT_UNSUPPORTED_KEYWORDS
        }
    if isinstance(node, list):
        return [_without_unsupported(item) for item in node]
    return node


def _write_strict_schema(schema_path: Path) -> Path:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", prefix="draft-goal-schema-", suffix=".json", delete=False
    ) as handle:
        json.dump(_without_unsupported(schema), handle)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)


def _codex_error_message(stdout: str) -> str | None:
    """The API error a failed Codex turn reports on stdout, which stderr never carries."""
    for line in reversed(cm_lines(stdout)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        error = event.get("error")
        message = (
            error.get("message") if isinstance(error, dict) else event.get("message")
        )
        if event.get("type") in {"turn.failed", "error"} and isinstance(message, str):
            return message
    return None


def dispatch_worker(  # noqa: PLR0913
    *,
    route: str,
    ask: str,
    kind: str,
    cadence_seconds: int | None,
    schema_path: Path,
    brief_path: Path,
    repo_root: Path,
    model: str,
    effort: str,
    retry_diagnostic: str | None = None,
) -> dict[str, Any]:
    """Run one real read-only worker and parse its structured result."""
    prompt = _worker_prompt(
        ask,
        kind=kind,
        cadence_seconds=cadence_seconds,
        brief_path=brief_path,
        schema_path=schema_path,
        retry_diagnostic=retry_diagnostic,
    )
    strict_schema: Path | None = None
    prompt_file: Path | None = None
    try:
        if route == "codex":
            strict_schema = _write_strict_schema(schema_path)
        argv = worker_argv(
            route=route,
            prompt=prompt,
            schema_path=strict_schema or schema_path,
            repo_root=repo_root,
            model=model,
            effort=effort,
        )
        if route == "codex" and len(prompt.encode("utf-8")) > PROMPT_ARG_LIMIT:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", prefix="draft-goal-prompt-", delete=False
            ) as handle:
                handle.write(prompt)
                handle.flush()
                os.fsync(handle.fileno())
                prompt_file = Path(handle.name)
            argv = argv[:-1] + ["--prompt-file", str(prompt_file)]
        completed = subprocess.run(  # noqa: S603 - argv is closed and never shell-evaluated
            argv,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RouteError(f"could not launch {route} worker: {exc}") from exc
    finally:
        if prompt_file:
            prompt_file.unlink(missing_ok=True)
        if strict_schema:
            strict_schema.unlink(missing_ok=True)
    if completed.returncode:
        detail = cm_lines(completed.stderr.strip())
        tail = (route == "codex" and _codex_error_message(completed.stdout)) or (
            detail[-1] if detail else "no diagnostic"
        )
        raise RouteError(
            f"{route} worker failed with exit {completed.returncode}: {tail}"
        )
    return _parse_response(route, completed.stdout.strip())
