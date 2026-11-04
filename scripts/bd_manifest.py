#!/usr/bin/env python3
"""Shared prefix-manifest helpers for portable fleet Beads tooling."""

from __future__ import annotations

import os
from pathlib import Path

# Where this machine's repos live. install.sh exports PROJECTS_DIR as the durable
# per-machine knob (ISSUE-REF) because the root is not always $HOME/projects — on the
# example-project SageMaker space $HOME is an ephemeral EBS volume and the repos sit on an EFS
# mount. Fall back to the historical default when the env var is unset.
DEFAULT_PROJECTS_ROOT = Path(os.environ.get("PROJECTS_DIR") or Path.home() / "projects")


def projects_root() -> Path:
    """The same answer as ``DEFAULT_PROJECTS_ROOT``, resolved at CALL time.

    The constant is frozen at import, which is fine for a module-level default and wrong
    for a tool that builds its ``--help`` default: an argparse default captured at import
    answers for the environment the module was loaded in.  Three tools answered this
    question with a bare ``~/projects`` literal instead (ISSUE-REF), which is silently an
    empty root on the example-project SageMaker space.
    """
    return Path(os.environ.get("PROJECTS_DIR") or Path.home() / "projects")


# The prefix manifest is a file inside THIS repo, so resolve it from this module's own
# location rather than via the projects root — that way it is found regardless of where
# the checkout lives or whether a sibling ai-harness exists under PROJECTS_DIR.
DEFAULT_PREFIX_MANIFEST = (
    Path(__file__).resolve().parent.parent / "config" / "bd_fleet_prefixes.yaml"
)


CEILING_KEY = "display_id_ceiling"

#: The manifest's two entry sections. ``fleet:`` maps a prefix to a REPO; ``homes:`` maps a
#: prefix to a Firstmate HOME — operational state outside any repository (D-13 (a), D-15 (a)).
FLEET_SECTION = "fleet"
HOMES_SECTION = "homes"


class MissingCeilingError(ValueError):
    """The manifest declares no ``display_id_ceiling``.

    A hard error rather than a built-in default, because the two readers of this
    policy are an ALLOCATOR and a REJECTING GATE. A defaulted ceiling would make
    an unreadable or renamed manifest look like a declared policy, so the gate
    would report clean while enforcing a number nobody wrote down — the same
    "guard that cannot fire" shape as ISSUE-REF itself, one file over.
    """


def _parse_manifest_section(text: str, section: str) -> list[dict[str, str]]:
    """Parse one named section of this manifest's deliberately tiny YAML subset.

    This is not a YAML implementation: it only accepts a named list of single-line
    ``- {key: value, key: value}`` mappings as used by
    ``config/bd_fleet_prefixes.yaml``.  ISSUE-REF requires this stdlib-only parser
    because Tier-B wrappers run under bare system ``python3``, where dev-only
    PyYAML is unavailable.

    **A section ENDS at the next top-level key** (ISSUE-REF). The original parser only
    ever switched its flag ON, which was invisible while ``fleet:`` was the manifest's
    only list and silently wrong the moment a second one was added: every ``homes:``
    row would have been appended to the ``fleet:`` result, so the prefix→repo bijection
    check would have validated entries with no ``repo`` key at all. Indentation is read
    from the RAW line because a top-level key and a nested one are indistinguishable
    after stripping.
    """
    entries: list[dict[str, str]] = []
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line[:1].isspace() and not line.startswith("- "):
            in_section = line == f"{section}:"
            continue
        if not in_section or not line.startswith("- {"):
            continue

        mapping, separator, remainder = line[2:].partition("}")
        trailing = remainder.strip()
        if not separator or (trailing and not trailing.startswith("#")):
            raise ValueError(f"malformed {section} manifest entry: {raw_line!r}")
        entry: dict[str, str] = {}
        for item in mapping[1:].split(","):
            key, separator, value = item.partition(":")
            if not separator or not key.strip() or not value.strip():
                raise ValueError(f"malformed {section} manifest entry: {raw_line!r}")
            entry[key.strip()] = value.strip()
        entries.append(entry)
    return entries


def _parse_prefix_manifest(text: str) -> list[dict[str, str]]:
    """The ``fleet:`` section of the manifest. Kept as a name because it is pinned by
    ``tests/test_bd_prefix_manifest.py``'s PyYAML-parity control."""
    return _parse_manifest_section(text, FLEET_SECTION)


def load_prefix_manifest(path: Path = DEFAULT_PREFIX_MANIFEST) -> list[dict]:
    """Load the non-empty ``fleet:`` list from the prefix manifest."""
    entries = _parse_prefix_manifest(Path(path).read_text())
    if not entries:
        raise ValueError(
            f"prefix manifest {path} declares zero repos — no non-empty 'fleet:' "
            "list found (check for a renamed/mistyped top-level key or an explicitly empty list)"
        )
    return entries


def load_home_manifest(path: Path = DEFAULT_PREFIX_MANIFEST) -> list[dict]:
    """Load the ``homes:`` list — prefix→Firstmate-home registrations (D-13 (a), D-15 (a)).

    A Firstmate home is operational state under ``$XDG_STATE_HOME/firstmate/...``, not a
    repository, so its row carries ``home`` (a logical, machine-independent NAME) where a
    ``fleet:`` row carries ``repo``. The per-machine PATH is not recorded here: it embeds
    the machine key, so a manifest value would be wrong on every other machine.

    An EMPTY list is returned rather than raised for, unlike ``fleet:``: homes are an
    optional section, and the guard that has to fire is the CONSUMER's — ``bd_repo_setup.py``
    refuses to provision a prefix this list does not contain, so an absent or mistyped
    section refuses everything instead of provisioning something unregistered.
    """
    return _parse_manifest_section(Path(path).read_text(), HOMES_SECTION)


def load_display_id_ceiling(path: Path = DEFAULT_PREFIX_MANIFEST) -> int:
    """The EXCLUSIVE upper bound on a display id's ``N``, from the manifest (ISSUE-REF).

    A sane id satisfies ``N < ceiling``; ``N >= ceiling`` is the retired range.
    Parsed with the same stdlib-only discipline as the ``fleet:`` list above, for
    the same ISSUE-REF reason: the Tier-B ``bd-precommit-validate`` hook reads this
    under bare system ``python3``, where PyYAML is unavailable.

    Raises ``MissingCeilingError`` when the key is absent, unparseable, or not a
    positive integer. Never returns a fallback — see ``MissingCeilingError``.
    """
    resolved = Path(path)
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise MissingCeilingError(
            f"could not read the prefix manifest {resolved}: {exc}"
        ) from exc
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(f"{CEILING_KEY}:"):
            continue
        value = line.split(":", 1)[1].split("#", 1)[0].strip()
        try:
            ceiling = int(value)
        except ValueError as exc:
            raise MissingCeilingError(
                f"{resolved} declares {CEILING_KEY}: {value!r}, which is not an integer"
            ) from exc
        if ceiling <= 0:
            raise MissingCeilingError(
                f"{resolved} declares {CEILING_KEY}: {ceiling}, which must be positive"
            )
        return ceiling
    raise MissingCeilingError(
        f"{resolved} declares no {CEILING_KEY} — the retired-display-id range is "
        "policy and must be declared, never inferred from the data being extended"
    )


def _prefix_shape_ok(prefix: str) -> bool:
    """UPPERCASE, starts with a letter, letters/digits/hyphens only (bd's own constraint)."""
    return (
        prefix[0].isalpha() and prefix.isupper() and prefix.replace("-", "").isalnum()
    )


def validate_home_registry(
    home_entries: list[dict], fleet_entries: list[dict]
) -> list[str]:
    """Return errors for the ``homes:`` list, INCLUDING collisions with ``fleet:``.

    A home's store is one more database on the same machine-local Dolt server as every
    repo's (D-13 (a)), and that server names each database after its prefix — so a prefix
    shared between a home and a repo is not a cosmetic duplicate, it is two stores claiming
    one database. Takes both lists for exactly that reason: a validator handed only
    ``homes:`` could not see the collision it exists to catch.

    An empty ``homes:`` list is VALID (the section is optional — see ``load_home_manifest``),
    which is the one way this differs from ``validate_prefix_bijection``.
    """
    errors: list[str] = []
    claimed: dict[str, str] = {}
    for entry in fleet_entries:
        prefix, repo = entry.get("prefix"), entry.get("repo")
        if prefix and repo:
            claimed[prefix] = f"fleet repo {repo!r}"
    seen_home: dict[str, str] = {}
    for entry in home_entries:
        prefix = entry.get("prefix")
        home = entry.get("home")
        if not prefix or not home:
            errors.append(f"malformed homes entry (needs prefix + home): {entry!r}")
            continue
        if not _prefix_shape_ok(prefix):
            errors.append(
                f"prefix {prefix!r} (home {home}) must be UPPERCASE, start with a letter, "
                "and contain only letters/digits/hyphens"
            )
        if prefix in claimed:
            errors.append(
                f"duplicate prefix {prefix!r}: claimed by both {claimed[prefix]} and "
                f"home {home!r} — one prefix is one database on the shared server (D-12/D-13)"
            )
        else:
            claimed[prefix] = f"home {home!r}"
        if home in seen_home:
            errors.append(
                f"home {home!r} appears twice (prefixes {seen_home[home]!r} and {prefix!r}) "
                "— one canonical prefix per home"
            )
        else:
            seen_home[home] = prefix
    return errors


def validate_prefix_bijection(entries: list[dict]) -> list[str]:
    """Return errors for a non-bijective or malformed prefix manifest."""
    errors: list[str] = []
    if not entries:
        return [
            "manifest declares zero repos (no 'fleet:' entries) — an empty fleet is never valid"
        ]
    seen_prefix: dict[str, str] = {}
    seen_repo: dict[str, str] = {}
    for entry in entries:
        prefix = entry.get("prefix")
        repo = entry.get("repo")
        if not prefix or not repo:
            errors.append(f"malformed manifest entry (needs prefix + repo): {entry!r}")
            continue
        if not _prefix_shape_ok(prefix):
            errors.append(
                f"prefix {prefix!r} (repo {repo}) must be UPPERCASE, start with a letter, "
                "and contain only letters/digits/hyphens"
            )
        if prefix in seen_prefix:
            errors.append(
                f"duplicate prefix {prefix!r}: claimed by both {seen_prefix[prefix]!r} and {repo!r} "
                "— prefixes must be a fleet-wide bijection (D-12)"
            )
        else:
            seen_prefix[prefix] = repo
        if repo in seen_repo:
            errors.append(
                f"repo {repo!r} appears twice (prefixes {seen_repo[repo]!r} and {prefix!r}) "
                "— one canonical prefix per repo"
            )
        else:
            seen_repo[repo] = prefix
    return errors
