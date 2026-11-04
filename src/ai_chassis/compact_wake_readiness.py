"""Is the auto-compact / auto-wake system OPERATIONAL (IN-135, ISSUE-REF)?

install.sh's header-less "Final step a2", now the core ``compact-wake-readiness`` step
(ISSUE-REF T-3.6 batch T6-1c). ``/install-harness`` must leave the compact/wake system
installed, configured AND operational, and those are three claims. The hooks-manifest step
writes settings.json FROM the manifest, so "declared implies wired" holds by construction and
proves nothing about whether any of it can run. The supervisor hooks swallow every failure
(correct for a sensor, which must not affect a turn), so a supervisor that never ran looks like
one that ran cleanly: measured on example-client-host-mac 2026-09-23, bare ``python3`` was 3.9.6 and could not
import the module at all (ISSUE-REF). This step makes that silent state visible, which is why it
probes rather than greps.

Three checks, in order of what they can catch:

1. every hook script this machine's manifest scope declares exists, is non-empty and parses (a
   0-byte script run as ``bash script`` exits 0 having done nothing, G111/ISSUE-REF);
2. the supervisor entrypoint runs under the interpreter the hooks resolve;
3. the compact boundary is wired: PreCompact and the SessionStart resume injection. These two
   are named, not derived from the manifest, because they ARE the feature.

Warn, not fail, with one exception: check 1's verdict that a declared hook CANNOT RUN is a
``[FAIL]`` on stderr. A degraded compact/wake system is not a broken machine, but a hook that
cannot be parsed runs its first half and dies (bash parses incrementally): a token-warming hook sat
on main in that state on four machines with one ``[warn]`` line saying so (ISSUE-REF).
Severity only: the exit policy is unchanged (ISSUE-REF), so the step exits 0 on every arm.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .hooks_manifest import skill_capability_states
from .skills_install import REGISTRY_NAME

#: The bash every stock Mac has (3.2.57), parsed against beside PATH's, which may be a newer one.
FALLBACK_BASH = "/bin/bash"
#: `bash -n` and `--help` answer at once; only a wedged process reaches either bound.
PARSE_SECONDS = 60
PROBE_SECONDS = 60
BOUNDARY = ("precompact-safety.sh", "resume-inject.sh")


def check(
    manifest: Path, settings: Path, root: Path, python: str, machine: str
) -> None:
    """Print the readiness section. ``python`` is the fleet-floor interpreter's answer, empty when
    there is none, which skips every check as install.sh's gate did."""
    print()
    print("--- Auto-compact / auto-wake readiness ---")
    if not (python and manifest.is_file()):
        print(
            "  [warn] cannot verify the auto-compact/auto-wake system: no python or no "
            f"{manifest}",
            file=sys.stderr,
        )
        return
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8"))
        hooks = declared["hooks"]
    except Exception as exc:  # noqa: BLE001 - a manifest we cannot read is a finding, not a skip
        print(f"  [warn] {manifest} is unreadable: {exc}")
        return
    # The hooks writer wires a `skill:<name>` hook only where the skill install registry beside the
    # manifest enables that skill, so the check asks the same registry (ISSUE-REF).
    gates = skill_capability_states(declared, manifest.parent / REGISTRY_NAME, machine)
    _hook_scripts(hooks, machine, gates)
    _supervisor(root)
    _compact_boundary(settings)


def _applies(hook: dict, machine: str) -> bool:
    """`machines` is the literal string "all" or a list of machine names.

    Treating "all" as a list is a substring test that is False for every machine: it reported
    every hook out of scope while twelve of them were demonstrably wired.
    """
    declared = hook.get("machines")
    return declared is None or declared == "all" or machine in declared


def _parsers() -> list[tuple[str, str]]:
    """Every distinct bash that could run a hook, as (name, resolved path).

    The hooks carry `#!/usr/bin/env bash`, so PATH decides, and a Mac with a Homebrew bash first
    on PATH resolves 5.x while 3.2.57 is the only bash on a stock one. Parsing under PATH's bash
    alone would have PASSED the ISSUE-REF hook on a provisioned Mac.
    """
    parsers: list[tuple[str, str]] = []
    for candidate in (shutil.which("bash"), FALLBACK_BASH):
        if candidate and Path(candidate).exists():
            resolved = str(Path(candidate).resolve())
            if resolved not in [found for _, found in parsers]:
                parsers.append((candidate, resolved))
    return parsers


def _parse_failure(parser: str, target: Path) -> str | None:
    """The first line of ``parser -n target``'s diagnostic when it refuses, else None."""
    try:
        parsed = subprocess.run(
            [parser, "-n", str(target)],
            capture_output=True,
            text=True,
            check=False,
            timeout=PARSE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {PARSE_SECONDS} s"
    if parsed.returncode == 0:
        return None
    return (parsed.stderr.strip().splitlines() or ["(no diagnostic)"])[0]


def _broken(name: str, target: Path, parsers: list[tuple[str, str]]) -> str | None:
    if not target.exists():
        return f"{name}: not installed at {target}"
    if target.stat().st_size == 0:
        return f"{name}: 0 bytes, so running it exits 0 having done nothing"
    if not name.endswith(".sh"):
        return None
    if not parsers:
        return f"{name}: no bash on this host, so nothing can parse or run it"
    for parser, _ in parsers:
        failure = _parse_failure(parser, target)
        if failure is not None:
            return f"{name}: does not parse under {parser} -- {failure}"
    return None


def _gated_off(hook: dict, gates: dict) -> str | None:
    """The `skill:<name>` capability that keeps this hook unwired on the machine, else None."""
    capability = hook.get("capability")
    if not isinstance(capability, str) or capability not in gates:
        return None
    return None if gates[capability].get("state") == "enabled" else capability


def _hook_scripts(hooks: list[dict], machine: str, gates: dict) -> None:
    parsers = _parsers()
    hook_dir = Path.home() / ".claude" / "hooks"
    declared = [
        (hook, _gated_off(hook, gates)) for hook in hooks if _applies(hook, machine)
    ]
    # A script some wired hook still declares is checked, whatever else gates it.
    scripts = {Path(hook["script"]).name for hook, gate in declared if gate is None}
    skipped = {
        Path(hook["script"]).name: gate
        for hook, gate in declared
        if gate is not None and Path(hook["script"]).name not in scripts
    }
    _report_skipped(skipped, machine)
    broken = [
        finding
        for name in sorted(scripts)
        if (finding := _broken(name, hook_dir / name, parsers)) is not None
    ]
    if broken:
        print(
            f"  [FAIL] {len(broken)} of {len(scripts)} hook scripts declared for {machine}"
            " CANNOT RUN:",
            file=sys.stderr,
        )
        for line in broken:
            print(f"         {line}", file=sys.stderr)
        return
    names = ", ".join(name for name, _ in parsers) or "no bash on this host"
    print(
        f"  [ok]  {len(scripts)} hook scripts declared for {machine} are installed and parse"
        f" ({names})"
    )


def _report_skipped(skipped: dict[str, str], machine: str) -> None:
    """One line naming each gated-off script and its gate: not checked, because not wired."""
    if not skipped:
        return
    gates = ", ".join(sorted(set(skipped.values())))
    print(
        f"  [skip] {len(skipped)} hook scripts gated off for {machine}, not checked: "
        f"{', '.join(sorted(skipped))} ({gates} is disabled for {machine} in "
        f"{REGISTRY_NAME})"
    )


def _supervisor(root: Path) -> None:
    """The supervisor's --help parses argv and exits, touching no state; what it proves is that
    the module IMPORTS, which is the half that fails on an old interpreter."""
    interpreter = next(
        (
            candidate
            for candidate in (
                root / ".venv/bin/python",
                root / ".venv/Scripts/python.exe",
            )
            if candidate.exists()
        ),
        None,
    )
    if interpreter is None:
        print(
            f"  [warn] no {root}/.venv interpreter, so the supervisor falls back to bare python3;"
            " an interpreter below the repo floor cannot import it (ISSUE-REF)"
        )
        return
    try:
        probe = subprocess.run(
            [str(interpreter), "-m", "ai_chassis.context_supervisor", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_SECONDS,
            # Inherit and override, not replace: the hooks set only PYTHONPATH too, and a
            # hand-built environment would probe a different process than the one that runs.
            env={**os.environ, "PYTHONPATH": str(root / "src")},
        )
        returncode, stdout, stderr = probe.returncode, probe.stdout, probe.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        returncode, stdout, stderr = 1, "", str(exc)
    # Assert on OUTPUT, not the status: a truncated interpreter exits 0 printing nothing.
    if returncode == 0 and "usage:" in stdout:
        print(f"  [ok]  context supervisor runs ({interpreter.name})")
        return
    first = (stderr or stdout).strip().splitlines()[-1:] or ["no output"]
    print(
        f"  [warn] the context supervisor does NOT run under {interpreter}: {first[0]}"
    )
    print(
        "         the compact/wake hooks swallow this failure by design, so nothing else"
        " would report it"
    )


def _compact_boundary(settings: Path) -> None:
    try:
        live = json.dumps(
            json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        )
    except Exception as exc:  # noqa: BLE001 - the shell block's own catch
        print(f"  [warn] cannot read {settings} to confirm the compact boundary: {exc}")
        return
    present = [name for name in BOUNDARY if re.search(re.escape(name), live)]
    if len(present) == len(BOUNDARY):
        print(
            "  [ok]  compact boundary wired (PreCompact safety + SessionStart resume injection)"
        )
        return
    absent = sorted(set(BOUNDARY) - set(present))
    print(
        f"  [warn] the compact boundary is incomplete -- not wired: {', '.join(absent)}."
        " A compact would lose the session's hand-off."
    )
