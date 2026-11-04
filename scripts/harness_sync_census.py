#!/usr/bin/env python3
# ruff: noqa: E402
"""Does this host need `/sync-git` + `/install-harness` for ai-harness? (ISSUE-REF T-05).

Usage:
    harness_sync_census.py refresh [--repo <checkout>]
    harness_sync_census.py notice            # a hook payload on stdin

`refresh` records, into `$XDG_STATE_HOME/ai-harness/harness-sync/census.json`: `origin/main`
from ONE bounded `git ls-remote origin refs/heads/main`, local `main` from
`git rev-parse refs/heads/main`, the installed-commit stamp (D-5) and `generated_at`. When the
remote commit is not in the local object store, one bounded fetch of `main` brings it in, so the
ahead count and the newest subject can be measured rather than guessed. A failed `ls-remote`
records `remote: failed (<reason>)`, keeps the last good `origin_main` with its own stamp, and
exits 1 naming the reason.

`notice` is `.claude/hooks/harness-sync-notice.sh`'s Python half: it reads the census and the
stamp, prints the one-paragraph notice or nothing, writes this session's next-due stamp (a
notice: the reinject window; silent: `0 <recheck epoch>`, which makes the hook skip Python until
the census is rewritten) and starts one background `refresh` when the census is missing, unparseable or older than
`max_age_s`. It never touches the network itself.

Writers: the notifier daemon's tick (`refresh_if_due`, D-4) and the hook's background refresh.
The stamp's writers are `.githooks/post-merge` and `skills/install-harness/scripts/
install_harness.py`, after a successful `install.sh` (D-5). Config: `config/harness-sync.toml`,
read at call time; `AIH_HARNESS_SYNC_CONFIG` overrides the path (a test seam).
"""

from __future__ import annotations

# Launched through a bare `python3`, so the interpreter is whatever the invoking PATH
# resolves -- an AMBIENT one this file does not choose, and on this fleet's EC2 and
# SageMaker hosts that is 3.10, where the `import tomllib` below does not exist at all.
# Copied verbatim from scripts/message_transports_cli.py, the variant for a file in
# scripts/; tests/test_interpreter_bootstrap_coverage.py requires it.
try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
sys.path.insert(0, str(ROOT / "src"))
DEFAULT_CONFIG = ROOT / "config" / "harness-sync.toml"
CONFIG_ENV = "AIH_HARNESS_SYNC_CONFIG"
CENSUS_VERSION = 1
SHORT = 10
#: The label line that heads the background refresh's command text (background-agent-lifecycle.md
#: § Label every programmatic launch): what the process is for = the surface that prescribes it.
REFRESH_LABEL_LINE = "HARNESS_SYNC_REFRESH=harness-sync-notice.sh"
#: A refresh launched this recently is still running or just finished; do not start another.
LAUNCH_COOLDOWN_S = 60
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REMOTE_OK = "ok"
#: `[notice.installer_by_host]` values (ISSUE-REF D-8, G-2): who runs /sync-git and /install-harness.
INSTALLER_ANY = "any"
INSTALLER_CHIEF = "chief"
INSTALLER_ROLES = (INSTALLER_ANY, INSTALLER_CHIEF)
INSTALLER_TABLE = "installer_by_host"
WAKE_IDLE = "wake_idle"
EXIT_OK = 0
EXIT_REMOTE_FAILED = 1
EXIT_USAGE = 2

_SCHEMA: dict[str, dict[str, type]] = {
    "census": {"max_age_s": int, "refresh_interval_s": int, "remote_timeout_s": int},
    "notice": {"reinject_hours": float},
}


class HarnessSyncConfigError(Exception):
    """`config/harness-sync.toml` is unreadable or carries a bad field."""


@dataclass(frozen=True)
class Config:
    path: Path
    max_age_s: int
    refresh_interval_s: int
    remote_timeout_s: int
    reinject_hours: float
    #: AI_HOST -> installer role; a host not listed is `any`.
    installer_by_host: dict[str, str] = field(default_factory=dict)
    #: The notifier warns passively and wakes one installation owner (default on).
    wake_idle: bool = True

    def installer(self) -> str:
        return self.installer_by_host.get(_ai_host(), INSTALLER_ANY)


def _ai_host() -> str:
    """``AI_HOST``, else the host marker file (``model_catalog.resolve_host``); "" if neither.

    A session's environment does not always carry ``AI_HOST`` (measured 2026-10-21 in a
    example-project-ec2 Bash tool shell), so the marker file is the fallback, as in worktree_drift.py.
    """
    host = os.environ.get("AI_HOST", "").strip()
    if host:
        return host
    try:
        import model_catalog

        return str(model_catalog.resolve_host() or "")
    except Exception:  # noqa: BLE001 - an unreadable marker is an unknown host: role "any"
        return ""


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_CONFIG


def load_config(path: Path | None = None) -> Config:
    """Read the config NOW; every hook run and tick calls this, so an edit applies at once."""
    path = path or config_path()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessSyncConfigError(f"cannot read {path}: {exc}") from exc
    values: dict[str, Any] = {}
    installers = _installers(path, document.get("notice"))
    wake_idle = _wake_idle(path, document.get("notice"))
    for section, keys in _SCHEMA.items():
        table = document.get(section)
        if not isinstance(table, dict):
            raise HarnessSyncConfigError(f"{path}: [{section}] is missing")
        unknown = sorted(
            set(table)
            - set(keys)
            - ({INSTALLER_TABLE, WAKE_IDLE} if section == "notice" else set())
        )
        if unknown:
            raise HarnessSyncConfigError(
                f"{path}: unknown [{section}] key(s) {unknown}"
            )
        for key, kind in keys.items():
            value = table.get(key)
            accepted = (int, float) if kind is float else (int,)
            if isinstance(value, bool) or not isinstance(value, accepted) or value <= 0:
                raise HarnessSyncConfigError(
                    f"{path}: [{section}] {key} must be a positive number"
                )
            values[key] = kind(value)
    return Config(
        path=path, installer_by_host=installers, wake_idle=wake_idle, **values
    )


def _wake_idle(path: Path, notice: Any) -> bool:
    """`[notice] wake_idle`: optional, default true; anything but a boolean is refused."""
    value = notice.get(WAKE_IDLE, True) if isinstance(notice, dict) else True
    if not isinstance(value, bool):
        raise HarnessSyncConfigError(
            f"{path}: [notice] {WAKE_IDLE} must be true or false"
        )
    return value


def _installers(path: Path, notice: Any) -> dict[str, str]:
    """`[notice.installer_by_host]`: optional; every value must be `any` or `chief`."""
    table = notice.get(INSTALLER_TABLE, {}) if isinstance(notice, dict) else {}
    if not isinstance(table, dict):
        raise HarnessSyncConfigError(
            f"{path}: [notice.{INSTALLER_TABLE}] must be a table"
        )
    for host, role in table.items():
        if role not in INSTALLER_ROLES:
            raise HarnessSyncConfigError(
                f'{path}: [notice.{INSTALLER_TABLE}] {host} must be "any" or "chief"'
            )
    return dict(table)


# ---------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-harness" / "harness-sync"


def census_path() -> Path:
    return state_dir() / "census.json"


def origin_marker_path() -> Path:
    """origin/main's sha, rewritten only when it changes, so its mtime says "origin moved".

    The hook's bash fast path compares this file's mtime with the session's stamp (G-3): the
    census itself is rewritten every refresh, so its mtime cannot say that.
    """
    return state_dir() / "origin-main"


def installed_commit_path() -> Path:
    """D-5's stamp: the main HEAD the last successful install installed."""
    return state_dir() / "installed-commit"


def notice_stamp_path(session_id: str) -> Path:
    return state_dir() / "notice-stamps" / session_id


def refresh_log_path() -> Path:
    return state_dir() / "refresh.log"


def _launch_marker() -> Path:
    return state_dir() / "refresh-launched"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_census() -> dict[str, Any] | None:
    try:
        document = json.loads(census_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("version") != CENSUS_VERSION:
        return None
    return document


def read_installed_commit() -> str | None:
    try:
        value = installed_commit_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, timeout: float = 10) -> tuple[int, str, str]:
    """One bounded git call; never prompts, never raises. A timeout is rc 124."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc.strerror or exc}"
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def main_tree(repo: Path) -> Path:
    """The main working tree of `repo`'s repository: what `/sync-git repo:` must name."""
    rc, out, _err = _git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if rc != 0 or not out:
        return repo
    common = Path(out)
    return common.parent if common.name == ".git" else common


def _reason(rc: int, err: str) -> str:
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    text = lines[-1] if lines else f"exit {rc}"
    return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", text)[:200]


def _has_commit(tree: Path, sha: str) -> bool:
    return _git(tree, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0


def _measure(
    tree: Path, local: str | None, origin: str | None, timeout: float
) -> tuple[int | None, str | None]:
    """`(ahead_count, newest_subject)` of origin over local, fetching main once if needed."""
    if not local or not origin:
        return None, None
    if not _has_commit(tree, origin):
        _git(
            tree,
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
            timeout=timeout,
        )
        if not _has_commit(tree, origin):
            return None, None
    rc, out, _err = _git(tree, "rev-list", "--count", f"{local}..{origin}")
    ahead = int(out) if rc == 0 and out.isdigit() else None
    rc, subject, _err = _git(tree, "log", "-1", "--format=%s", origin)
    return ahead, (subject if rc == 0 else None)


def refresh(repo: Path | None, config: Config) -> tuple[dict[str, Any], str | None]:
    """Measure and write the census. Returns it and the remote failure reason, if any."""
    tree = main_tree(repo or ROOT)
    previous = read_census() or {}
    now = time.time()
    rc, local, _err = _git(tree, "rev-parse", "--verify", "--quiet", "refs/heads/main")
    local_main = local if rc == 0 and local else None
    rc, out, err = _git(
        tree,
        "ls-remote",
        "--quiet",
        "origin",
        "refs/heads/main",
        timeout=config.remote_timeout_s,
    )
    failure: str | None = None
    if rc == 0 and out.split():
        origin_main: str | None = out.split()[0]
        origin_epoch: float | None = now
        failing_since = None
    else:
        failure = _reason(rc, err) if rc != 0 else "origin has no refs/heads/main"
        origin_main = previous.get("origin_main")
        origin_epoch = previous.get("origin_main_epoch")
        failing_since = previous.get("remote_failing_since_epoch") or now
    ahead, subject = _measure(tree, local_main, origin_main, config.remote_timeout_s)
    census = {
        "version": CENSUS_VERSION,
        "repo": str(tree),
        "generated_at": _iso(now),
        "generated_at_epoch": now,
        "local_main": local_main,
        "origin_main": origin_main,
        "origin_main_at": _iso(origin_epoch) if origin_epoch else None,
        "origin_main_epoch": origin_epoch,
        "ahead_count": ahead,
        "newest_subject": subject,
        "installed_commit": read_installed_commit(),
        "remote": REMOTE_OK if failure is None else f"failed ({failure})",
        "remote_failing_since_epoch": failing_since,
    }
    _write_atomic(census_path(), json.dumps(census, indent=1, sort_keys=True) + "\n")
    _mark_origin(origin_main)
    return census, failure


def _mark_origin(origin_main: str | None) -> None:
    if not origin_main:
        return
    marker = origin_marker_path()
    try:
        if marker.read_text(encoding="utf-8").strip() == origin_main:
            return
    except OSError:
        pass
    _write_atomic(marker, origin_main + "\n")


def refresh_on_event(repo: Path | None = None) -> dict[str, Any]:
    """Exactly one authoritative census for a main/install doorbell."""
    census, failure = refresh(repo, load_config())
    if failure:
        raise OSError("harness-census-unverified")
    return census


def refresh_if_due(repo: Path | None = None) -> str | None:
    """The notifier tick's one call (D-4): refresh at most every `refresh_interval_s`.

    Raises on a config or write failure; the tick isolates that. A remote failure is recorded in
    the census and returned, never raised.
    """
    config = load_config()
    census = read_census()
    last = census.get("generated_at_epoch") if census else None
    if isinstance(last, int | float) and time.time() - last < config.refresh_interval_s:
        return None
    _census, failure = refresh(repo, config)
    return failure


# ---------------------------------------------------------------------------------------------
# Notice
# ---------------------------------------------------------------------------------------------


def _short(sha: str | None) -> str:
    return sha[:SHORT] if sha else "none"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:  # noqa: PLR2004 - the unit boundary
        return f"{seconds}s"
    if seconds < 7200:  # noqa: PLR2004 - the unit boundary
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _sync_action(tree: Any, chief: bool) -> str:
    steps = f"/sync-git repo:{tree} then /install-harness"
    if chief:
        return f"the host chief runs {steps} here; leave both to it unless you are the chief."
    return f"run {steps}."


def _install_action(chief: bool) -> str:
    if chief:
        return "the host chief runs /install-harness here; leave it to the chief unless you are the chief."
    return "run /install-harness."


def _is_behind(census: dict[str, Any]) -> bool:
    local, origin, ahead = (
        census.get("local_main"),
        census.get("origin_main"),
        census.get("ahead_count"),
    )
    return bool(local and origin and origin != local and (ahead is None or ahead > 0))


def verdict(
    census: dict[str, Any], installed: str | None, installer: str = INSTALLER_ANY
) -> tuple[str | None, str]:
    """`(notice line or None, short verdict)` from the census and the live stamp.

    `installer` is this host's role (G-2): where the host chief alone syncs and installs, the
    notice says so instead of telling every session to run both.
    """
    chief = installer == INSTALLER_CHIEF
    local = census.get("local_main")
    origin = census.get("origin_main")
    ahead = census.get("ahead_count")
    tree = census.get("repo")
    if _is_behind(census):
        span = f"{_short(local)}..{_short(origin)}"
        if ahead is None:
            detail, short = (
                f"({span}, not fetched here yet)",
                "ahead of this host's main",
            )
            count = "ahead"
        else:
            subject = census.get("newest_subject") or "unknown"
            detail, short = f'({span}, newest: "{subject}")', f"{ahead} ahead of main"
            count = f"{ahead} ahead"
        return (
            f"ai-harness: origin/main is {count} of this host's main {detail}: "
            f"{_sync_action(tree, chief)}",
            short,
        )
    if local and installed != local:
        return (
            f"ai-harness: main {_short(local)} has not been installed here "
            f"(last install {_short(installed)}): {_install_action(chief)}",
            f"main {_short(local)} not installed",
        )
    return None, "in sync"


def build_notice(
    census: dict[str, Any], installed: str | None, config: Config, now: float
) -> str:
    line, short = verdict(census, installed, config.installer())
    lines: list[str] = []
    if census.get("remote") != REMOTE_OK:
        last_good = census.get("origin_main_epoch") or census.get(
            "remote_failing_since_epoch"
        )
        if isinstance(last_good, int | float) and now - last_good > config.max_age_s:
            lines.append(
                f"ai-harness: origin could not be read since {_iso(last_good)} "
                f"({_ago(now - last_good)} ago, {census.get('remote')}); last known "
                f"origin/main {_short(census.get('origin_main'))} ({short})"
            )
    if line:
        lines.append(line)
    return "\n".join(lines)


def wake_line() -> dict[str, Any]:
    """Call-time warning and census facts; the notifier owns episode/owner wake tokens.

    Disabled notices still report completion facts so an installed episode can close. Unknown,
    failed or stale measurements cannot open or close an episode. Prompt warnings are independent
    of ``wake_idle`` and continue through ``build_notice``.
    """
    config = load_config()
    census = read_census() or {}
    installed = read_installed_commit()
    local = census.get("local_main")
    origin = census.get("origin_main")
    generated = census.get("generated_at_epoch")
    valid = bool(
        isinstance(local, str)
        and re.fullmatch(r"[0-9a-f]{40,64}", local)
        and isinstance(origin, str)
        and re.fullmatch(r"[0-9a-f]{40,64}", origin)
        and isinstance(generated, int | float)
        and 0 <= time.time() - generated <= config.max_age_s
        and census.get("remote") == REMOTE_OK
    )
    chief = config.installer() == INSTALLER_CHIEF
    line = ""
    if _is_behind(census):
        line = (
            f"ai-harness: this host's main {_short(local)} is behind origin/main: "
            f"{_sync_action(census.get('repo'), chief)}"
        )
    elif local and installed != local:
        line = (
            f"ai-harness: main {_short(local)} has not been installed here: "
            f"{_install_action(chief)}"
        )
    return {
        "line": line,
        "enabled": config.wake_idle,
        "installer": config.installer(),
        "installed": installed,
        "current": valid and not _is_behind(census) and installed == local,
        "needed": valid and bool(line),
    }


def start_background_refresh(config: Config) -> bool:
    """One detached, bounded, labelled `refresh` on legacy-selected hosts."""
    from ai_chassis.wake import sessions

    if sessions.enabled():
        return False
    marker = _launch_marker()
    try:
        if time.time() - marker.stat().st_mtime < LAUNCH_COOLDOWN_S:
            return False
    except OSError:
        pass
    bound = 2 * config.remote_timeout_s + 30
    launch = shlex.join([sys.executable, str(SCRIPT), "refresh"])
    timeout = shutil.which("timeout")
    if timeout:
        launch = f"{shlex.join([timeout, '-k', '5s', f'{bound}s'])} {launch}"
    command = f"{REFRESH_LABEL_LINE}\n{launch}"
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with refresh_log_path().open("a", encoding="utf-8") as log:
            log.write(f"{_iso(time.time())} launch {REFRESH_LABEL_LINE}\n")
            log.flush()
            subprocess.Popen(  # noqa: S603 - fixed argv
                ["bash", "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=directory,
                start_new_session=True,
            )
    except OSError:
        return False
    return True


def _emit(event: str, text: str) -> None:
    output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.write(json.dumps(output) + "\n")


def notice_main(stdin_text: str) -> int:
    """The hook's Python half. Exits 0 on every path: a notice must never fail a turn."""
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        return EXIT_OK
    if not isinstance(payload, dict) or {"agent_id", "agent_type"} & set(payload):
        return EXIT_OK
    session_id = str(payload.get("session_id") or "")
    if not SESSION_ID_RE.match(session_id):
        return EXIT_OK
    event = str(payload.get("hook_event_name") or "SessionStart")
    try:
        config = load_config()
    except HarnessSyncConfigError:
        return EXIT_OK
    now = time.time()
    census = read_census()
    if census is None:
        start_background_refresh(config)
        return EXIT_OK
    generated = census.get("generated_at_epoch")
    if not isinstance(generated, int | float) or now - generated > config.max_age_s:
        start_background_refresh(config)
    text = build_notice(census, read_installed_commit(), config, now)
    # A silent run stamps due=0, so it never suppresses the first real notice once the remote
    # moves; the hook skips Python until the census is rewritten or the recheck epoch passes.
    if text:
        _emit(event, text)
        stamp = f"{int(now + config.reinject_hours * 3600)}\n"
    else:
        stamp = f"0 {int(now + config.max_age_s)}\n"
    try:
        _write_atomic(notice_stamp_path(session_id), stamp)
    except OSError:
        pass
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_sync_census.py",
        description="Record and report whether ai-harness needs /sync-git + /install-harness here.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    refresh_parser = sub.add_parser("refresh", help="measure and write the census")
    refresh_parser.add_argument(
        "--repo", type=Path, help="an ai-harness checkout (default: this script's)"
    )
    sub.add_parser("notice", help="the hook's half: payload on stdin, notice on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "notice":
        return notice_main(sys.stdin.read())
    try:
        config = load_config()
    except HarnessSyncConfigError as exc:
        print(f"harness_sync_census: {exc}", file=sys.stderr)
        return EXIT_USAGE
    census, failure = refresh(args.repo, config)
    if failure:
        print(
            f"harness_sync_census: origin unreadable: {failure}; kept last known "
            f"origin/main {_short(census.get('origin_main'))}",
            file=sys.stderr,
        )
        return EXIT_REMOTE_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
