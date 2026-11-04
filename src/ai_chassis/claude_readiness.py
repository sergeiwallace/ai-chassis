"""Claude Code readiness: is Claude Code launchable AND configured (IN-134, ISSUE-REF)?

install.sh's header-less "Final step", now the core ``claude-readiness`` step (ISSUE-REF
T-3.6 batch T6-1c). It runs after every other install step, on purpose: the auth route it reads
is written by the bedrock-auth step and the settings merge, so a check placed next to the
claude-code step would read the file before it was written and report a false failure.

Verify by artifact, not by exit code: it runs the binary and reads the resulting settings file.
It never fails the install. A machine with no Claude Code still wants its hooks and skills, so
every outcome is ``[ok]`` or ``[warn]``. ``--version`` parses argv and exits without touching
auth, so it proves LAUNCHABLE only; a real model call would cost tokens and make every install
depend on network and credential state.

Configured means the auth route this machine's profile requires is in ``settings.json``. Only the
Bedrock switch and the region are printed, both non-secret; the file can hold credentials, so no
other key is. Whether the profile requires the route is the bedrock-auth step's own verdict,
handed in, never re-derived: a second copy of that membership rule once left example-project-ec2 in one
list and out of another, and the install completed rc=0 with the agent unable to authenticate.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ai_chassis import dependency_bootstrap
from ai_chassis.install_sequence import BEDROCK_AUTH_ENV

#: install.sh's `_run_bounded 60 5 claude --version`: the bound, and the grace before the KILL.
VERSION_SECONDS = 60
VERSION_GRACE_SECONDS = 5
BEDROCK_KEYS = ("CLAUDE_CODE_USE_BEDROCK", "AWS_REGION")


def check(home: Path, python: str, bedrock_auth: str | None, machine: str) -> None:
    """Print the readiness section. ``python`` is the fleet-floor interpreter's answer (empty when
    there is none, as install.sh's gate read it); ``bedrock_auth`` is the bedrock-auth step's
    ``_bedrock_api_auth_enabled``, or None when nobody handed it in."""
    print()
    print("--- Claude Code readiness ---")
    _launchable()
    _configured(home / ".claude" / "settings.json", python, bedrock_auth, machine)


def _launchable() -> None:
    claude = shutil.which("claude")
    if claude is None:
        # The remedy names no install route: installing the agent is the private installer's
        # act (IN-74), so the public core points back at the install that does it.
        print(
            "  [warn] claude is NOT on PATH. Claude Code is configured but cannot be started.\n"
            "         Install Claude Code, then re-run install.sh.",
            file=sys.stderr,
        )
        return
    result = dependency_bootstrap.run_bounded(
        [claude, "--version"],
        seconds=VERSION_SECONDS,
        grace=VERSION_GRACE_SECONDS,
        capture=True,
    )
    # The shell's `"$(... 2>&1)"`: both streams, trailing newlines dropped.
    version = (result.stdout + result.stderr).rstrip("\n")
    if result.returncode == 0:
        print(f"  [ok]  launchable: {claude} ({version})")
        return
    print(
        f"  [warn] claude is on PATH at {claude} but --version failed:\n         {version}",
        file=sys.stderr,
    )


def _configured(
    settings: Path, python: str, bedrock_auth: str | None, machine: str
) -> None:
    if not (settings.is_file() and python):
        print(f"  [warn] no {settings} to verify", file=sys.stderr)
        return
    try:
        env = json.loads(settings.read_text(encoding="utf-8")).get("env") or {}
    except Exception as exc:  # noqa: BLE001 - the shell block's own catch: any unreadable file is a finding
        print(f"  [warn] {settings} is unparseable: {exc}")
        return
    if bedrock_auth is None:
        # Never a default of "not required" (G121): that default once made this line contradict
        # the Bedrock step on every machine.
        print(
            f"  [warn] configured: cannot tell whether {machine} needs the Bedrock route "
            f"({BEDROCK_AUTH_ENV} is not set; install.sh and the full apply hand in the "
            "bedrock-auth step's answer)"
        )
        return
    if bedrock_auth != "1":
        print(f"  [ok]  configured: {machine} does not require the Bedrock auth route")
        return
    missing = [key for key in BEDROCK_KEYS if not env.get(key)]
    if missing:
        print(
            f"  [warn] configured: {machine} needs the Bedrock route but settings.json is "
            f"missing {', '.join(missing)}"
        )
        return
    print(
        "  [ok]  configured: Bedrock route present "
        f"(CLAUDE_CODE_USE_BEDROCK={env['CLAUDE_CODE_USE_BEDROCK']}, "
        f"AWS_REGION={env['AWS_REGION']})"
    )
