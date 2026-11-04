"""The shared call-time delegation command target from the fleet's billing registry."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ai_chassis import config as harness_config
from ai_chassis import machine, machine_registry

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The Codex family the maintainer named for a subscription profile's repair ("gpt sol (latest
#: version available)", ISSUE-REF): the sol tier, never the human-gated frontier tier.
SOL_FAMILY = "sol"
#: The catalog route cx itself resolves a family on for a subscription (OAuth) profile.
CODEX_SUBSCRIPTION_ROUTE = "codex-oauth"
#: One bounded refresh-on-miss inside the resolver (its own bound is 25 s) plus start-up.
CATALOG_TIMEOUT_SECONDS = 60
_RESOLVED = re.compile(r"(?:^|\s)resolved=(\S+)")


def default_delegation_target(identity: str | None = None) -> str:
    """Use the canonical billing route; an unproven route cannot authorize Claude.

    Identity follows the goal waker's environment-then-marker order, using the shared
    marker reader. No hostname detection or default machine can authorize API billing.
    The Codex target invokes the /codex skill; Opus remains a Claude persona argument.
    """
    try:
        registry = harness_config.load_config(
            REPO_ROOT / "projects-wide-session-config"
        ).registry
        if registry is not None:
            if identity is None:
                identity = (
                    os.environ.get(machine.MACHINE_ENV_VAR, "").strip()
                    or machine.marker_identity()[0]
                )
            billing = machine_registry.resolve(registry, identity).attributes
            if billing.get("billing_mode") == "api":
                return "opus"
    except (OSError, ValueError):
        pass
    return "/codex"


def latest_sol_model() -> str:
    """The newest sol id on the subscription route, read now from cx's own resolver.

    ``model-catalog resolve`` is the one definition of "latest" every launch surface
    shares. When it refuses, the bare family token is returned: cx resolves a family at
    launch itself, so the command stays correct without a remembered id.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "model_catalog.py"),
                "resolve",
                "--route",
                CODEX_SUBSCRIPTION_ROUTE,
                "--family",
                SOL_FAMILY,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=CATALOG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return SOL_FAMILY
    found = _RESOLVED.search(result.stdout or "") if result.returncode == 0 else None
    model = found.group(1) if found else ""
    return model if model.endswith(f"-{SOL_FAMILY}") else SOL_FAMILY


def bug_fix_delegation(identity: str | None = None) -> str:
    """The ``/delegate ... /bug-fix`` command for this machine profile's billing route.

    A per-token (API) profile delegates to Opus 5.5; a subscription profile, or one whose
    route cannot be proven, delegates to Codex on the latest sol model (ISSUE-REF AC-4).
    """
    target = default_delegation_target(identity)
    if target == "opus":
        return "/delegate opus 5.5 /bug-fix"
    return f"/delegate {target} {latest_sol_model()} /bug-fix"
