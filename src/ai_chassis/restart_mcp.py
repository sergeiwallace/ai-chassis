"""Generate restart-gate identities from configuration and isolated, owned starts.

No runtime session is inspected. Configuration values and process errors must never
be included in diagnostics: either may contain credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .manual_steps import sanitize


class Unverified(ValueError):
    """A fixed, credential-free reason why certification was refused."""


@dataclass(frozen=True)
class Server:
    name: str
    command: str
    args: tuple[str, ...]
    cwd: Path
    blocked: str = ""
    installation: Path | None = None
    package: str = ""
    config_home: Path | None = None
    identity_files: tuple[Path, ...] = ()


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise Unverified("MCP configuration unreadable") from None


def _scopes(home: Path, cwd: Path) -> list[dict]:
    # Same inputs as session_restart._settings / _mcp_commands, with settings'
    # precedence applied to names. Local settings are the final override.
    user = _object(home / ".claude.json")
    projects = user.get("projects", {})
    if not isinstance(projects, dict):
        raise Unverified("MCP configuration unreadable")
    return [
        _object(home / ".claude/settings.json"),
        user,
        projects.get(str(cwd), {}),
        _object(cwd / ".mcp.json"),
        _object(cwd / ".claude/settings.json"),
        _object(cwd / ".claude/settings.local.json"),
    ]


def _definition(name: str, definition: object, cwd: Path) -> Server | None:
    if not isinstance(definition, dict):
        raise Unverified("MCP configuration unreadable")
    if definition.get("type", "stdio") != "stdio":
        return None
    command, args = definition.get("command"), definition.get("args", [])
    if not isinstance(command, str) or not command:
        raise Unverified("MCP configuration unreadable")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise Unverified("MCP configuration unreadable")
    if sanitize(name) != name:
        raise Unverified("unsafe server name")
    text = " ".join((command, *args))
    if sanitize(text) != text:
        return Server(name, "", (), cwd, "credential-bearing arguments refused")
    # Never copy env into a Server (including its repr), a subprocess, or a log.
    blocked = "configured environment required" if definition.get("env") else ""
    if any("${" in word for word in (command, *args)):
        blocked = "environment expansion required"
    return Server(name, command, tuple(args), cwd, blocked)


def discover(home: Path, cwd: Path, core_state: Path | None = None) -> list[Server]:
    """Read effective stdio definitions, never discover trust from live children."""
    servers: dict[str, Server] = {}
    for scope in _scopes(home, cwd):
        if not isinstance(scope, dict) or not isinstance(
            scope.get("mcpServers", {}), dict
        ):
            raise Unverified("MCP configuration unreadable")
        for name, definition in scope.get("mcpServers", {}).items():
            if not isinstance(name, str) or not name:
                raise Unverified("MCP configuration unreadable")
            server = _definition(name, definition, cwd)
            servers.pop(name, None)
            if server:
                servers[name] = server
    core = _core_plugin(home, cwd, core_state)
    if core or "ai-harness-core" in servers:
        # A direct registration of this name is a conflict, even alongside a receipt.
        servers["ai-harness-core"] = (
            Server(
                "ai-harness-core", "", (), cwd, "core registration ownership unresolved"
            )
            if "ai-harness-core" in servers
            else core
        )
    return [servers[name] for name in sorted(servers)]


def _core_plugin(home: Path, cwd: Path, state: Path | None) -> Server | None:
    """Trust only the receipt and exact client entries, never scan plugin caches."""
    from platformdirs import user_state_path
    from .mcp import client_install as core

    state = (
        state if state is not None else Path(user_state_path("ai-harness")) / "core-mcp"
    )
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    name = "ai-harness-core"
    try:
        core._registration_paths(state, directory)
        receipt = core._registration_json(state / "registration.json")
        documents = core._preflight_claude(directory, receipt, state)
        owned = receipt.get("claude")
        if not owned:
            return None
        generation = Path(owned["generation"])
        artifacts = core.ClientArtifacts(
            {name: (generation / name).read_bytes() for name in owned["manifest"]},
            owned["manifest"],
            "",
        )
        runtime = core._artifact_runtime(artifacts)
        definition = _definition(
            name,
            {"command": runtime.interpreter, "args": ["-P", "-m", "ai_chassis.mcp"]},
            cwd,
        )
        if definition.blocked or "${" in runtime.config_home:
            raise Unverified("core environment expansion unresolved")
        if core._installed_claude(documents, generation, artifacts, directory) != "ok":
            raise core.ArtifactError("Core registration unavailable")
        interpreter = Path(runtime.interpreter)
        if not interpreter.is_file():
            raise core.ArtifactError("Core runtime unavailable")
        installed = Path(core._claude_entries(documents)["installed"][0]["installPath"])
        paths = (
            state / "registration.json",
            *(directory / name for name in core.CLAUDE_FILES),
            *(generation / name for name in (*owned["manifest"], core.MANIFEST)),
            installed / ".mcp.json",
            *_core_implementation(interpreter),
        )
        identities(paths)
        return Server(
            name,
            runtime.interpreter,
            ("-P", "-m", "ai_chassis.mcp"),
            cwd,
            config_home=Path(runtime.config_home),
            identity_files=paths,
        )
    except (
        core.ArtifactError,
        AttributeError,
        Unverified,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ):
        return Server(
            name, "", (), cwd, "core registration ownership or identity unresolved"
        )


def _core_implementation(interpreter: Path) -> tuple[Path, ...]:
    """Locate installed package code from its environment metadata, without importing it."""
    from importlib.metadata import distributions
    from urllib.parse import unquote, urlparse

    prefix = interpreter.parent.parent
    paths = [prefix / "Lib/site-packages", *prefix.glob("lib/python*/site-packages")]
    for dist in distributions(path=[str(path) for path in paths]):
        if dist.metadata["Name"].replace("_", "-") != "ai-chassis":
            continue
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        url = direct.get("url", "")
        if direct.get("dir_info", {}).get("editable") and url.startswith("file:"):
            package = Path(unquote(urlparse(url).path)) / "src/ai_chassis"
        else:
            package = Path(dist.locate_file("ai_chassis"))
        package = package.resolve(strict=True)
        if ".worktrees" in package.parts or not (package / "mcp/__main__.py").is_file():
            break
        return tuple(sorted(package.rglob("*.py")))
    raise Unverified("installed core implementation unavailable")


def allowlist_path(home: Path, environ: Mapping[str, str]) -> Path:
    """Mirror ai-cli-utils' non-Windows get_xdg_config_home, without migration.

    The consumer migrates ai-cli to ai-cli-utils if necessary; the installer does
    that only at apply time, so plan and doctor remain read-only.
    """
    raw = environ.get("XDG_CONFIG_HOME", "")
    base = Path(raw) if raw and Path(raw).is_absolute() else home / ".config"
    return base / "ai-cli-utils/restart-session.d/mcp-servers.toml"


def identities(paths: Sequence[Path]) -> list[dict]:
    """Stamp canonical files, refusing a file modified during its read."""
    result = []
    try:
        for path in sorted({p.resolve(strict=True) for p in paths}):
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise Unverified("implementation changed during observation")
            result.append({"path": str(path), "size": before.st_size, "sha256": digest})
    except OSError:
        raise Unverified("implementation identity unreadable") from None
    return result


def render(entries: list[dict]) -> str:
    """JSON string escaping is also valid TOML basic-string escaping."""
    lines = [
        "# Generated by ai-harness; regenerate with apply --only restart-mcp.",
        "version = 1",
    ]
    if not entries:
        lines.append("servers = []")
    for entry in entries:
        lines.extend(
            [
                "",
                "[[servers]]",
                f"name = {json.dumps(entry['name'], ensure_ascii=False)}",
                f"executable = {json.dumps(entry['executable'], ensure_ascii=False)}",
                f"argv = {json.dumps(entry['argv'], ensure_ascii=False)}",
            ]
        )
        for stamp in entry["identities"]:
            lines.extend(
                [
                    "",
                    "[[servers.identities]]",
                    f"path = {json.dumps(stamp['path'], ensure_ascii=False)}",
                    f"size = {stamp['size']}",
                    f'sha256 = "{stamp["sha256"]}"',
                ]
            )
    return "\n".join(lines) + "\n"


def _migrate_config_home(target: Path) -> int:
    """Mirror the consumer's one-time ai-cli -> ai-cli-utils directory rename."""
    config = target.parent.parent
    if config.name != "ai-cli-utils" or target.parent.name != "restart-session.d":
        return 0
    old = config.with_name("ai-cli")
    if old.exists() and not config.exists():
        if old.is_symlink() or not old.is_dir() or config.is_symlink():
            raise Unverified("legacy config directory unsafe")
        # Both actual targets were inspected. Rename preserves the legacy files.
        old.rename(config)
        return 1
    return 0


def generate(
    home: Path, cwd: Path, target: Path, core_state: Path | None = None
) -> tuple[int, list[str]]:
    """Only apply observes servers, and only changed bytes reach the atomic writer."""
    from .agent_mail_notifier import write_if_changed
    from .restart_mcp_observe import npm_installations, verify

    servers = discover(home, cwd, core_state)
    entries, messages = [], []
    for server in servers:
        try:
            candidates = npm_installations(server, home)
        except Unverified as exc:
            messages.append(f"restart-mcp {server.name}: unverified: {exc}")
            continue
        for candidate in candidates:
            _certify(candidate, entries, messages, verify)
    content = render(entries)
    migrated = _migrate_config_home(target)
    # Inspect the actual target before replacement; an unreadable file is a refusal.
    if target.is_symlink():
        raise Unverified("allowlist target is a symlink")
    try:
        target.read_bytes()
    except FileNotFoundError:
        pass
    except OSError:
        raise Unverified("allowlist target unreadable") from None
    changed = migrated + int(write_if_changed(target, content))
    messages.append(f"restart-mcp apply: {changed} mutations")
    return changed, messages


def _certify(server: Server, entries: list[dict], messages: list[str], verify) -> None:
    try:
        if server.blocked:
            raise Unverified(server.blocked)
        before = identities(server.identity_files)
        entry = verify(server)
        if before != identities(server.identity_files):
            raise Unverified("implementation changed during observation")
        if server.config_home is not None:
            if entry["executable"] != str(
                Path(server.command).resolve(strict=True)
            ) or entry["argv"][1:] != list(server.args):
                raise Unverified("core runtime does not match registration")
            entry["identities"] = identities(
                [Path(stamp["path"]) for stamp in (*entry["identities"], *before)]
            )
        try:
            _validate_entry(entry)
        except (ValueError, OSError, KeyError, TypeError):
            raise Unverified("implementation changed after observation") from None
        entries.append(entry)
    except Unverified as exc:
        messages.append(f"restart-mcp {server.name}: unverified: {exc}")


def _validate_entry(entry: dict) -> None:
    import re

    executable, argv, stamps = entry["executable"], entry["argv"], entry["identities"]
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ValueError
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) for a in argv)
        or not argv[0]
    ):
        raise ValueError
    if not isinstance(stamps, list) or not stamps:
        raise ValueError
    paths = []
    for stamp in stamps:
        path = Path(stamp["path"])
        if not path.is_absolute() or path.resolve(strict=True) != path or path in paths:
            raise ValueError
        if (
            type(stamp["size"]) is not int
            or stamp["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", stamp["sha256"])
        ):
            raise ValueError
        paths.append(path)
    if Path(executable) not in paths or identities(paths) != sorted(
        stamps, key=lambda s: s["path"]
    ):
        raise ValueError


def check(target: Path, servers: list[Server]) -> tuple[list[str], bool]:
    """Doctor/plan only read configured names and the recorded on-disk identities."""
    import tomllib

    try:
        data = tomllib.loads(target.read_text())
        if (
            type(data.get("version")) is not int
            or data["version"] != 1
            or not isinstance(data.get("servers"), list)
        ):
            raise ValueError
        entries = data["servers"]
        names = [e["name"] for e in entries]
        if any(
            not isinstance(n, str) or not n or sanitize(n) != n for n in names
        ) or len(set(names)) != len(names):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        return [
            "restart-mcp: missing or invalid allowlist; run ai-harness apply --only restart-mcp"
        ], False
    messages = []
    valid_entries = []
    for entry in entries:
        try:
            _validate_entry(entry)
            valid_entries.append(entry)
        except (OSError, ValueError, TypeError, KeyError):
            messages.append(
                f"restart-mcp {entry['name']}: stale identity; run ai-harness apply --only restart-mcp"
            )
    for server in servers:
        if (
            server.blocked
            or server.name not in names
            or (
                server.config_home is not None
                and not any(
                    entry["name"] == server.name
                    and entry["executable"] == str(Path(server.command).resolve())
                    and entry["argv"][1:] == list(server.args)
                    and set(server.identity_files).issubset(
                        {Path(stamp["path"]) for stamp in entry["identities"]}
                    )
                    for entry in valid_entries
                )
            )
        ):
            messages.append(
                f"restart-mcp {server.name}: unverified; run ai-harness apply --only restart-mcp"
            )
    return messages or ["restart-mcp: verified identities current"], not messages
