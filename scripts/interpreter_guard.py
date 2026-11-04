"""Refuse to run a venv-dependent script under an interpreter that cannot run it.

THE DEFECT CLASS THIS EXISTS TO STOP: a MEASUREMENT failure wearing a VERDICT's
clothes. `generate_share_surface_manifest.py` runs the exported pytest suite as the
oracle for ~60 manifest rows. Launched as a bare `python3
scripts/generate_share_surface_manifest.py --check`, that subprocess died during
collection and the last line of its output was folded into a DOMAIN sentence:

    ERROR: runtime test closure could not identify failing portable test modules:
    E   ModuleNotFoundError: No module named 'hypothesis'

Read as written, that says the manifest derivation is broken. It was not. `hypothesis`
is correctly declared in pyproject's `dev` group, and `uv run python <the same script>
--check` returned 0 on the same tree in the same second. The only fault was the
interpreter the operator used, and the message named everything except that -- so the
delegating session nearly filed a defect against a healthy file.

WHY A STARTUP GUARD AND NOT MERELY BETTER WORDING AT THE FOLD SITE. Both, and in that
order. Wording at the fold site can only speak AFTER the derivation has spent 2-6
minutes materialising an export and running a suite that was never going to import. The
guard answers at startup, before that cost, and at the one moment when the answer is
still unambiguous: nothing has been measured yet, so nothing can be mistaken for a
measurement.

THE THREE THINGS THE MESSAGE MUST DO, none of which the old one did:
  1. Name the fault as an environment/invocation fault, not a finding about the tree.
  2. Explicitly DENY the wrong inference the old wording invited. An operator who has
     just read "could not identify failing portable test modules" is already reasoning
     about the closure; leaving that unaddressed lets them carry on.
  3. Hand over the exact invocation that converts the failure into a real verdict.

Modelled on `bd_prepush_divergence.py`'s `_report_measurement_failure`, which fixed this
same shape for the divergence gate after an expired `bd export` budget printed the
divergence REJECTION. That fix stayed local to one script, and so did the equivalent fix
in the sibling managed-platform repo's `credential_diagnostics.py`; nothing generalised either, so
the next script repeated it. This helper is the generalisation.

WHICH SCRIPTS ENROL IS DATA, NOT CODE: `scripts/interpreter-requirements.toml`. Adding a
script is a config edit, so the next script to grow a venv dependency does not have to
re-derive any of the reasoning above.

A NOTE ON WHAT THIS DELIBERATELY DOES NOT DO. It does not probe a foreign interpreter,
because it does not need to: the enrolled script hands `sys.executable` to its own
subprocess, so the interpreter running this guard IS the interpreter under test. An
in-process `find_spec` is therefore the exact oracle, and it keeps the guard free of any
subprocess of its own.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import TextIO

# The environment-fault exit code, deliberately distinct from every DOMAIN exit code a
# calling script defines. `bd_prepush_divergence.py` established that "could not
# measure" must be distinguishable from "measured, and here is the finding" by exit code
# as well as by wording. A caller whose domain codes already occupy 1 (stale artifact)
# and 2 (the derivation itself failed) therefore needs a third, not a reuse of either.
ENVIRONMENT_FAULT_EXIT = 3

REQUIREMENTS_CONFIG = "interpreter-requirements.toml"

# pytest exit codes that mean "the run never produced a verdict" rather than "a test
# failed": 3 = internal error, 4 = usage/collection error, 5 = no tests collected.
PYTEST_NONVERDICT_EXITS = frozenset({3, 4, 5})

# Substrings that identify a subprocess failure as an ENVIRONMENT fault. Matched only in
# a branch that has already failed to find any test-level verdict, so a test whose own
# output mentions ImportError cannot be misread: by then there is no verdict to confuse
# it with.
_ENVIRONMENT_MARKERS = (
    "ModuleNotFoundError",
    "No module named",
    "ImportError",
    "error: unrecognized arguments",
    "unrecognized option",
)


class InterpreterRequirementsError(Exception):
    """The requirements config is unusable -- a repository fault, not an environment one.

    Kept distinct from `InterpreterEnvironmentFault` on purpose: a malformed config is
    exactly the kind of domain problem this module exists to stop being confused with an
    environment one, and it would be self-defeating to report it as "wrong interpreter".
    """


class InterpreterEnvironmentFault(Exception):
    """This interpreter cannot run this script. NOT a finding about the repository.

    Carried as its own exception type precisely so it cannot be caught by, or reported
    through, a handler that frames its detail as a domain error. That fold is the defect;
    a distinct type makes the fold a type error rather than a wording choice.
    """

    def __init__(
        self,
        *,
        script: str,
        missing: Sequence[str] = (),
        reason: str = "",
        detail: str = "",
    ) -> None:
        self.script = script
        self.missing = tuple(missing)
        self.reason = reason
        self.detail = detail
        summary = detail or f"missing {', '.join(self.missing) or 'required modules'}"
        super().__init__(f"{script}: {summary}")


@dataclass(frozen=True)
class Requirement:
    """What one enrolled script needs, and why it needs it."""

    script: str
    modules: tuple[str, ...]
    reason: str


def _config_path(scripts_dir: Path | None = None) -> Path:
    return (scripts_dir or Path(__file__).resolve().parent) / REQUIREMENTS_CONFIG


def load_requirements(scripts_dir: Path | None = None) -> dict[str, Requirement]:
    """Parse the enrolment config, rejecting anything that would weaken the message.

    `reason` is mandatory and non-empty because it is printed verbatim: a guard that says
    only "hypothesis is missing" leaves the operator to work out whether that is their
    problem or the repository's, which is the ambiguity this whole module exists to
    remove. Validating it here means a future enrolment cannot quietly degrade the
    failure text.
    """
    path = _config_path(scripts_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # ABSENT config means "no enrolments", not a crash. This module gets copied into
        # synthesized fixture repositories and into the portable export alongside the
        # scripts that call it, and a guard that killed an enrolled script wherever its
        # config did not travel would be a worse failure than the one it prevents.
        #
        # The obvious objection -- that deleting the config silently disables the guard --
        # is closed where it actually matters, by a test that pins this repository's
        # config to the enrolments it must carry. Tolerated absence plus a pinning test is
        # safe; tolerated absence alone would not be.
        return {}
    except OSError as exc:
        raise InterpreterRequirementsError(f"{path} is not readable: {exc}") from exc

    # A config that EXISTS but is malformed is strict, and stays strict: that is a
    # repository fault, and passing it off as "no enrolments" would disable the guard for
    # the one reason nobody would notice.
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InterpreterRequirementsError(f"{path} is not valid TOML: {exc}") from exc

    if raw.get("version") != 1:
        raise InterpreterRequirementsError(f"{path} must declare version = 1")
    table = raw.get("script")
    if not isinstance(table, dict):
        raise InterpreterRequirementsError(f"{path} must declare a [script] table")

    requirements: dict[str, Requirement] = {}
    for name, entry in table.items():
        label = f'{path}: [script."{name}"]'
        if not isinstance(entry, dict):
            raise InterpreterRequirementsError(f"{label} is not a table")
        modules = entry.get("modules")
        if (
            not isinstance(modules, list)
            or not modules
            or not all(isinstance(module, str) and module.strip() for module in modules)
        ):
            raise InterpreterRequirementsError(
                f"{label} needs a non-empty list of module names"
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise InterpreterRequirementsError(f"{label} needs a non-empty reason")
        requirements[name] = Requirement(
            script=name,
            modules=tuple(module.strip() for module in modules),
            reason=" ".join(reason.split()),
        )
    return requirements


def missing_modules(
    modules: Iterable[str],
    *,
    finder: Callable[[str], ModuleSpec | None] | None = None,
) -> tuple[str, ...]:
    """Which of `modules` this interpreter cannot import.

    `find_spec` rather than a real import: importing executes module code, and a
    heavyweight dependency's side effects have no business running inside a guard. It is
    also the only check that stays correct for a module the caller never imports itself
    -- the generator needs `hypothesis` present for the SUBPROCESS suite it spawns, not
    for its own namespace, so an `import hypothesis` here would be a fiction that
    happened to work.
    """
    find = finder or importlib.util.find_spec
    absent: list[str] = []
    for module in modules:
        try:
            if find(module) is None:
                absent.append(module)
        except (ImportError, ValueError):
            # A module whose PARENT package is absent raises instead of returning None.
            absent.append(module)
    return tuple(absent)


def require(
    script: str,
    *,
    scripts_dir: Path | None = None,
    finder: Callable[[str], ModuleSpec | None] | None = None,
) -> None:
    """Raise `InterpreterEnvironmentFault` when this interpreter cannot run `script`.

    A script with no config entry is not enrolled and passes. Enrolment is opt-in so
    that importing this helper never invents a requirement nobody declared.
    """
    requirement = load_requirements(scripts_dir).get(script)
    if requirement is None:
        return
    absent = missing_modules(requirement.modules, finder=finder)
    if absent:
        raise InterpreterEnvironmentFault(
            script=script,
            missing=absent,
            reason=requirement.reason,
        )


def environment_shaped(output: str, returncode: int | None = None) -> str | None:
    """The line proving a subprocess failed for an ENVIRONMENT reason, or None.

    The second layer, for the case the startup guard cannot cover: a dependency that
    resolves for THIS interpreter but not inside the subprocess, or one the config does
    not yet name. Call it only once a caller has failed to extract any test-level
    verdict, so that "no verdict, and the output says ModuleNotFoundError" resolves to an
    environment fault instead of being folded into a domain sentence.
    """
    for line in output.splitlines():
        for marker in _ENVIRONMENT_MARKERS:
            if marker in line:
                return line.strip()
    if returncode in PYTEST_NONVERDICT_EXITS:
        return f"the test runner exited {returncode} without reporting any test outcome"
    return None


def _venv_interpreter(repo_root: Path) -> str | None:
    """The existing project venv's interpreter, relative to `repo_root`, or None.

    Both layouts, because a guard that prints a POSIX-only command on Windows has failed
    at the one job it has.
    """
    for relative in ("bin/python", "Scripts/python.exe"):
        if (repo_root / ".venv" / relative).is_file():
            return f".venv/{relative}"
    return None


def correct_invocation(script_path: str, argv: Sequence[str] | None = None) -> str:
    """The authoritative command that turns this failure into a real verdict.

    `uv run` is the authority: it provisions the environment as well as using it, so it
    works whether or not a venv exists yet. See `remedy_lines` for why it is not always
    the command an operator should be handed FIRST.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    return " ".join(
        shlex.quote(part) for part in ["uv", "run", "python", script_path, *arguments]
    )


def remedy_lines(
    script_path: str,
    argv: Sequence[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> list[str]:
    """The remedy to print: the fastest command that WORKS, then the authority.

    WHY THIS IS NOT SIMPLY `uv run` (measured 2026-08-24 on the EFS-backed box where this
    defect was found). `uv run pytest <one file>` STALLED ~30 minutes resolving an
    environment on the network mount and produced nothing, while `.venv/bin/pytest` on the
    same tree finished the same file in 25.21s. `uv run python scripts/check_ascii.py` had
    to be killed at 300s.

    That matters here specifically, and is not somebody else's performance problem: an
    operator who follows this guard's advice and gets a half-hour stall reaches for bare
    `python3` next -- which is exactly the invocation this guard exists to stop. A remedy
    nobody can afford to run sends them back to the fault.

    So when a `.venv` already exists, lead with it: same interpreter, no resolution step.
    `uv run` is still named, and is still the authority when there is no venv to use --
    the fast path is an equivalent, never a replacement.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    authority = correct_invocation(script_path, arguments)
    interpreter = _venv_interpreter(repo_root or Path.cwd())
    if interpreter is None:
        return [f"    {authority}"]
    direct = " ".join(
        shlex.quote(part) for part in [interpreter, script_path, *arguments]
    )
    return [
        f"    {direct}",
        "  Equivalent, and the authority when no .venv exists yet -- but it re-resolves",
        "  the environment, which is slow on a network mount:",
        f"    {authority}",
    ]


def report(
    fault: InterpreterEnvironmentFault,
    *,
    script_path: str,
    argv: Sequence[str] | None = None,
    verdict_exits: Mapping[int, str] | None = None,
    repo_root: Path | None = None,
    stream: TextIO | None = None,
) -> int:
    """Print `fault` as an environment fault and return `ENVIRONMENT_FAULT_EXIT`.

    Every clause is load-bearing; see this module's docstring for which misreading each
    one closes. `verdict_exits` comes from the caller because those codes are the
    caller's own contract -- printing them beside this one is what makes the distinction
    checkable by an operator rather than merely stated.
    """
    out = stream or sys.stderr
    print(
        f"{fault.script}: ENVIRONMENT FAULT -- this script did not run, because the "
        "interpreter that launched it cannot import what it requires. This is NOT a "
        "finding about the repository.",
        file=out,
    )
    if fault.missing:
        print(f"  missing module(s): {', '.join(fault.missing)}", file=out)
    if fault.detail:
        print(f"  cause: {fault.detail}", file=out)
    print(f"  interpreter used: {sys.executable}", file=out)
    if fault.reason:
        print(f"  required because: {fault.reason}", file=out)
    print(
        "  Nothing was derived, compared or checked, so NO verdict exists in either\n"
        "  direction. This is NOT a stale artifact, NOT a derivation error, and NOT a\n"
        "  failing test -- the work never started. A real problem may still be sitting\n"
        "  behind this, unreported; nothing has been ruled out.",
        file=out,
    )
    print(
        f"    exit {ENVIRONMENT_FAULT_EXIT} = wrong interpreter, nothing measured. This one.",
        file=out,
    )
    for code, meaning in sorted((verdict_exits or {}).items()):
        print(f"    exit {code} = {meaning}", file=out)
    print(
        "  Re-run through the project venv, which is the supported invocation:",
        file=out,
    )
    for line in remedy_lines(script_path, argv, repo_root=repo_root):
        print(line, file=out)
    print(
        "  Do not bypass a hook over this, and do not 'fix' it by installing into the\n"
        "  system interpreter: a bypass commits the artifact unchecked, which is the\n"
        "  one outcome worse than the fault above.",
        file=out,
    )
    return ENVIRONMENT_FAULT_EXIT


def verdict_note(verdict_exit: int, subject: str) -> str:
    """The counterpart line on a REAL finding, so both outcomes announce themselves.

    Without it only the failure mode is labelled, and an operator who has learned to read
    a blocked command as "the environment again" has nothing telling them this one is
    different.
    """
    return (
        f"  exit {verdict_exit}: the check RAN, and the line above is its finding about "
        f"{subject}.\n  This is a verdict, not a gate that could not measure (which is "
        f"exit {ENVIRONMENT_FAULT_EXIT})."
    )
