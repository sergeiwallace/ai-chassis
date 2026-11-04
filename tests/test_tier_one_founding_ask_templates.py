"""Regression coverage for Tier 1 founding-ask durability in document stubs."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOC_KINDS = (
    "analysis",
    "audit",
    "bug",
    "design",
    "docket",
    "handoff",
    "plan",
    "procedure",
    "research",
    "spec",
    "test",
    "tool",
)
DOC_DIRECTORIES = {
    "analysis": "analysis",
    "audit": "audits",
    "bug": "bugs",
    "design": "designs",
    "docket": "docket",
    "handoff": "handoffs",
    "plan": "plans",
    "procedure": "procedures",
    "research": "research",
    "spec": "specs",
    "test": "test",
    "tool": "tools",
}
TIER_ONE_INSTRUCTION = (
    "When this `founding_ask_ref` is `N/A` for a Tier 1 dispatch with no "
    "`canonical_root_json`, paste the literal `verbatim_exact_ask` from the worker "
    "brief directly below in a fenced `text` block. Never replace it with a summary "
    "or a reference to the transient brief. Do not add this block for Tier 0 or Tier 2/3."
)


def _template_paths(kind: str) -> tuple[Path, ...]:
    directory = ROOT / "docs" / DOC_DIRECTORIES[kind]
    paths = [directory / "STUB.md.jinja"]
    direct_copy = directory / "STUB.md"
    if direct_copy.is_file():
        paths.append(direct_copy)
    return tuple(paths)


@pytest.mark.parametrize("kind", DOC_KINDS)
def test_document_stubs_require_durable_tier_one_ask(kind: str) -> None:
    for template in _template_paths(kind):
        assert TIER_ONE_INSTRUCTION in template.read_text(encoding="utf-8"), template
