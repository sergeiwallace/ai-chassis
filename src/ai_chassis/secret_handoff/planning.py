"""Pure request validation shared by local and MCP callers."""

from pathlib import Path
from importlib.resources import files
import re
import tomllib

from ai_chassis.machine_registry import Registry, resolve
from .models import HandoffError, INVALID, Plan, Request, SourceDescription

BACKENDS = ("pbcopy", "wayland", "xclip", "xsel")
SOURCES = ("ssh-script", "cli-token", "file", "prompt", "fd")
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
METADATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ROOT = Path(__file__).resolve().parents[3]


def invalid() -> None:
    raise HandoffError(2, INVALID)


def load_catalog(path: Path | None = None) -> dict:
    try:
        source = (
            path
            if path is not None
            else files("ai_chassis").joinpath("defaults/secret-handoff.toml")
        )
        catalog = tomllib.loads(source.read_text("utf-8"))
        if set(catalog) != {"schema_version", "target", "hosts", "recipes"}:
            invalid()
        if catalog["schema_version"] != 1:
            invalid()
        for recipe in catalog["recipes"].values():
            if recipe["source"] not in SOURCES:
                invalid()
            if len(recipe["names"]) != len(recipe["prefixes"]):
                invalid()
        return catalog
    except (OSError, ValueError, KeyError, TypeError):
        raise HandoffError(2, INVALID) from None


def profile_enabled(registry: Registry, document: dict, machine: str | None) -> bool:
    result = resolve(registry, machine)
    if result.unknown:
        raise HandoffError(3, "secret-handoff: machine profile unavailable.")
    flag = document["machine"][result.canonical].get("secret_handoff", False)
    if type(flag) is not bool:
        invalid()
    return flag


def _metadata(request: Request, catalog: dict) -> tuple[dict, str, str, str]:
    recipe = catalog["recipes"].get(request.recipe)
    if recipe is None:
        invalid()
    project = request.project or catalog["target"]["project"]
    config = request.config or catalog["target"]["config"]
    operation = request.operation or recipe.get("operation")
    if any(not METADATA.fullmatch(v) for v in (project, config)):
        invalid()
    if operation not in ("add", "update"):
        invalid()
    if request.backend is not None and request.backend not in BACKENDS:
        invalid()
    return recipe, project, config, operation


def source_description(recipe: dict, names: tuple[str, ...]) -> SourceDescription:
    requirements = {
        "ssh-script": ("ssh",),
        "cli-token": ("registered-issuer",),
        "file": ("private-file",),
        "fd": ("private-descriptor",),
        "prompt": ("controlling-tty",),
    }
    return SourceDescription(
        names,
        requirements[recipe["source"]],
        "credential-issuance"
        if recipe["source"] in ("ssh-script", "cli-token")
        else "read",
    )


def build_plan(request: Request, catalog: dict, *, enabled: bool) -> Plan:
    recipe, project, config, operation = _metadata(request, catalog)
    names = tuple(recipe["names"])
    if names:
        if request.name is not None and request.name not in names:
            invalid()
    elif request.name and NAME.fullmatch(request.name):
        names = (request.name,)
    else:
        invalid()
    argv = [
        "secret-handoff",
        "copy",
        request.recipe,
        "--project",
        project,
        "--config",
        config,
        "--operation",
        operation,
    ]
    if recipe["source"] == "ssh-script":
        host_id = request.host_id or recipe["host_id"]
        host = catalog["hosts"].get(host_id)
        if host is None:
            invalid()
        root = request.repo_dir or host["roots"][0]
        if root not in host["roots"]:
            invalid()
        argv.extend(("--host", host_id, "--repo-dir", root))
    elif request.host_id is not None or request.repo_dir is not None:
        invalid()
    if not recipe["names"]:
        argv.extend(("--name", names[0]))
    if request.backend:
        argv.extend(("--backend", request.backend))
    return Plan(
        1,
        enabled,
        request.recipe,
        recipe["source"],
        project,
        config,
        operation,
        names,
        len(names),
        tuple(argv),
        True,
        () if enabled else ("profile_disabled",),
    )
