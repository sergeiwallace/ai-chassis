"""Fail closed until ``bd`` advertises atomic arbitrary-metadata mutations.

This is intentionally a rollout gate, not a wrapper.  Performing a business
mutation and then a second metadata-only write would violate D-12's atomicity
contract, so an unsupported ``bd`` release must block rather than degrade.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

REQUIRED_MUTATORS = frozenset(
    {"create", "update", "claim", "unclaim", "close", "reopen", "dependency"}
)
_VERSION = re.compile(r"\bbd version (?P<version>\d+(?:\.\d+)+)\b", re.IGNORECASE)
_MUTATORS = re.compile(r"^Supported atomic mutations:\s*(?P<values>.+)$", re.MULTILINE)
_METADATA = re.compile(r"^Supported atomic metadata:\s*arbitrary\b", re.MULTILINE)


@dataclass(frozen=True)
class CapabilityResult:
    """The checked-in minimum evidence required to unblock D-12 rollout."""

    version: str | None
    problems: tuple[str, ...]

    @property
    def supported(self) -> bool:
        return not self.problems


def assess_capability(version_output: str, batch_help: str) -> CapabilityResult:
    """Assess only official, explicit capability declarations from ``bd`` output."""
    match = _VERSION.search(version_output)
    version = match.group("version") if match else None
    problems: list[str] = []
    if version is None:
        problems.append("could not determine an installed bd version")

    mutators_match = _MUTATORS.search(batch_help)
    if not mutators_match:
        problems.append("bd does not advertise supported atomic mutations")
    else:
        actual = {
            value.strip().lower()
            for value in mutators_match.group("values").split(",")
            if value.strip()
        }
        missing = sorted(REQUIRED_MUTATORS - actual)
        if missing:
            problems.append(
                "bd atomic mutation coverage is missing: " + ", ".join(missing)
            )

    if not _METADATA.search(batch_help):
        problems.append("bd does not advertise arbitrary atomic metadata support")
    return CapabilityResult(version, tuple(problems))


def _run(command: list[str]) -> str:
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    """Print a rollout verdict from the installed ``bd`` capability declaration."""
    bd = (argv or sys.argv[1:] or ["bd"])[0]
    try:
        result = assess_capability(_run([bd, "version"]), _run([bd, "batch", "--help"]))
    except (OSError, RuntimeError) as exc:
        print(f"beads-atomic-metadata: BLOCKED — {exc}", file=sys.stderr)
        return 2

    if result.supported:
        print(f"beads-atomic-metadata: READY — bd {result.version}")
        return 0
    print(
        "beads-atomic-metadata: BLOCKED"
        + (f" — bd {result.version}" if result.version else "")
        + "; "
        + "; ".join(result.problems),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
