"""fleet_envaudit: a report-only pytest plugin that makes hidden environment inputs name themselves.

A test's verdict must be a function of the tree under test alone. When it also depends
on which binaries, locales or shells the machine happens to have, the failure names the
*symptom* and never the *class*, so the same diagnosis gets paid for again. Representative
incidents:

    INCIDENT-101  installed shell set      a manifest row derived from whether `zsh` existed
    INCIDENT-102  installed locales        setlocale(en_US.UTF-8) hard-errored where absent
    INCIDENT-103  local object database    a pin reachable from no remote -> git exit 128
    INCIDENT-104  installed binaries       several tests reached an `lsof` the container lacks
    INCIDENT-105  inherited LC_ALL         repeated failures from bash's setlocale warning on stderr

"lsof missing", "no en_US.UTF-8" and "not our ref" read like unrelated bugs. They are one
bug, and this plugin prints it: every external binary the session actually spawned, whether
it resolves *here*, and every non-trivial locale request, each attributed to the test that
caused it.

Report-only, absolutely. It never changes an outcome; the report is the whole product. That
property is load-bearing rather than merely polite -- see ``_audit_hook``, where an
uncaught exception would *block the spawn it is observing*.

THE ``fleet_`` PREFIX IS LOAD-BEARING, NOT NOISE -- do not "simplify" it away. The bare
name ``envaudit`` is an UNRELATED project already published on PyPI ("Environment variable
validator and manager", installable today), as is ``envclass``, which the prototype used
for its report directory. Verified against the PyPI JSON API rather than assumed:
``envaudit`` and ``envclass`` return HTTP 200, ``fleet-envaudit``/``fleet_envaudit`` return
404. Were this module named for the bare string, anyone who later declared a dependency on
it would silently install a stranger's package. The prefix also matches the existing
``fleet-testkit`` naming in ``packages/``. This module is deliberately NOT a distribution:
it lives inside ``tests/``, so it needs no PyPI name at all and cannot collide by
construction -- the prefix is belt-and-braces for the day it is extracted.
"""

from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Probe route. Public first: PEP 578 audit events are a documented contract, while
# ``subprocess.Popen._execute_child`` carries no compatibility guarantee at all.
TIER_PUBLIC = "public-audit-hook"
TIER_PRIVATE = "private-execute-child"
TIER_DISABLED = "disabled"

# A name chosen so that no machine can plausibly have it on PATH. Spawning something
# that CANNOT exist is what makes the capability self-test cheap (no fork/exec/reap:
# a fraction of a millisecond less than `true`) and, more importantly, keeps the
# self-test from being an instance of the very defect this plugin detects -- a probe
# that spawns a real binary is itself asserting that binary is installed.
_SELFTEST_SENTINEL = "fleet-envaudit-capability-selftest-no-such-binary"

# Suppressed only WHEN THEY RESOLVE (see ``_reportable``). A missing `git` is at least as
# interesting as a missing `lsof`, so presence in this set must never be able to hide the
# decisive case.
_BASELINE = frozenset(
    {
        "awk",
        "bash",
        "cat",
        "cp",
        "echo",
        "env",
        "false",
        "git",
        "ls",
        "mkdir",
        "mv",
        "pip",
        "printf",
        "pytest",
        "python",
        "python3",
        "rm",
        "ruff",
        "sed",
        "sh",
        "sleep",
        "touch",
        "true",
        "uname",
    }
)

_TRIVIAL_LOCALES = frozenset({None, "", "C"})

_events: dict = {"binary": {}, "locale": [], "missing": {}}
_state: dict = {
    "tier": TIER_DISABLED,
    "reason": "probes not installed yet",
    "selftest_observed": False,
    "install_error": None,
}
_resolution_cache: dict = {}
_in_hook = False

# Errors the probes swallowed to preserve report-only. Recorded rather than discarded: a
# detector that quietly stopped detecting is worse than one that is plainly absent, because
# its silence reads as "nothing to report". Surfaced in the terminal summary.
_probe_errors: list[str] = []


def _note_probe_error(context: str, exc: BaseException) -> None:
    """Record an error the plugin absorbed, keeping the list bounded."""
    if len(_probe_errors) < 20:
        message = f"{context}: {type(exc).__name__}: {exc}"
        if message not in _probe_errors:
            _probe_errors.append(message)


def _nodeid() -> str:
    """The test that caused the event, or a marker for collection/session scope."""
    return os.environ.get("PYTEST_CURRENT_TEST", "<collection>").split(" ")[0]


def _effective_path(env) -> str | None:
    """PATH from an explicit ``env=`` mapping, or None to mean "use this process's"."""
    if env is None:
        return None
    try:
        value = env.get("PATH")
    except AttributeError:
        return None
    if isinstance(value, bytes):
        try:
            return os.fsdecode(value)
        except ValueError:
            return None
    return value if isinstance(value, str) else None


def _as_text(executable) -> str | None:
    """Normalise an audit event's executable to str, or None if it is not path-like."""
    if isinstance(executable, bytes):
        try:
            return os.fsdecode(executable)
        except ValueError:
            return None
    try:
        candidate = os.fspath(executable)
    except TypeError:
        return None
    if isinstance(candidate, bytes):
        try:
            return os.fsdecode(candidate)
        except ValueError:
            return None
    return candidate if isinstance(candidate, str) else None


def _resolves(executable: str, cwd=None, path: str | None = None) -> bool:
    """Whether *executable* would resolve to something ``exec`` could actually run, here.

    This mirrors CPython's own resolution rather than delegating to ``shutil.which``,
    because the audit event fires BEFORE the spawn and so never observes the
    ``FileNotFoundError`` that the old private-API wrapper caught. The resolved-versus-
    missing distinction therefore has to be *decided* here, and a detector that gets it
    wrong is worse than no detector: the next instance of this class would be diagnosed
    against it.

    Two branches, matching ``subprocess`` (CPython 3.12 ``_execute_child``): a name
    containing a directory separator is used literally, and a bare name is searched along
    the exec path. ``shutil.which`` alone is not enough -- it takes no ``cwd``, so a
    cwd-relative ``./tool`` is misjudged, and the file-versus-directory and
    executable-bit cases need the explicit test below.

    Validated against real spawn outcomes across 13 cases -- bare name present/absent,
    absolute present/absent, a directory, a file with its executable bit cleared, bytes,
    a PathLike, and a cwd-relative path resolving both ways -- with 13/13 agreement.
    Still an approximation of ``exec``, not an oracle for it: it cannot see a broken
    interpreter line, a wrong-architecture binary, or a race between this check and the
    spawn. Those would be reported as present. Being report-only, the cost of such a
    miss is one imprecise line, never a changed verdict.
    """
    if not executable:
        return False
    if os.path.dirname(executable):
        candidate = executable
        if cwd is not None and not os.path.isabs(executable):
            cwd_text = _as_text(cwd)
            if cwd_text is None:
                return False
            candidate = os.path.join(cwd_text, executable)
        return os.path.isfile(candidate) and os.access(candidate, os.X_OK)
    return shutil.which(executable, path=path) is not None


def _reportable(name: str, resolved: bool, declared: set[str] | None) -> bool:
    """Whether one binary belongs in the report.

    An unresolved binary is ALWAYS reportable. Allow-listing exists to quiet the expected
    ones, and an allow-list able to suppress "this binary is absent here" would silence
    exactly the finding the plugin is for.
    """
    if not resolved:
        return True
    if name in _BASELINE:
        return False
    return not (declared is not None and name in declared)


def _record_spawn(executable, cwd=None, env=None) -> None:
    """Record one spawn attempt, keyed by binary basename."""
    text = _as_text(executable)
    if text is None:
        return
    if os.path.basename(text) == _SELFTEST_SENTINEL:
        _state["selftest_observed"] = True
        return
    name = os.path.basename(text) or text
    path = _effective_path(env)
    key = (text, _as_text(cwd), path)
    if key in _resolution_cache:
        resolved = _resolution_cache[key]
    else:
        resolved = _resolves(text, cwd=cwd, path=path)
        _resolution_cache[key] = resolved
    entry = _events["binary"].setdefault(
        name, {"count": 0, "tests": [], "resolved": resolved}
    )
    entry["count"] += 1
    if not resolved:
        entry["resolved"] = False
    nodeid = _nodeid()
    if nodeid not in entry["tests"]:
        entry["tests"].append(nodeid)
    if not resolved:
        _events["missing"][name] = entry["tests"]


def _audit_hook(event: str, args) -> None:
    """PEP 578 hook. Receives EVERY audited event in the process, so it returns fast.

    The blanket ``except`` is not defensive habit, it is AC-1. Measured on CPython
    3.12.13: an exception raised inside an audit hook propagates out of
    ``subprocess.Popen`` and the spawn never happens. An unguarded hook would therefore
    convert a passing test into a failing one -- this plugin turning a green suite red is
    the one outcome that would make it worse than absent.
    """
    if event != "subprocess.Popen":
        return
    global _in_hook
    if _in_hook:
        # Re-entrancy belt-and-braces. Nothing in _record_spawn is audited today
        # (shutil.which raises no events; os.access and os.path.isfile raise none),
        # but a future stdlib addition must degrade to a dropped record, not recursion.
        return
    _in_hook = True
    try:
        _record_spawn(
            args[0],
            args[2] if len(args) > 2 else None,
            args[3] if len(args) > 3 else None,
        )
    except Exception as exc:  # noqa: BLE001 - a raise here would BLOCK the observed spawn.
        _note_probe_error("recording a spawn", exc)
    finally:
        _in_hook = False


def _probe_capabilities() -> dict:
    """Decide the route by RUNTIME SELF-TEST: install, spawn, confirm observation.

    Deliberately not a version comparison. ``hasattr(sys, "addaudithook")`` is necessary
    but NOT sufficient -- the attribute can exist while ``subprocess`` raises no
    ``subprocess.Popen`` event at all (a vendored or patched build, or a future removal),
    and a version check would mis-predict silently in both directions: wrong on a
    backport, wrong on a build that dropped the event. The check performed here can come
    out the other way, which is the whole point of running it.

    The hook installed is the REAL hook, not a probe stand-in, so a pass means the exact
    callable that will do the recording was seen firing.
    """
    audit_hook_available = hasattr(sys, "addaudithook")
    if audit_hook_available:
        sys.addaudithook(_audit_hook)
        _state["selftest_observed"] = False
        try:
            subprocess.Popen([_SELFTEST_SENTINEL])
        except OSError:
            # Expected and required: the sentinel does not resolve, so the spawn fails
            # after the audit event has already fired. The event is the observation, and
            # the failure to launch is what keeps the self-test free of a real dependency.
            pass
        except Exception as exc:  # noqa: BLE001 - a self-test must not take the suite down.
            _note_probe_error("capability self-test", exc)
    return {
        "audit_hook_available": audit_hook_available,
        "audit_event_observed": bool(_state["selftest_observed"]),
        "private_api_available": hasattr(subprocess.Popen, "_execute_child"),
    }


def select_tier(
    *,
    audit_hook_available: bool,
    audit_event_observed: bool,
    private_api_available: bool,
) -> tuple[str, str]:
    """Pick a probe route from capability flags. Pure, so every branch is testable.

    Tier 2 is narrower than it looks, and conflating the two failure modes is the easy
    mistake here. It covers ONLY "the audit event is absent but the private method
    survives". It does NOT protect against ``_execute_child`` being the thing that went
    away -- which is the original risk that held adoption back. For that case the honest
    answer is tier 3: install nothing, say why, and let the suite run untouched. So tier 2
    is a transitional convenience, never the safety net, and tier 3 is the terminal state.
    """
    if audit_hook_available and audit_event_observed:
        return TIER_PUBLIC, "sys.addaudithook observed a subprocess.Popen audit event"
    if audit_hook_available:
        unavailable = "no subprocess.Popen audit event was raised for a known spawn"
    else:
        unavailable = "sys.addaudithook is absent"
    if private_api_available:
        return (
            TIER_PRIVATE,
            f"{unavailable}; degraded to subprocess.Popen._execute_child",
        )
    return (
        TIER_DISABLED,
        f"{unavailable}, and subprocess.Popen._execute_child is absent too",
    )


def _install_private_spawn_probe() -> None:
    """Tier 2 only. Wrap the private spawn boundary, delegating and re-raising untouched."""
    real_execute = subprocess.Popen._execute_child

    def audited_execute(self, args, *rest, **kwargs):
        executable = args[0] if isinstance(args, (list, tuple)) and args else args
        try:
            result = real_execute(self, args, *rest, **kwargs)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            # Unlike the audit hook, this boundary sees resolution failure directly, so
            # the missing signal is the exception itself. Record, then re-raise untouched.
            try:
                _record_spawn_unresolved(executable)
            except Exception as exc:  # noqa: BLE001 - never mask the caller's real error.
                _note_probe_error("recording an unresolved spawn", exc)
            raise
        try:
            _record_spawn(executable, kwargs.get("cwd"), kwargs.get("env"))
        except Exception as exc:  # noqa: BLE001 - report-only.
            _note_probe_error("recording a spawn (tier 2)", exc)
        return result

    subprocess.Popen._execute_child = audited_execute


def _record_spawn_unresolved(executable) -> None:
    """Record a spawn already known to have failed resolution (tier 2 path)."""
    text = _as_text(executable)
    if text is None:
        return
    name = os.path.basename(text) or text
    entry = _events["binary"].setdefault(
        name, {"count": 0, "tests": [], "resolved": False}
    )
    entry["count"] += 1
    entry["resolved"] = False
    nodeid = _nodeid()
    if nodeid not in entry["tests"]:
        entry["tests"].append(nodeid)
    _events["missing"][name] = entry["tests"]


def _install_locale_probe() -> None:
    """Wrap ``locale.setlocale``.

    This one cannot move to ``addaudithook``: measured on CPython 3.12.13, a
    ``setlocale`` call raises NO audit event, so there is nothing to subscribe to. The
    port to PEP 578 removes ONE private-API dependency (the spawn probe's), not all
    monkeypatching -- worth stating plainly, because "we eliminated the monkeypatching"
    is the tempting and wrong summary. The residual risk is ordinary rather than
    open-ended: ``locale.setlocale`` is public, documented API.
    """
    real_setlocale = locale.setlocale

    def audited_setlocale(category, value=None):
        try:
            if value not in _TRIVIAL_LOCALES:
                _events["locale"].append({"test": _nodeid(), "requested": str(value)})
        except Exception as exc:  # noqa: BLE001 - report-only.
            _note_probe_error("recording a locale request", exc)
        return real_setlocale(category, value)

    locale.setlocale = audited_setlocale


def _install_probes(capabilities: dict | None = None) -> str:
    """Install the probes and return the active tier.

    ``capabilities`` is injectable so tests can drive every tier -- including the ones
    this interpreter cannot reach -- without monkeypatching ``sys``. A fallback nobody has
    watched execute is indistinguishable from one that would crash on the day it is
    needed.

    ONE-WAY DOOR, stated because it is a real trade rather than an oversight:
    ``sys.addaudithook`` is append-only and CANNOT be uninstalled for the lifetime of the
    process. There is no runtime disable and no teardown; the old private-API patch was
    reversible and this is not. Acceptable here on two grounds. The hook is installed once
    per pytest process, which exits at the end of the run, so "for the process lifetime"
    means "for this test run". And the hook is inert by construction: it returns
    immediately on every event other than ``subprocess.Popen``, and on that event it only
    ever appends to a dict. To disable it, do not load the plugin -- that is the supported
    off switch, and it is why the plugin is a plugin.
    """
    caps = capabilities if capabilities is not None else _probe_capabilities()
    tier, reason = select_tier(**caps)
    if tier == TIER_PRIVATE:
        _install_private_spawn_probe()
    if tier != TIER_DISABLED:
        _install_locale_probe()
    _state["tier"] = tier
    _state["reason"] = reason
    return tier


class _XdistHooks:
    """Holds the xdist-only hook.

    ``pytest_testnodedown`` is not a hookspec unless xdist is installed, and pluggy
    rejects an unknown hook at collection with a hard PluginValidationError -- which would
    take down every suite in a repo that runs single-process. Registered conditionally
    for that reason.
    """

    def pytest_testnodedown(self, node, error):
        _absorb(node)


def pytest_configure(config) -> None:
    """Install the probes, failing safe.

    AC-10: if installation raises, the run proceeds completely unaffected and the failure
    is reported in the terminal summary. Never swallowed silently -- a detector that
    quietly stopped detecting is worse than one that is plainly absent, because its
    silence reads as "nothing to report".
    """
    try:
        _install_probes()
    except Exception as exc:  # noqa: BLE001 - the suite must outlive our own failure.
        _state["tier"] = TIER_DISABLED
        _state["install_error"] = f"{type(exc).__name__}: {exc}"
        _state["reason"] = "probe installation raised"
    if config.pluginmanager.hasplugin("xdist"):
        config.pluginmanager.register(_XdistHooks(), "fleet-envaudit-xdist")


def pytest_report_header(config) -> str:
    """Name the active tier on every run (AC-11)."""
    if _state["install_error"]:
        return (
            f"fleet_envaudit: DISABLED - probe installation failed "
            f"({_state['install_error']}); this run is unaffected"
        )
    return f"fleet_envaudit: recording environment dependencies (report-only, tier={_state['tier']})"


def _declared(rootdir) -> set[str] | None:
    """Read the declared-prerequisites manifest, if the repo ships one."""
    manifest = Path(rootdir) / "fleet-envaudit-allow.txt"
    if not manifest.is_file():
        return None
    names = set()
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if line:
            names.add(line)
    return names


def pytest_sessionfinish(session, exitstatus) -> None:
    """Hand this worker's events to the controller over xdist's own channel.

    File-based shards CANNOT work here: workers run sessionfinish only after the
    controller's terminal summary returns, so any wait in the summary deadlocks and any
    unsynchronised read picks up the PREVIOUS run's shards -- stale data that looks
    entirely plausible. ``workeroutput`` is the supported channel.
    """
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["fleet_envaudit"] = _events


def _absorb(node) -> None:
    """Controller side: absorb one worker's events as its node shuts down."""
    data = getattr(node, "workeroutput", {}).get("fleet_envaudit")
    if not data:
        return
    for name, entry in data.get("binary", {}).items():
        current = _events["binary"].setdefault(
            name,
            {"count": 0, "tests": [], "resolved": entry.get("resolved", True)},
        )
        current["count"] += entry.get("count", 0)
        if not entry.get("resolved", True):
            current["resolved"] = False
        for nodeid in entry.get("tests", []):
            if nodeid not in current["tests"]:
                current["tests"].append(nodeid)
    _events["missing"].update(data.get("missing", {}))
    _events["locale"].extend(data.get("locale", []))


def build_report(declared: set[str] | None) -> dict:
    """The report as data, so tests assert on it without scraping terminal output."""
    reported = {
        name: entry
        for name, entry in sorted(_events["binary"].items())
        if _reportable(name, entry.get("resolved", True), declared)
    }
    return {
        "tier": _state["tier"],
        "reason": _state["reason"],
        "install_error": _state["install_error"],
        "probe_errors": list(_probe_errors),
        "binary": reported,
        "missing": dict(_events["missing"]),
        "locale": list(_events["locale"]),
        "declared_manifest_present": declared is not None,
    }


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    report = build_report(_declared(config.rootdir))

    destination = os.environ.get("FLEET_ENVAUDIT_REPORT")
    if destination:
        # Opt-in only. This suite asserts at session scope that no test dirtied the
        # repository working tree (tests/conftest.py), so an unconditional write into
        # the repo would fail the run it is supposed to leave alone.
        try:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            terminalreporter.write_line(
                f"fleet_envaudit: could not write {destination}: {exc}"
            )

    if report["install_error"]:
        terminalreporter.write_sep(
            "=", "fleet_envaudit: DISABLED (report-only, not a failure)"
        )
        terminalreporter.write_line(
            f"  probe installation failed: {report['install_error']}"
        )
        terminalreporter.write_line("  This run's results are unaffected.")
        return

    if report["tier"] != TIER_PUBLIC:
        terminalreporter.write_sep("=", "fleet_envaudit: degraded probe route")
        terminalreporter.write_line(f"  tier={report['tier']} - {report['reason']}")

    if report["probe_errors"]:
        # Absorbed to keep the plugin report-only, but never hidden: a detector that
        # silently stopped detecting reads as "nothing to report", which is a lie.
        terminalreporter.write_sep(
            "=", "fleet_envaudit: errors absorbed while recording"
        )
        for message in report["probe_errors"]:
            terminalreporter.write_line(f"  {message}")
        terminalreporter.write_line("  No test outcome was affected by these.")

    if not report["binary"] and not report["locale"]:
        return

    terminalreporter.write_sep(
        "=", "fleet_envaudit: environment dependencies (REPORT ONLY, not a failure)"
    )
    for name, entry in sorted(report["binary"].items()):
        flag = (
            "MISSING-ON-THIS-MACHINE"
            if not entry.get("resolved", True)
            else "present here"
        )
        terminalreporter.write_line(f"  {name:<14} x{entry['count']:<4d} {flag}")
        for nodeid in entry["tests"][:3]:
            terminalreporter.write_line(f"      {nodeid}")
        if len(entry["tests"]) > 3:
            terminalreporter.write_line(
                f"      ... +{len(entry['tests']) - 3} more tests"
            )

    # Deduplicate: one third-party import can call setlocale hundreds of times, and 757
    # identical lines is how a report-only detector gets scrolled past and ignored.
    counts: dict = {}
    for event in report["locale"]:
        key = (event["requested"], event["test"])
        counts[key] = counts.get(key, 0) + 1
    for (requested, test), n in sorted(counts.items()):
        times = f" (x{n})" if n > 1 else ""
        terminalreporter.write_line(f"  locale.setlocale({requested}) in {test}{times}")

    terminalreporter.write_line("")
    terminalreporter.write_line(
        "  Each line is a way this suite's verdict can differ on another machine."
    )
    if not report["declared_manifest_present"]:
        terminalreporter.write_line(
            "  Declare intended ones in fleet-envaudit-allow.txt to quiet them "
            "(a missing binary is always reported)."
        )
