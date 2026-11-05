"""The GitHub Pages site builds only what it is meant to build.

Pages builds the public repository from main, root, with GitHub's Jekyll. Jekyll turns any file
that starts with front matter into a page and runs Liquid over it, so a stray document with
`{{` in it can fail the build or render wrong. The site therefore excludes every directory,
and these tests fail when a new top-level directory or a front-matter file appears that the
configuration does not account for.

The same tests run in two places. In the private source tree the site files sit in
export-docs/site and the README in export-docs; they check those sources. In the generated
public tree they sit at the root, and the tests also check the real top-level listing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = (ROOT / "_config.yml").is_file()
SITE = ROOT if PUBLIC else ROOT / "export-docs" / "site"
README = ROOT / "README.md" if PUBLIC else ROOT / "export-docs" / "README.md"

# The one external script the layout may load, exactly as pinned.
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
MERMAID_SRI = "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I"

# Top-level entries that Jekyll builds.
BUILT = {"index.md", "README.md", "_config.yml", "_layouts"}


def _exclude_list() -> list[str]:
    """Read the `exclude:` sequence of _config.yml without a YAML dependency."""
    items: list[str] = []
    inside = False
    for line in (SITE / "_config.yml").read_text(encoding="utf-8").splitlines():
        if re.match(r"^exclude:\s*$", line):
            inside = True
            continue
        if inside:
            match = re.match(r"^  - (\S+)\s*$", line)
            if match:
                items.append(match.group(1))
            elif line.strip() and not line.startswith("#"):
                break
    return items


def test_given_the_site_sources_then_all_three_exist() -> None:
    for name in ("_config.yml", "index.md", "_layouts/default.html"):
        assert (SITE / name).is_file(), name


def test_given_the_readme_then_it_has_no_liquid_syntax() -> None:
    text = README.read_text(encoding="utf-8")
    assert "{{" not in text and "{%" not in text, (
        "index.md includes the README through Liquid; `{{` or `{%` in it would be run as "
        "Liquid. Wrap the text in a raw block or reword it."
    )


def test_given_the_readme_then_it_has_no_front_matter() -> None:
    # GitHub shows front matter as a table on the repository page.
    assert not README.read_text(encoding="utf-8").startswith("---")


def test_given_the_index_page_then_it_includes_only_the_readme() -> None:
    text = (SITE / "index.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    tags = re.findall(r"\{%.*?%\}", text)
    assert tags == ["{% include_relative README.md %}"], tags
    assert "{{" not in text


def test_given_the_layout_then_it_loads_one_pinned_script_with_integrity() -> None:
    html = (SITE / "_layouts" / "default.html").read_text(encoding="utf-8")
    sources = re.findall(r"<script[^>]*\ssrc=\"([^\"]+)\"", html)
    assert sources == [MERMAID_URL]
    tag = re.search(r"<script[^>]*\ssrc=\"[^\"]+\"[^>]*>", html)
    assert tag is not None
    assert f'integrity="{MERMAID_SRI}"' in tag.group(0)
    assert 'crossorigin="anonymous"' in tag.group(0)


def test_given_the_layout_then_it_references_no_other_external_resource() -> None:
    html = (SITE / "_layouts" / "default.html").read_text(encoding="utf-8")
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    assert urls == {MERMAID_URL}, urls
    assert "@import" not in html and "<link" not in html and "<iframe" not in html


def test_given_the_config_then_every_excluded_name_is_a_plain_top_level_name() -> None:
    excluded = _exclude_list()
    assert excluded, "the exclude list is empty or unreadable"
    for name in excluded:
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
        assert name not in BUILT, (
            f"{name} is built by the site and must not be excluded"
        )


@pytest.mark.skipif(
    not PUBLIC, reason="the top-level listing is only checked in the public tree"
)
def test_given_the_public_tree_then_every_top_level_directory_is_excluded() -> None:
    excluded = set(_exclude_list())
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith((".", "_")) or not entry.is_dir():
            continue
        assert entry.name in excluded, (
            f"top-level directory {entry.name}/ would be processed by Jekyll; "
            "add it to the exclude list in _config.yml"
        )


@pytest.mark.skipif(
    not PUBLIC, reason="the top-level listing is only checked in the public tree"
)
def test_given_the_public_tree_then_only_the_index_page_has_front_matter() -> None:
    excluded = set(_exclude_list())
    for entry in sorted(ROOT.iterdir()):
        if entry.name.startswith((".", "_")) or not entry.is_file():
            continue
        if entry.name in excluded or entry.name == "index.md":
            continue
        head = entry.read_bytes()[:4]
        assert head != b"---\n", (
            f"{entry.name} starts with front matter and would become a page"
        )
