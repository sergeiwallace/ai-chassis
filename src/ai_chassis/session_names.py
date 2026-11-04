"""Fleet session-name policy: the registry ref prefix and the ``config/session-names.toml`` check.

Governing design: ``docs/designs/issue-ref-human-name-session-identifiers.md`` (Phase 2, D-2,
D-6, D-9, D-13, D-14). ai-cli-utils writes the registry ref and reads this file; ai-harness only
validates the file, at commit time (``tests/test_session_names_config.py``) and at install time
(the ``session-names`` apply step), so a bad value stops an install before any host applies it.

Every problem is one line that names the file and the key, and for a value check the value and
the range it must lie in.
"""

from __future__ import annotations

import base64
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

#: The ref namespace of the reservation registry. The repository's pre-push hook exempts it from
#: the pre-push gates and the branch-retirement census skips it. A test pins the hook's spelling
#: and ``registry_ref`` in the config to this value.
REGISTRY_REF_PREFIX = "refs/heads/fleet-session-names/"

CONFIG_PATH = Path("config") / "session-names.toml"
FLEET_PROJECTS_PATH = Path("config") / "fleet-projects.toml"
HOST_ALIASES_PATH = Path("config") / "machine-host-aliases.toml"

SCHEMA = 1
LENGTH_FLOOR = 2
LENGTH_CEILING = 16
LENGTH_KEYS = ("min_name_length", "max_name_length")
POSITIVE_INT_KEYS = (
    "pick_floor",
    "low_water",
    "offline_spares",
    "publish_timeout_s",
    "publish_attempts",
)
NAME_WITH_LABEL = ("keep", "drop")
EDIT_DISTANCES = (0, 1)

_LETTERS = re.compile(r"[a-z]+")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_SHA512_SRI = re.compile(r"sha512-([A-Za-z0-9+/]+={0,2})")
_SHA512_BYTES = 64


class SessionNamesConfigError(ValueError):
    """The registries this check reads beside the config could not be read."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _distinct_letters(alias: str) -> list[str]:
    return sorted({char for char in alias if char.isalpha()})


def _table(document: Mapping[str, object], key: str, problems: list[str]) -> dict:
    value = document.get(key)
    if isinstance(value, dict):
        return value
    problems.append(f"{CONFIG_PATH}: [{key}] is missing or is not a table")
    return {}


def _check_top_level(document: Mapping[str, object], problems: list[str]) -> None:
    if document.get("schema") != SCHEMA:
        problems.append(
            f"{CONFIG_PATH}: schema = {document.get('schema')!r}; it must be {SCHEMA}"
        )
    ref = document.get("registry_ref")
    if (
        not isinstance(ref, str)
        or not ref.startswith(REGISTRY_REF_PREFIX)
        or not ref[len(REGISTRY_REF_PREFIX) :]
        or "/" in ref[len(REGISTRY_REF_PREFIX) :]
    ):
        problems.append(
            f"{CONFIG_PATH}: registry_ref = {ref!r}; it must be one ref directly under "
            f"{REGISTRY_REF_PREFIX}, the prefix the pre-push gate and the branch census exempt"
        )
    remote = document.get("registry_remote")
    if not isinstance(remote, str) or not remote:
        problems.append(
            f"{CONFIG_PATH}: registry_remote = {remote!r}; it must name a git remote"
        )
    for key in POSITIVE_INT_KEYS:
        value = document.get(key)
        if not _is_int(value) or value < 1:
            problems.append(
                f"{CONFIG_PATH}: {key} = {value!r}; it must be a positive integer (1 or more)"
            )
    _check_names_list(document.get("denylist"), "denylist", problems)


def _check_names_list(value: object, key: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list):
        problems.append(f"{CONFIG_PATH}: {key} = {value!r}; it must be a list of names")
        return []
    for entry in value:
        if not isinstance(entry, str) or not _LETTERS.fullmatch(entry):
            problems.append(
                f"{CONFIG_PATH}: {key} entry {entry!r} is not lowercase letters a-z"
            )
    return [entry for entry in value if isinstance(entry, str)]


def _check_limits(document: Mapping[str, object], problems: list[str]) -> None:
    limits = _table(document, "limits", problems)
    low = limits.get("min_name_length")
    if not _is_int(low) or not LENGTH_FLOOR <= low <= LENGTH_CEILING:
        problems.append(
            f"{CONFIG_PATH}: [limits] min_name_length = {low!r}; it must be an integer "
            f"from {LENGTH_FLOOR} to {LENGTH_CEILING}"
        )
        low = LENGTH_FLOOR
    high = limits.get("max_name_length")
    if not _is_int(high) or not low <= high <= LENGTH_CEILING:
        problems.append(
            f"{CONFIG_PATH}: [limits] max_name_length = {high!r}; it must be an integer "
            f"from min_name_length ({low}) to {LENGTH_CEILING}"
        )


def _check_per_machine(
    table: Mapping[str, object], where: str, problems: list[str]
) -> None:
    """The keys a ``[defaults]`` or ``[hosts.<id>]`` table may carry, and the two it may not."""
    for key in LENGTH_KEYS:
        if key in table:
            problems.append(
                f"{CONFIG_PATH}: {where} sets {key}; the two length limits are fleet-wide "
                "and belong in [limits] only"
            )
    if "name_with_label" in table and table["name_with_label"] not in NAME_WITH_LABEL:
        problems.append(
            f"{CONFIG_PATH}: {where} name_with_label = {table['name_with_label']!r}; it "
            f"must be one of {', '.join(NAME_WITH_LABEL)}"
        )
    if "enabled" in table and not isinstance(table["enabled"], bool):
        problems.append(
            f"{CONFIG_PATH}: {where} enabled = {table['enabled']!r}; it must be true or false"
        )


def _check_defaults_and_hosts(
    document: Mapping[str, object], host_ids: set[str], problems: list[str]
) -> None:
    defaults = _table(document, "defaults", problems)
    if defaults.get("name_with_label") not in NAME_WITH_LABEL:
        problems.append(
            f"{CONFIG_PATH}: [defaults] name_with_label = "
            f"{defaults.get('name_with_label')!r}; it must be one of "
            f"{', '.join(NAME_WITH_LABEL)}"
        )
    _check_per_machine(
        {key: value for key, value in defaults.items() if key != "name_with_label"},
        "[defaults]",
        problems,
    )
    hosts = document.get("hosts", {})
    if not isinstance(hosts, dict):
        problems.append(f"{CONFIG_PATH}: [hosts] is not a table of host tables")
        return
    for host, table in hosts.items():
        where = f"[hosts.{host}]"
        if host not in host_ids:
            problems.append(
                f"{CONFIG_PATH}: {where} names {host!r}, which is not a host id in "
                f"{HOST_ALIASES_PATH} [aliases]"
            )
        if not isinstance(table, dict):
            problems.append(f"{CONFIG_PATH}: {where} is not a table")
            continue
        _check_per_machine(table, where, problems)


def _check_repos(
    document: Mapping[str, object], task_prefixes: set[str], problems: list[str]
) -> None:
    repos = document.get("repos", {})
    if not isinstance(repos, dict):
        problems.append(f"{CONFIG_PATH}: [repos] is not a table of repo tables")
        return
    for alias, table in repos.items():
        where = f"[repos.{alias}]"
        if alias not in task_prefixes:
            problems.append(
                f"{CONFIG_PATH}: {where} names {alias!r}, which is not a task_prefix in "
                f"{FLEET_PROJECTS_PATH}"
            )
        letters = table.get("letters") if isinstance(table, dict) else None
        if not isinstance(letters, list) or not letters:
            problems.append(
                f"{CONFIG_PATH}: {where} letters must be a non-empty list of single "
                f"lowercase letters, got {letters!r}"
            )
            continue
        own = _distinct_letters(alias)
        seen: set[object] = set()
        for letter in letters:
            if not isinstance(letter, str) or not re.fullmatch(r"[a-z]", letter):
                problems.append(
                    f"{CONFIG_PATH}: {where} letters entry {letter!r} is not one "
                    "lowercase letter a-z"
                )
            elif letter in seen:
                problems.append(
                    f"{CONFIG_PATH}: {where} letters lists {letter!r} more than once"
                )
            elif letter not in own:
                problems.append(
                    f"{CONFIG_PATH}: {where} letters entry {letter!r} is not a letter of "
                    f"the alias {alias!r}, whose letter set is {', '.join(own)}"
                )
            seen.add(letter)


def _check_https(value: object, where: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not value.startswith("https://"):
        problems.append(
            f"{CONFIG_PATH}: {where} = {value!r}; it must be an https:// URL"
        )


def _is_sha512_sri(value: object) -> bool:
    if not isinstance(value, str):
        return False
    matched = _SHA512_SRI.fullmatch(value)
    if matched is None:
        return False
    try:
        return len(base64.b64decode(matched.group(1), validate=True)) == _SHA512_BYTES
    except ValueError:
        return False


def _check_sources(document: Mapping[str, object], problems: list[str]) -> None:
    source = _table(document, "source", problems)
    _check_https(source.get("url"), "[source] url", problems)
    if not isinstance(source.get("digest"), str) or not _SHA256.fullmatch(
        source["digest"]
    ):
        problems.append(
            f"{CONFIG_PATH}: [source] digest = {source.get('digest')!r}; it must be "
            "sha256: followed by 64 lowercase hex characters"
        )
    members = source.get("members")
    if (
        not isinstance(members, list)
        or not members
        or not all(isinstance(entry, str) and entry for entry in members)
    ):
        problems.append(
            f"{CONFIG_PATH}: [source] members = {members!r}; it must list at least one "
            "archive member"
        )

    confusable = _table(document, "confusable", problems)
    _check_https(confusable.get("words_url"), "[confusable] words_url", problems)
    if not _is_sha512_sri(confusable.get("words_digest")):
        problems.append(
            f"{CONFIG_PATH}: [confusable] words_digest = "
            f"{confusable.get('words_digest')!r}; it must be sha512- followed by the "
            "base64 of a 64-byte SHA-512"
        )
    member = confusable.get("words_member")
    if not isinstance(member, str) or not member:
        problems.append(
            f"{CONFIG_PATH}: [confusable] words_member = {member!r}; it must name the "
            "archive member"
        )
    distance = confusable.get("max_edit_distance")
    if not _is_int(distance) or distance not in EDIT_DISTANCES:
        problems.append(
            f"{CONFIG_PATH}: [confusable] max_edit_distance = {distance!r}; it must be 0 or 1"
        )
    extra = _check_names_list(
        confusable.get("extra_words"), "[confusable] extra_words", problems
    )
    keep = _check_names_list(
        confusable.get("keep_names"), "[confusable] keep_names", problems
    )
    for name in sorted(set(extra) & set(keep)):
        problems.append(
            f"{CONFIG_PATH}: [confusable] {name!r} is in both extra_words and keep_names"
        )


def check_document(
    document: Mapping[str, object], *, task_prefixes: set[str], host_ids: set[str]
) -> list[str]:
    """Every problem with one parsed ``config/session-names.toml``, in file order."""
    problems: list[str] = []
    _check_top_level(document, problems)
    _check_limits(document, problems)
    _check_sources(document, problems)
    _check_defaults_and_hosts(document, host_ids, problems)
    _check_repos(document, task_prefixes, problems)
    return problems


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SessionNamesConfigError(f"{path.name} cannot be read: {exc}") from exc


def registered_task_prefixes(checkout: Path) -> set[str]:
    projects = _read_toml(checkout / FLEET_PROJECTS_PATH).get("projects", [])
    return {
        str(entry["task_prefix"])
        for entry in projects
        if isinstance(entry, dict) and entry.get("task_prefix")
    }


def registered_host_ids(checkout: Path) -> set[str]:
    aliases = _read_toml(checkout / HOST_ALIASES_PATH).get("aliases", {})
    return set(aliases) if isinstance(aliases, dict) else set()


def check_checkout(checkout: Path) -> list[str]:
    """Every problem with ``checkout``'s ``config/session-names.toml``; empty when it is valid."""
    path = checkout / CONFIG_PATH
    if not path.is_file():
        return [f"{CONFIG_PATH}: the file is missing from {checkout}"]
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"{CONFIG_PATH}: the file cannot be parsed: {exc}"]
    try:
        prefixes = registered_task_prefixes(checkout)
        hosts = registered_host_ids(checkout)
    except SessionNamesConfigError as exc:
        return [f"{CONFIG_PATH}: cannot be checked, because {exc}"]
    return check_document(document, task_prefixes=prefixes, host_ids=hosts)


def summary(checkout: Path) -> str:
    """The step's one ok line for a checkout ``check_checkout`` found valid."""
    document = tomllib.loads((checkout / CONFIG_PATH).read_text(encoding="utf-8"))
    limits = document["limits"]
    hosts = document.get("hosts", {})
    enabled = sum(1 for table in hosts.values() if table.get("enabled") is True)
    return (
        f"  [ok]  session-names: {CONFIG_PATH} is valid (names "
        f"{limits['min_name_length']}-{limits['max_name_length']} letters, "
        f"{len(document.get('repos', {}))} repo letter set(s), "
        f"{enabled} host(s) enabled)"
    )
