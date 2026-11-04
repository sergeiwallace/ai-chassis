"""Installer-only rendering and read-only plans; no default publication location.

The caller supplies a managed runtime identity and the validated config's [mcp]
table. Rendering does not discover a checkout, inspect the environment or launch
clients. Registration/publication belongs to the installer, never server handlers.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import errno
import json
import os
import subprocess
from pathlib import Path, PureWindowsPath
import re
from string import Template
import tomllib

from ai_chassis import codex_integration, plugins
from ai_chassis.links import LinkError
from ai_chassis.render import _write_if_changed
from ai_chassis.owned_registration import (
    RegistrationError,
    json_object,
    merge_owned_entry,
    publish_if_changed,
)

from .policy import load_catalog, load_policy

PLUGIN = "plugins/ai-harness-core"
JSON_TEMPLATES = (
    ".claude-plugin/marketplace.json",
    f"{PLUGIN}/.claude-plugin/plugin.json",
    f"{PLUGIN}/.mcp.json",
)
MANIFEST = "artifact-manifest.json"
CLAUDE_DIGEST_SCHEME = "semantic-v1"
ARTIFACT_PATHS = frozenset(
    (*JSON_TEMPLATES, "artifact.json", f"{PLUGIN}/artifact.json")
)
BEGIN = "# BEGIN ai-harness-core sha256:"
END = "# END ai-harness-core\n"
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class ArtifactError(ValueError):
    """Invalid runtime/settings or a conflicting artifact; contains no input bytes."""


@dataclass(frozen=True)
class Runtime:
    interpreter: str
    config_home: str
    package_version: str
    source_revision: str
    sdk_version: str


@dataclass(frozen=True)
class ClientArtifacts:
    files: dict[str, bytes]
    manifest: dict[str, str]
    codex_block: str


@dataclass(frozen=True)
class Change:
    path: Path
    state: str


@dataclass(frozen=True)
class InstallPlan:
    changes: tuple[Change, ...]
    codex_candidate: str | None = None
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return int(
            self.error is not None or any(c.state == "conflict" for c in self.changes)
        )


def _digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def _validate_runtime(runtime: Runtime) -> None:
    version = runtime.package_version
    if (
        not isinstance(version, str)
        or not SEMVER.fullmatch(version)
        or version == "0.0.0"
    ):
        raise ArtifactError("Invalid package version")
    for path in (runtime.interpreter, runtime.config_home):
        if not isinstance(path, str) or not (
            Path(path).is_absolute() or PureWindowsPath(path).is_absolute()
        ):
            raise ArtifactError("Invalid runtime path")
        if any(
            part in {".worktrees", ".."} for part in path.replace("\\", "/").split("/")
        ):
            raise ArtifactError("Invalid runtime path")
    if not all(
        isinstance(v, str) and v and ".worktrees" not in v
        for v in (runtime.source_revision, runtime.sdk_version)
    ):
        raise ArtifactError("Invalid runtime provenance")


def _template(name: str, values: dict) -> tuple[bytes, str]:
    source = files("ai_chassis").joinpath("defaults/mcp", name).read_bytes()
    # JSON basic-string escaping is also TOML basic-string escaping. Like the
    # existing restart_mcp serializer, never interpolate native paths unquoted.
    quoted = {
        key: json.dumps(value, ensure_ascii=False) for key, value in values.items()
    }
    if name in JSON_TEMPLATES:
        # JSON templates already contain string quotes and remain valid JSON.
        quoted = {
            key: value[1:-1]
            for key, value in quoted.items()
            if isinstance(values[key], str)
        }
    return Template(source.decode()).substitute(quoted).encode(), _digest(source)


def render_client_artifacts(
    runtime: Runtime, settings: dict | None = None
) -> ClientArtifacts:
    """Return deterministic bytes/digests, or refuse before returning any artifact."""
    try:
        policy = load_policy(settings)
    except ValueError:
        raise ArtifactError("Invalid MCP settings") from None
    settings = {} if settings is None else settings
    _validate_runtime(runtime)
    if not settings.get("enabled", True):
        return ClientArtifacts({}, {}, "")
    clients = settings.get("clients", ["claude", "codex"])
    values = {
        "package_version": runtime.package_version,
        "interpreter": runtime.interpreter,
        "config_home": runtime.config_home,
        "tools": list(policy.tools),
        "timeout": policy.call_timeout_seconds,
    }
    provenance = {
        "package_version": runtime.package_version,
        "api_major": load_catalog()["api_major"],
        "source_revision": runtime.source_revision,
        "sdk_version": runtime.sdk_version,
        "interpreter": runtime.interpreter,
        "config_home": runtime.config_home,
    }
    rendered, block = {}, ""
    if "claude" in clients:
        digests = {}
        for name in JSON_TEMPLATES:
            content, digests[name] = _template(name, values)
            json.loads(content)
            rendered[name] = content
        # Provenance lives beside the vendor manifests, which reject extra keys.
        metadata = {
            **provenance,
            "template_digests": digests,
            "artifact_digests": {
                name: _digest(content) for name, content in rendered.items()
            },
        }
        metadata["render_digest"] = _digest(_json(metadata))
        rendered["artifact.json"] = _json(metadata)
        rendered[f"{PLUGIN}/artifact.json"] = _json(metadata)
    if "codex" in clients:
        content, digest = _template("codex.toml.template", values)
        metadata = {**provenance, "template_digests": {"codex.toml.template": digest}}
        body = (
            "# "
            + json.dumps(metadata, sort_keys=True, ensure_ascii=False)
            + "\n"
            + content.decode()
        )
        block = BEGIN + _digest(body.encode()) + "\n" + body + END
        tomllib.loads(block)
    return ClientArtifacts(
        rendered, {name: _digest(data) for name, data in rendered.items()}, block
    )


def _safe_path(path: Path) -> bool:
    return not any(p.is_symlink() for p in (path, *path.parents))


def _read(path: Path) -> bytes | None:
    if not _safe_path(path):
        raise ArtifactError("Artifact conflict")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _owned_files(directory: Path) -> dict[str, str]:
    content = _read(directory / MANIFEST)
    if content is None:
        return {}
    manifest = json.loads(content)
    if not isinstance(manifest, dict) or set(manifest) - ARTIFACT_PATHS:
        raise ArtifactError("Artifact conflict")
    if any(
        not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None
        for v in manifest.values()
    ):
        raise ArtifactError("Artifact conflict")
    return manifest


def _artifact_changes(artifacts: ClientArtifacts, directory: Path) -> list[Change]:
    try:
        owned = _owned_files(directory)
        manifest = _read(directory / MANIFEST)
        if manifest is not None:
            owned[MANIFEST] = _digest(manifest)
    except (ArtifactError, OSError, ValueError):
        return [Change(directory, "conflict")]
    desired_files = _materialized_files(artifacts)
    changes = []
    for name in sorted(set(desired_files) | set(owned)):
        path = directory / name
        try:
            current = _read(path)
            desired = desired_files.get(name)
            if current is None:
                state = "create" if desired is not None else "unchanged"
            elif owned.get(name) != _digest(current):
                state = "conflict"
            elif desired is None:
                state = "remove"
            else:
                state = "unchanged" if current == desired else "change"
        except (ArtifactError, OSError):
            state = "conflict"
        changes.append(Change(path, state))
    return changes


def _materialized_files(artifacts: ClientArtifacts) -> dict[str, bytes]:
    return (
        {**artifacts.files, MANIFEST: _json(artifacts.manifest)}
        if artifacts.files
        else {}
    )


def write_client_artifacts(artifacts: ClientArtifacts, directory: Path) -> None:
    """Materialize only into an explicit caller directory; never replace differing bytes.

    This is a staging writer, not publication or registration. The installer must
    supply a fresh versioned directory for an upgrade. Identical repeats do no IO.
    """
    if set(artifacts.files) - ARTIFACT_PATHS or artifacts.manifest != {
        name: _digest(content) for name, content in artifacts.files.items()
    }:
        raise ArtifactError("Artifact conflict")
    desired = _materialized_files(artifacts)
    try:
        # Preflight every target before creating any directories/files.
        for name, content in desired.items():
            current = _read(directory / name)
            if current is not None and current != content:
                raise ArtifactError("Artifact conflict")
        for name, content in desired.items():
            path = directory / name
            if _read(path) is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_if_changed(path, content)
    except OSError:
        raise ArtifactError("Artifact conflict") from None


def _core_table(parsed: dict) -> object:
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ArtifactError("Codex conflict")
    core = servers.get("ai-harness-core")
    if core is not None and not isinstance(core, dict):
        raise ArtifactError("Codex conflict")
    return core


def _split_owned_block(existing: str) -> tuple[str, str]:
    """Verify an exact marked block and reject extensions outside its boundaries."""
    lines = existing.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line == END]
    parsed = tomllib.loads(existing)
    if not starts and not ends and _core_table(parsed) is None:
        return existing, ""
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ArtifactError("Codex conflict")
    start, end = starts[0], ends[0]
    body = "".join(lines[start + 1 : end])
    if lines[start] != BEGIN + _digest(body.encode()) + "\n":
        raise ArtifactError("Codex conflict")
    table = tomllib.loads(body)
    owned_table = _core_table(table)
    if set(table) != {"mcp_servers"} or set(table["mcp_servers"]) != {
        "ai-harness-core"
    }:
        raise ArtifactError("Codex conflict")
    remainder = "".join(lines[:start] + lines[end + 1 :])
    if (
        _core_table(tomllib.loads(remainder)) is not None
        or _core_table(parsed) != owned_table
    ):
        raise ArtifactError("Codex conflict")
    return remainder, "".join(lines[start : end + 1])


def _codex_change(path: Path, desired: str) -> tuple[Change, str | None]:
    try:
        current = _read(path)
        existing = current.decode() if current is not None else ""
        remainder, owned = _split_owned_block(existing)
        if owned:
            candidate = existing.replace(owned, desired, 1)
        else:
            separator = (
                "\n" if remainder and not remainder.endswith("\n") and desired else ""
            )
            candidate = remainder + separator + desired
        tomllib.loads(candidate)  # Validate the complete file, not only the block.
        if existing == candidate:
            state = "unchanged"
        elif not desired:
            state = "remove"
        else:
            state = "create" if current is None or not owned else "change"
        return Change(path, state), candidate
    except (ArtifactError, OSError, UnicodeError, ValueError):
        return Change(path, "conflict"), None


def plan_client_install(
    runtime: Runtime,
    settings: dict | None,
    artifact_directory: Path,
    codex_config: Path,
) -> InstallPlan:
    """Read supplied paths only; invalid inputs use the existing CLI failure code 1."""
    try:
        artifacts = render_client_artifacts(runtime, settings)
    except ArtifactError:
        return InstallPlan((), error="invalid_input")
    changes = _artifact_changes(artifacts, artifact_directory)
    change, candidate = _codex_change(codex_config, artifacts.codex_block)
    return InstallPlan(tuple([*changes, change]), codex_candidate=candidate)


@dataclass(frozen=True)
class RegistrationCheck:
    client: str
    code: str
    healthy: bool = True
    detail: str = ""

    @property
    def message(self) -> str:
        if not self.healthy:
            return "Client registration needs attention; no configuration contents are reported."
        return (
            "Client check was not performed."
            if self.code in {"not_checked", "skipped"}
            else "Client registration is ready."
        )


@dataclass(frozen=True)
class RegistrationResult:
    checks: tuple[RegistrationCheck, ...]

    @property
    def exit_code(self) -> int:
        return int(any(not check.healthy for check in self.checks))


CORE_ID = "ai-harness-core@ai-harness-core-local"
MARKETPLACE = "ai-harness-core-local"
CLAUDE_FILES = (
    "settings.json",
    "plugins/known_marketplaces.json",
    "plugins/installed_plugins.json",
)


@contextmanager
def _client_environment(directory: Path | None):
    """Temporarily isolate the unchanged plugin runner, which inherits os.environ.

    Installer calls must be serialized: process environment is shared by threads.
    No environment discovery is used to select a registration or config path.
    """
    if directory is None:
        yield
        return
    home = directory.parent
    overrides = {
        "CLAUDE_CONFIG_DIR": str(directory),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / "codex"),
        **{
            f"XDG_{kind}_HOME": str(home / kind.lower())
            for kind in ("CONFIG", "STATE", "CACHE", "DATA")
        },
    }
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _plugin_call(binary: str, directory: Path | None, *args: str) -> None:
    with _client_environment(directory):
        result = plugins._claude(binary, *args)
    if result.returncode:
        raise ArtifactError("Client operation failed")


def _registration_json(path: Path) -> dict:
    try:
        raw = _read(path)
        return json_object(raw.decode() if raw is not None else None)
    except RegistrationError:
        raise ArtifactError("Registration conflict") from None


def _claude_documents(directory: Path) -> dict[str, dict]:
    documents = {name: _registration_json(directory / name) for name in CLAUDE_FILES}
    settings, _, installed = documents.values()
    for value in (
        settings.get("enabledPlugins", {}),
        settings.get("extraKnownMarketplaces", {}),
        settings.get("mcpServers", {}),
        installed.get("plugins", {}),
    ):
        if not isinstance(value, dict):
            raise ArtifactError("Registration conflict")
    if "ai-harness-core" in settings.get("mcpServers", {}):
        raise ArtifactError("Registration conflict")
    return documents


def _claude_entries(documents: dict[str, dict]) -> dict:
    settings, known, installed = documents.values()
    return {
        "enabled": settings.get("enabledPlugins", {}).get(CORE_ID),
        "marketplace_setting": settings.get("extraKnownMarketplaces", {}).get(
            MARKETPLACE
        ),
        "marketplace": known.get(MARKETPLACE),
        "installed": installed.get("plugins", {}).get(CORE_ID),
    }


def _claude_semantic_entries(documents: dict[str, dict]) -> dict:
    """Select installer-owned fields, excluding client-maintained timestamps."""
    entries = _claude_entries(documents)
    setting, market, rows = (
        entries["marketplace_setting"],
        entries["marketplace"],
        entries["installed"],
    )
    if (
        not isinstance(setting, dict)
        or not isinstance(market, dict)
        or not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise ArtifactError("Modified client registration")
    sources = [setting.get("source"), market.get("source")]
    if not all(isinstance(source, dict) for source in sources):
        raise ArtifactError("Modified client registration")
    return {
        "plugin_id": CORE_ID,
        "enabled": entries["enabled"],
        "marketplace_setting": {key: sources[0].get(key) for key in ("source", "path")},
        "marketplace": {key: sources[1].get(key) for key in ("source", "path")},
        "installLocation": market.get("installLocation"),
        "installed": {
            key: rows[0].get(key) for key in ("installPath", "version", "scope")
        },
    }


def _expected_claude_semantics(generation: Path) -> dict:
    """Expected registration for a verified, installer-written generation."""
    source = {"source": "directory", "path": str(generation)}
    return {
        "plugin_id": CORE_ID,
        "enabled": True,
        "marketplace_setting": source,
        "marketplace": source,
        "installLocation": str(generation),
        "installed": {
            "installPath": str(generation / PLUGIN),
            "version": _registration_json(generation / "artifact.json")[
                "package_version"
            ],
            "scope": "user",
        },
    }


def _client_install_path_ok(value: object, generation: Path, directory: Path) -> bool:
    """The two places a client may record the plugin: in place, or its own cache copy."""
    if not isinstance(value, str):
        return False
    path = Path(value)
    return path == generation / PLUGIN or path.is_relative_to(
        directory / "plugins/cache" / MARKETPLACE / "ai-harness-core"
    )


def _verify_generation(directory: Path, manifest: dict) -> None:
    if not manifest or _owned_files(directory) != manifest:
        raise ArtifactError("Artifact conflict")
    if {
        str(path.relative_to(directory)).replace("\\", "/")
        for path in directory.rglob("*")
        if path.is_file() or path.is_symlink()
    } != {*manifest, MANIFEST}:
        raise ArtifactError("Artifact conflict")
    for name, digest in manifest.items():
        content = _read(directory / name)
        if content is None or _digest(content) != digest:
            raise ArtifactError("Artifact conflict")


def _verify_claude_owner(
    documents: dict, owned: dict, state_root: Path, directory: Path
) -> None:
    entries = _claude_entries(documents)
    if not owned:
        if any(value is not None for value in entries.values()):
            raise ArtifactError("Unowned client name")
        return
    generation = Path(owned["generation"])
    if not generation.is_relative_to(state_root / "artifacts"):
        raise ArtifactError("Registration conflict")
    _verify_generation(generation, owned["manifest"])
    live = _claude_semantic_entries(documents)
    semantic = _json(live)
    scheme = owned.get("entry_digest_scheme")
    if "entry_digest_scheme" not in owned:
        # Never accept the old full-record digest. Re-establish ownership only
        # from the exact fields this verified generation would register. The
        # install path is the one field the client chooses between two places
        # (the rule _installed_claude applies), so it is compared as that choice.
        expected = _expected_claude_semantics(generation)
        if _client_install_path_ok(
            live["installed"]["installPath"], generation, directory
        ):
            expected["installed"]["installPath"] = live["installed"]["installPath"]
        if semantic != _json(expected):
            raise ArtifactError("Modified client registration")
        owned.update(
            entry_digest_scheme=CLAUDE_DIGEST_SCHEME,
            entry_digest=_digest(semantic),
        )
    elif scheme != CLAUDE_DIGEST_SCHEME or _digest(semantic) != owned["entry_digest"]:
        raise ArtifactError("Modified client registration")


def _artifact_runtime(artifacts: ClientArtifacts) -> Runtime:
    """Validate the actual executable/config entries as well as their provenance."""
    sources = []
    if artifacts.files:
        if set(artifacts.files) != ARTIFACT_PATHS or artifacts.manifest != {
            name: _digest(data) for name, data in artifacts.files.items()
        }:
            raise ArtifactError("Artifact conflict")
        metadata = json.loads(artifacts.files["artifact.json"])
        server = json.loads(artifacts.files[f"{PLUGIN}/.mcp.json"])["mcpServers"][
            "ai-harness-core"
        ]
        sources.append((metadata, server))
    if artifacts.codex_block:
        _, block = _split_owned_block(artifacts.codex_block)
        metadata = json.loads(block.splitlines()[1].removeprefix("# "))
        sources.append((metadata, _core_table(tomllib.loads(block))))
    runtimes = []
    for metadata, server in sources:
        runtime = Runtime(
            **{field: metadata[field] for field in Runtime.__dataclass_fields__}
        )
        _validate_runtime(runtime)
        if (
            server["command"] != runtime.interpreter
            or server["args"] != ["-P", "-m", "ai_chassis.mcp"]
            or server["env"] != {"AI_HARNESS_CONFIG_HOME": runtime.config_home}
        ):
            raise ArtifactError("Invalid runtime registration")
        runtimes.append(runtime)
    if not runtimes or any(runtime != runtimes[0] for runtime in runtimes):
        raise ArtifactError("Invalid runtime registration")
    return runtimes[0]


def _refusal_cause(exc: BaseException) -> str:
    """Name why a registration was refused without echoing foreign exception text.

    Only this module's own `ArtifactError` messages, which are fixed strings written
    here, are printed in full. Any other exception gives its class, plus the errno
    name for an `OSError`, because its text can carry configuration contents
    (ISSUE-REF; the refusal used to name no cause at all).
    """
    if isinstance(exc, ArtifactError):
        return f"ArtifactError: {exc}"
    name = type(exc).__name__
    code = getattr(exc, "errno", None)
    if isinstance(exc, OSError) and code in errno.errorcode:
        return f"{name} [{errno.errorcode[code]}]"
    return name


def _registration_paths(*paths: Path) -> None:
    for path in paths:
        if not path.is_absolute() or not _safe_path(path) or ".worktrees" in path.parts:
            raise ArtifactError("Invalid registration path")


def _installed_claude(
    documents: dict, generation: Path, artifacts: ClientArtifacts, directory: Path
) -> str:
    """Check recorded version and bytes of the source the local marketplace loads."""
    entries = _claude_entries(documents)
    market = entries["marketplace"] or {}
    if market.get("source") != {"source": "directory", "path": str(generation)}:
        return "source_mismatch"
    rows = entries["installed"]
    expected = json.loads(artifacts.files["artifact.json"])["package_version"]
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("scope") != "user":
        return "registration_missing"
    if rows[0].get("version") != expected:
        return "version_mismatch"
    # Directory marketplaces load in place on the measured native client. Also
    # verify its recorded installation path if a client actually copied the files.
    install_path = Path(rows[0].get("installPath", ""))
    _registration_paths(install_path, generation)
    if not _client_install_path_ok(str(install_path), generation, directory):
        raise ArtifactError("Registration conflict")
    for name, content in artifacts.files.items():
        if name.startswith(PLUGIN + "/"):
            relative = name.removeprefix(PLUGIN + "/")
            if _read(generation / name) != content or (
                install_path.exists() and _read(install_path / relative) != content
            ):
                return "source_mismatch"
    if entries["enabled"] is not True:
        return "registration_missing"
    return "ok"


class _RegistrationTransaction:
    """Reuse the atomic config writer for publication and exact-byte rollback."""

    def __init__(self):
        self.before: dict[Path, tuple[bytes | None, os.stat_result | None]] = {}

    def watch(self, path: Path) -> None:
        if path not in self.before:
            raw = _read(path)
            self.before[path] = (raw, path.stat() if raw is not None else None)

    def publish(self, path: Path, content: str) -> None:
        self.watch(path)
        if _read(path) != content.encode():
            path.parent.mkdir(parents=True, exist_ok=True)
            publish_if_changed(path, content)

    def rollback(self) -> None:
        for path, (content, stat) in reversed(self.before.items()):
            if _read(path) == content:
                continue
            # Do not delete client files/cache directories on failure. Empty JSON
            # registries restore the absence of entries for a first installation.
            codex_integration._replace_config(
                path, content.decode() if content is not None else "{}\n"
            )
            if stat is not None:
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def _disable_claude(
    documents: dict,
    directory: Path,
    transaction: _RegistrationTransaction,
    receipt: dict,
) -> None:
    if not receipt.get("claude"):
        return
    for name, mapping, key in (
        (CLAUDE_FILES[0], "enabledPlugins", CORE_ID),
        (CLAUDE_FILES[0], "extraKnownMarketplaces", MARKETPLACE),
        (CLAUDE_FILES[1], None, MARKETPLACE),
        (CLAUDE_FILES[2], "plugins", CORE_ID),
    ):
        candidate = merge_owned_entry(documents[name], mapping, key)
        if candidate is not None:
            documents[name] = candidate
    receipt.pop("claude")
    for name, value in documents.items():
        transaction.publish(directory / name, _json(value).decode())


def _apply_claude(  # noqa: PLR0913 - explicit transaction inputs
    artifacts: ClientArtifacts,
    state_root: Path,
    directory: Path,
    *,
    binary: str,
    version: str | None,
    isolation: Path | None,
    documents: dict,
    receipt: dict,
    transaction: _RegistrationTransaction,
) -> RegistrationCheck:
    generation = (
        state_root
        / "artifacts"
        / (
            json.loads(artifacts.files["artifact.json"])["package_version"]
            + "-"
            + _digest(_json(artifacts.manifest))
        )
    )
    owned = receipt.get("claude", {})
    if owned and Path(owned["generation"]) == generation:
        if _installed_claude(documents, generation, artifacts, directory) != "ok":
            raise ArtifactError("Modified client registration")
        return RegistrationCheck("claude", owned.get("validation", "not_checked"))
    if generation.exists():
        _verify_generation(generation, artifacts.manifest)
    write_client_artifacts(artifacts, generation)
    measured = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version or "")
    strict = bool(measured and tuple(map(int, measured.groups())) >= (2, 1, 281))
    if strict:
        for target in (generation / PLUGIN, generation):
            _plugin_call(binary, isolation, "validate", str(target), "--strict")
    _preflight_claude(directory, receipt, state_root)
    for name in CLAUDE_FILES:
        transaction.watch(directory / name)
    _plugin_call(binary, isolation, "marketplace", "add", str(generation))
    if owned:
        # The client keeps a copy keyed by plugin version, and `plugin update` does not
        # refresh it when only the bytes changed (the revision in artifact.json changes
        # with every commit), so an owned upgrade replaces the installation.
        _plugin_call(binary, isolation, "uninstall", CORE_ID, "--scope", "user")
    _plugin_call(binary, isolation, "install", CORE_ID, "--scope", "user")
    installed = _claude_documents(directory)
    if _installed_claude(installed, generation, artifacts, directory) != "ok":
        raise ArtifactError("Client verification failed")
    receipt["claude"] = {
        "generation": str(generation),
        "manifest": artifacts.manifest,
        "entry_digest_scheme": CLAUDE_DIGEST_SCHEME,
        "entry_digest": _digest(_json(_claude_semantic_entries(installed))),
        "validation": "ok" if strict else "not_checked",
    }
    return RegistrationCheck("claude", "ok" if strict else "not_checked")


def _preflight_claude(
    directory: Path, receipt: dict, state_root: Path, isolation: Path | None = None
) -> dict:
    if isolation is not None and isolation != directory:
        raise ArtifactError("Mismatched client isolation root")
    documents = _claude_documents(directory)
    # Check the explicitly supplied config root and its user home; no cwd/home
    # discovery is performed. The installer owns project-policy discovery.
    for path in (directory / ".claude.json", directory.parent / ".claude.json"):
        direct = _registration_json(path)
        registrations = [direct, *direct.get("projects", {}).values()]
        if any(
            "ai-harness-core" in entry.get("mcpServers", {}) for entry in registrations
        ):
            raise ArtifactError("Unowned client name")
    _verify_claude_owner(documents, receipt.get("claude", {}), state_root, directory)
    return documents


def _registration_candidate(
    path: Path, block: str, enabled: bool, expected: str | None = None
) -> str:
    change, candidate = _codex_change(path, block if enabled else "")
    if change.state == "conflict" or (enabled and not block):
        raise ArtifactError("Codex conflict")
    if expected is not None and candidate != expected:
        raise ArtifactError("Registration changed during publication")
    return candidate


def _prepare_codex(path, block, enabled, binary, required):
    if enabled and binary is None:
        return None, (
            RegistrationCheck(
                "codex", "client_missing" if required else "skipped", not required
            ),
        )
    return _registration_candidate(path, block, enabled), (
        RegistrationCheck("codex", "ok" if enabled else "disabled"),
    )


def apply_client_registration(  # noqa: PLR0913 - explicit installer paths and client policy
    artifacts: ClientArtifacts,
    state_root: Path,
    claude_config: Path,
    codex_config: Path,
    *,
    clients: list[str] | tuple[str, ...] = ("claude", "codex"),
    enabled: bool = True,
    claude_binary: str | None = None,
    codex_binary: str | None = None,
    claude_version: str | None = None,
    claude_config_dir: Path | None = None,
    required_clients: tuple[str, ...] = (),
) -> RegistrationResult:
    """Stage, validate and switch only owned client entries, using explicit paths.

    ``claude_version`` must be measured by the calling installer, not inferred from
    help output. Unknown/older versions yield ``not_checked`` for strict validation.
    ``claude_config_dir`` overrides the runner environment only when supplied;
    tests must supply an isolated directory matching ``claude_config``. A real
    installer may inherit its caller's environment with None. Calls must be
    serialized with respect to other installers and client writers.
    """
    transaction = _RegistrationTransaction()
    checks = []
    try:
        load_policy({"clients": list(clients), "enabled": enabled})
        _registration_paths(state_root, claude_config, codex_config)
        if enabled:
            _artifact_runtime(artifacts)
        receipt_path = state_root / "registration.json"
        receipt = _registration_json(receipt_path)
        documents = None
        if "claude" in clients:
            documents = _preflight_claude(
                claude_config, receipt, state_root, claude_config_dir
            )
        candidate, codex_checks = (
            _prepare_codex(
                codex_config,
                artifacts.codex_block,
                enabled,
                codex_binary,
                "codex" in required_clients,
            )
            if "codex" in clients
            else (None, ())
        )
        if documents is not None:
            if not enabled:
                _disable_claude(documents, claude_config, transaction, receipt)
                checks.append(RegistrationCheck("claude", "disabled"))
            elif claude_binary is None:
                checks.append(
                    RegistrationCheck(
                        "claude",
                        "client_missing" if "claude" in required_clients else "skipped",
                        "claude" not in required_clients,
                    )
                )
            else:
                checks.append(
                    _apply_claude(
                        artifacts,
                        state_root,
                        claude_config,
                        binary=claude_binary,
                        version=claude_version,
                        isolation=claude_config_dir,
                        documents=documents,
                        receipt=receipt,
                        transaction=transaction,
                    )
                )
        if candidate is not None:
            transaction.publish(
                codex_config,
                _registration_candidate(
                    codex_config, artifacts.codex_block, enabled, candidate
                ),
            )
        checks.extend(codex_checks)
        if receipt or _read(receipt_path) is not None:
            transaction.publish(receipt_path, _json(receipt).decode())
    except (
        ArtifactError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        plugins.PluginInstallError,
        LinkError,
        subprocess.SubprocessError,
    ) as exc:
        cause = _refusal_cause(exc)
        try:
            transaction.rollback()
        except (OSError, LinkError) as rollback_exc:
            return RegistrationResult(
                (
                    RegistrationCheck(
                        "registration",
                        "rollback_failed",
                        False,
                        f"{cause}; rollback: {_refusal_cause(rollback_exc)}",
                    ),
                )
            )
        return RegistrationResult(
            (RegistrationCheck("registration", "registration_refused", False, cause),)
        )
    return RegistrationResult(tuple(checks))


def _doctor_claude(
    artifacts, state_root, claude_config, enabled
) -> list[RegistrationCheck]:
    checks = []
    receipt = _registration_json(state_root / "registration.json")
    documents = _claude_documents(claude_config)
    owned = receipt.get("claude", {})
    if owned and not Path(owned["generation"]).is_relative_to(state_root / "artifacts"):
        raise ArtifactError("Registration conflict")
    if enabled:
        code = (
            _installed_claude(
                documents, Path(owned["generation"]), artifacts, claude_config
            )
            if owned
            else "registration_missing"
        )
        checks.append(RegistrationCheck("claude", code, code == "ok"))
        if owned and owned["manifest"] != artifacts.manifest:
            checks.append(RegistrationCheck("claude", "source_mismatch", False))
    elif owned:
        checks.append(RegistrationCheck("claude", "registration_present", False))
    try:
        _verify_claude_owner(documents, owned, state_root, claude_config)
    except ArtifactError:
        checks.append(RegistrationCheck("claude", "source_mismatch", False))
    return checks


def _doctor_codex(artifacts, codex_config, enabled, runtime) -> list[RegistrationCheck]:
    checks = []
    raw = _read(codex_config)
    _, block = _split_owned_block(raw.decode() if raw else "")
    expected = artifacts.codex_block if enabled else ""
    code = (
        "ok"
        if block == expected
        else "source_mismatch"
        if block
        else "registration_missing"
    )
    checks.append(RegistrationCheck("codex", code, code == "ok"))
    if block and enabled:
        metadata = json.loads(block.splitlines()[1].removeprefix("# "))
        if metadata["package_version"] != runtime.package_version:
            checks.append(RegistrationCheck("codex", "version_mismatch", False))
    return checks


def doctor_client_registration(  # noqa: PLR0913 - explicit installer paths and client policy
    artifacts: ClientArtifacts,
    state_root: Path,
    claude_config: Path,
    codex_config: Path,
    *,
    clients: list[str] | tuple[str, ...] = ("claude", "codex"),
    enabled: bool = True,
) -> RegistrationResult:
    """Read explicit paths only: never run a client, repair or publish a file."""
    checks = []
    try:
        _registration_paths(state_root, claude_config, codex_config)
        runtime = _artifact_runtime(artifacts) if enabled else None
        if runtime and not Path(runtime.interpreter).is_file():
            checks.append(RegistrationCheck("runtime", "runtime_missing", False))
        load_policy({"clients": list(clients), "enabled": enabled})
        for client in clients:
            try:
                if client == "claude":
                    checks.extend(
                        _doctor_claude(artifacts, state_root, claude_config, enabled)
                    )
                elif client == "codex":
                    checks.extend(
                        _doctor_codex(artifacts, codex_config, enabled, runtime)
                    )
            except (ArtifactError, OSError, ValueError, KeyError, TypeError):
                checks.append(RegistrationCheck(client, "registration_modified", False))
    except (ArtifactError, OSError, ValueError, KeyError, TypeError):
        checks.append(RegistrationCheck("registration", "registration_modified", False))
    return RegistrationResult(tuple(checks))
