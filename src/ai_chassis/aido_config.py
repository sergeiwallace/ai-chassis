"""Seed fleet defaults without reserializing aido's user-owned YAML."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import tomllib


_KEY = re.compile(
    r"""^(?P<key>doppler_project|'doppler_project'|"doppler_project")[ \t]*:(?P<rest>.*)$"""
)
_VALUE = re.compile(r"^(?P<lead>[ \t]*)(?P<value>[^#]*?)(?P<tail>[ \t]+#.*|[ \t]*)$")
_PROFILES_KEY = re.compile(
    r"""^(machine_profiles|'machine_profiles'|"machine_profiles")[ \t]*:"""
)
_ANCHOR = re.compile(r"(?:^|[\s\[{,:])[&*][^\s]+")


def _append_mapping(text: str, addition: str) -> str:
    """Insert before a document end marker while preserving existing bytes."""
    lines = text.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].rstrip("\r\n") == "...":
            lines.insert(index, addition)
            return "".join(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = newline if text and not text.endswith("\n") else ""
    return text + separator + addition


def _seed_profiles(text: str, profiles: dict[str, list[str]]) -> str:
    lines = text.splitlines()
    body = [
        line
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "---", "%"))
    ]
    if body and body[0].lstrip()[:1] in {"{", "["}:
        raise ValueError("expected a block-style YAML mapping")
    if any(_ANCHOR.search(line.split("#", 1)[0]) for line in lines):
        raise ValueError("cannot safely edit YAML anchors or aliases")
    hits = sum(bool(_PROFILES_KEY.match(line)) for line in lines)
    if hits > 1:
        raise ValueError("duplicate machine profile keys")
    if hits or not profiles:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    addition = ["machine_profiles:"]
    for profile, hosts in profiles.items():
        # The fleet file uses plain identifiers; reject YAML syntax in TOML data.
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", profile) or not isinstance(hosts, list):
            raise ValueError("expected profile identifiers and host lists")
        addition.append(f"  {profile}:" if hosts else f"  {profile}: []")
        for host in hosts:
            if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", host):
                raise ValueError("expected host identifiers")
            addition.append(f"    - {host}")
    return _append_mapping(text, newline.join(addition) + newline)


def updated_config(text: str, project: str) -> str:
    """Edit just the top-level ``doppler_project`` line, keeping every other byte.

    Stdlib only: this module is imported by ``cli.py`` and runs inside the
    ai-harness uv tool, which must not need PyYAML. aido's config is block-style
    YAML; any shape this line edit cannot handle safely raises ``ValueError``
    so the step skips instead of guessing.
    """
    rendered = json.dumps(project)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    body = [
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if body and body[0].lstrip()[:1] in {"{", "["}:
        raise ValueError("expected a block-style YAML mapping")
    hits = [
        index for index, line in enumerate(lines) if _KEY.match(line.rstrip("\r\n"))
    ]
    if len(hits) > 1:
        raise ValueError("duplicate Doppler project keys")
    if hits:
        index = hits[0]
        raw = lines[index]
        ending = raw[len(raw.rstrip("\r\n")) :]
        found = _KEY.match(raw.rstrip("\r\n"))
        parts = _VALUE.match(found["rest"])
        value = parts["value"].strip()
        if value[:1] in {"&", "*", "|", ">", "[", "{"}:
            raise ValueError("cannot safely edit this Doppler project value")
        if value in {project, rendered, f"'{project}'"}:
            return text
        lines[index] = f"{found['key']}: {rendered}{parts['tail']}{ending}"
        return "".join(lines)
    addition = f"doppler_project: {rendered}{newline}"
    return _append_mapping(text, addition)


def seed(projects_root: Path, config: Path) -> bool:
    """Return whether the projects-wide config changed; do not write on a no-op."""
    with config.open("rb") as stream:
        defaults = tomllib.load(stream)
    project = defaults["doppler_project"]
    profiles = defaults.get("machine_profiles", {})
    if not isinstance(project, str) or not project:
        raise ValueError("expected a nonempty project string")
    if not isinstance(profiles, dict):
        raise ValueError("expected a machine profile mapping")
    target = projects_root / ".aido" / "config.yaml"
    original = target.read_bytes().decode("utf-8") if target.exists() else ""
    updated = _seed_profiles(updated_config(original, project), profiles)
    if original == updated:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        stream.write(updated.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, target)
    return True


def apply_config(projects_root: Path, config: Path) -> int:
    """Seed the projects-wide config, keeping diagnostics free of user values."""
    if not config.is_file():
        print("  [skip] aido fleet defaults: no fleet config")
        return 0
    try:
        changed = seed(projects_root, config)
    except (OSError, ValueError, KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Parser exceptions may contain user config values; never print them.
        print("  [skip] aido fleet defaults: config could not be safely updated")
        return 1
    status = "fixed" if changed else "ok"
    print(f"  [{status}] aido projects-wide fleet defaults")
    return 0
