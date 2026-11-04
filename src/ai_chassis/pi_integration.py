"""pi on this machine: its pinned entrypoint, its settings and its work-queue link (ISSUE-REF).

install.sh's retired Steps 5cc (bootstrap the pi CLI binary), 5d (pi CLI integration) and 5f (the
pi task-tracking extension and the work-queue extension link). Installing software is the private
orchestrator's (D-C15, after D-C12 and the IN-56/IN-74 consent axis): its `pi-bootstrap` step
installs or updates the pi CLI and its `pi-extensions` step installs the task-tracking extension.
This module is what stays public, and it installs nothing:

* `pi-wrapper` publishes ``~/.claude/bin/pi`` on a machine whose persistent npm prefix holds pi.
  pi's npm entrypoint is ``#!/usr/bin/env node``, so a bare link resolves the interpreter from the
  caller's PATH, which an installer cannot order; the published file is a generated wrapper that
  pins an interpreter meeting pi's engines minimum. With none available nothing is published and a
  stale entrypoint is removed, so a pi that cannot start is unreachable. The machine's own default
  Node is never installed or switched: that is a human decision.
* `pi-settings` renders ``pi/settings.<AI_HOST>.json``, wires the lifecycle extension and runs the
  auth-shape check.
* `pi-wq-link` links the fleet's own work-queue extension behind ``PI_MIN_VERSION``.

The resolution helpers (`PiMachine`, `resolve_pi`, `npm_global_prefix`, `semver_at_least`,
`semver_token`, `resolve_node`) are shared with the private orchestrator's steps, so all of them
judge the machine the same way.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path

from .links import LinkError
from .shell_startup import publish_link

#: pi's npm metadata declares ``"engines":{"node":">=22.19.0"}``; under an older Node pi cannot
#: start.
PI_NODE_MIN = "22.19.0"
#: The pi release whose documented extension API the work-queue extension is written against
#: (D-15). The private bootstrap upgrades to the registry's latest, which clears it; the
#: work-queue link enforces it.
PI_MIN_VERSION = "0.86.1"
#: A pi-private Node beside the persistent npm prefix, used only to RUN pi and never put on PATH.
PI_PRIVATE_NODE_RELPATH = Path("tools") / "node22" / "bin" / "node"
UPDATE_MODE_ENV = "AIH_INSTALL_UPDATE_MODE"
_PROBE_TIMEOUT_S = 60
_WRAPPER_MODE = 0o755
_SEMVER_TOKEN = re.compile(r"^[0-9]+\.[0-9]+")
_LEADING_DIGITS = re.compile(r"^[0-9]*")


def update_mode(environ: Mapping[str, str]) -> bool:
    """``AIH_INSTALL_UPDATE_MODE``, as install.sh's ``$UPDATE_MODE`` hands it on; unset is 0."""
    return environ.get(UPDATE_MODE_ENV, "0") not in ("", "0")


def semver_at_least(have: str, want: str) -> bool:
    """install.sh's ``_semver_at_least``: the first three numeric release components, compared.

    A leading ``v`` is stripped and a prerelease or build suffix dropped; a missing or unparseable
    component reads as 0, so an empty version fails every non-zero floor rather than passing one.
    """
    have_parts = have.removeprefix("v").split(".")
    want_parts = want.split(".")
    for index in range(3):
        have_part = _LEADING_DIGITS.match(
            have_parts[index] if index < len(have_parts) else ""
        ).group()
        want_part = _LEADING_DIGITS.match(
            want_parts[index] if index < len(want_parts) else ""
        ).group()
        have_number, want_number = int(have_part or 0), int(want_part or 0)
        if have_number != want_number:
            return have_number > want_number
    return True


def semver_token(report: str) -> str | None:
    """The last semver-shaped token of a version report, or ``None``.

    Tolerant of ``0.87.1``, ``v0.87.1`` and ``pi 0.87.1``; anything that does not parse is
    ``None``, never a default version.
    """
    candidate = None
    for token in report.split():
        token = token.removeprefix("v")
        if _SEMVER_TOKEN.match(token):
            candidate = token
    return candidate


def _executable(path: Path | None) -> bool:
    return path is not None and path.is_file() and os.access(path, os.X_OK)


def _output(argv: Sequence[str], *, merge_stderr: bool = False) -> tuple[int, str]:
    """Run a probe with no stdin; ``(returncode, stdout)``, ``(1, "")`` when it cannot start."""
    try:
        result = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def resolve_node(candidates: Sequence[Path | None]) -> Path | None:
    """The first executable candidate whose ``--version`` meets ``PI_NODE_MIN``."""
    for candidate in candidates:
        if not _executable(candidate):
            continue
        _code, version = _output([str(candidate), "--version"])
        if semver_at_least(version.strip(), PI_NODE_MIN):
            return candidate
    return None


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def npm_global_prefix() -> str:
    """``npm prefix -g 2>/dev/null || true``: empty when npm is absent or fails."""
    npm = shutil.which("npm")
    if npm is None:
        return ""
    code, stdout = _output([npm, "prefix", "-g"])
    return stdout.strip() if code == 0 else ""


def resolve_pi() -> Path | None:
    """The pi a non-interactive installer can run: PATH's, else ``<npm prefix -g>/bin/pi``.

    A machine outside ``pi_persistent_prefix_machines`` keeps pi in npm's global prefix, whose bin
    directory an interactive shell puts on PATH and an install launched from a hook or an agent
    session usually does not (ISSUE-REF).
    """
    found = _which("pi")
    if found is not None:
        return found
    prefix = npm_global_prefix()
    candidate = Path(prefix) / "bin" / "pi" if prefix else None
    return candidate if _executable(candidate) else None


class PiMachine:
    """This machine as pi sees it: its persistent prefix, the Node pi would run under, its pi."""

    def __init__(
        self,
        machine: str,
        persistent_root: Path,
        persistent_prefix_machines: Sequence[str],
    ) -> None:
        self.npm_prefix: Path | None = None
        self.private_node: Path | None = None
        if machine and machine in persistent_prefix_machines:
            self.npm_prefix = persistent_root / "tools" / "npm-global"
            self.private_node = persistent_root / PI_PRIVATE_NODE_RELPATH
        path_node = _which("node")
        self.node = resolve_node([path_node, self.private_node])
        self.node_report = "no node on PATH"
        if path_node is not None:
            _code, version = _output([str(path_node), "--version"])
            self.node_report = f"{version.rstrip(chr(10))} at {path_node}"
        self._current_bin: Path | None = None
        self._resolved = False

    @property
    def prefix_pi(self) -> Path | None:
        return self.npm_prefix / "bin" / "pi" if self.npm_prefix else None

    @property
    def current_bin(self) -> Path | None:
        """The pi an operator actually runs. A persistent-prefix machine: that prefix's copy, then
        PATH's. Any other: `resolve_pi`, since npm's global prefix is where pi lands there.
        Resolved on first use, so a step that never asks never runs npm."""
        if not self._resolved:
            if self.npm_prefix is None:
                self._current_bin = resolve_pi()
            elif _executable(self.prefix_pi):
                self._current_bin = self.prefix_pi
            else:
                self._current_bin = _which("pi")
            self._resolved = True
        return self._current_bin

    def refresh(self) -> None:
        self._resolved = False

    def installed(self) -> bool:
        return self.current_bin is not None

    def installed_version(self) -> str | None:
        """The running pi's version; retried under the pinned Node, which an old PATH node needs."""
        if self.current_bin is None:
            return None
        _code, raw = _output([str(self.current_bin), "--version"])
        if not any(character.isdigit() for character in raw) and self.node is not None:
            _code, raw = _output([str(self.node), str(self.current_bin), "--version"])
        return semver_token(raw)


# --- pi-wrapper (the end of Step 5cc, IN-58) -------------------------------------------------


def _wrapper(node: Path, prefix_pi: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# GENERATED by ai-harness apply (the pi-wrapper step) — do not edit, every run "
        "overwrites this.\n"
        "# pi's own entrypoint is `#!/usr/bin/env node`, which resolves the interpreter from "
        "the\n"
        "# caller's PATH. This machine's PATH node is older than pi's engines minimum "
        f"({PI_NODE_MIN}),\n"
        "# so the interpreter is pinned here instead of left to PATH ordering.\n"
        f'exec "{node}" "{prefix_pi}" "$@"\n'
    )


def _remove(path: Path) -> None:
    if path.is_symlink() or path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise LinkError(f"could not remove {path}: {exc}") from exc


def publish_wrapper(
    state: PiMachine, home: Path, environ: MutableMapping[str, str]
) -> None:
    """``~/.claude/bin/pi``, re-made on every run including ``--update``: that directory can be
    ephemeral while the persistent prefix it points into survives a rebuild. The private
    bootstrap names how to provision the pinned Node when there is none."""
    prefix_pi = state.prefix_pi
    if not _executable(prefix_pi):
        return
    bin_dir = home / ".claude" / "bin"
    entrypoint = bin_dir / "pi"
    if state.node is None:
        _remove(entrypoint)
        _warn(
            "  [warn] not publishing ~/.claude/bin/pi: pi needs Node >= "
            f"{PI_NODE_MIN} and this machine has {state.node_report}."
        )
        _warn(f"         pi is installed at {prefix_pi} but nothing here can run it.")
        return
    content = _wrapper(state.node, prefix_pi)
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        current = (
            entrypoint.read_bytes()
            if entrypoint.is_file() and not entrypoint.is_symlink()
            else None
        )
        if current != content.encode("utf-8"):
            # Removed first: a link left by an earlier install would be written THROUGH,
            # clobbering pi's own entrypoint on persistent storage.
            _remove(entrypoint)
            entrypoint.write_text(content, encoding="utf-8")
        if entrypoint.stat().st_mode & 0o777 != _WRAPPER_MODE:
            os.chmod(entrypoint, _WRAPPER_MODE)
    except OSError as exc:
        raise LinkError(f"could not publish {entrypoint}: {exc}") from exc
    print(
        f"  [ok]  ~/.claude/bin/pi → {prefix_pi} run by {state.node} "
        "(on-PATH; survives a space rebuild)"
    )
    # The helper-path step only writes ~/.claude/bin into the shell profiles, which never reach
    # this process, so the extension steps' PATH lookup would still miss the pi just published.
    # A private step this process launches inherits the extended PATH.
    path = environ.get("PATH", "")
    if str(bin_dir) not in path.split(os.pathsep):
        environ["PATH"] = f"{bin_dir}{os.pathsep}{path}" if path else str(bin_dir)


# --- pi-settings (Step 5d, IN-51) -----------------------------------------------------------


def _pi_home(home: Path, environ: Mapping[str, str]) -> Path:
    """pi follows Node's ``os.homedir()``, which is ``USERPROFILE`` on Windows."""
    profile = environ.get("USERPROFILE")
    return Path(profile) if profile else home


def _load_object(path: Path, source: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _warn(f"  [warn] {source} is invalid JSON; treating it as empty: {error}")
        return {}
    if not isinstance(value, dict):
        _warn(f"  [warn] {source} must be a JSON object; treating it as empty")
        return {}
    return value


def _deep_merge(existing: dict, managed: dict) -> dict:
    for key, value in managed.items():
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            _deep_merge(existing[key], value)
        else:
            existing[key] = value
    return existing


def render_settings(template: Path, destination: Path) -> str | None:
    """Merge the template's managed keys into the user-owned settings file.

    pi rewrites ``settings.json`` when a user changes a setting, so the file is rendered, never
    linked: managed values win and every user-local value is kept. Returns ``"rendered"`` or
    ``"unchanged"`` (a semantically identical file is not rewritten), or ``None`` after a warning
    when the template is unusable or the file cannot be written.
    """
    try:
        managed = json.loads(template.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _warn(f"  [warn] pi template '{template}' is invalid JSON: {error}")
        return None
    if not isinstance(managed, dict):
        _warn(f"  [warn] pi template '{template}' must be a JSON object")
        return None
    existing = (
        _load_object(destination, f"existing pi settings '{destination}'")
        if destination.is_symlink() or destination.exists()
        else {}
    )
    rendered = _deep_merge(existing, managed)
    if not destination.is_symlink() and destination.exists():
        try:
            on_disk = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            on_disk = None
        if on_disk == rendered:
            return "unchanged"
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".settings.json.", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(rendered, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError as error:
        _warn(f"  [warn] could not write {destination}: {error}")
        return None
    return "rendered"


def _wire_lifecycle(checkout: Path, pi_home: Path, environ: Mapping[str, str]) -> None:
    """Link the lifecycle extension's two modules; pi discovers a subdirectory's ``index.ts``."""
    source = checkout / "pi" / "extensions" / "lifecycle"
    if not ((source / "index.ts").is_file() and (source / "handoff.ts").is_file()):
        _warn("  [skip] Pi lifecycle extension source is incomplete")
        return
    target = pi_home / ".pi" / "agent" / "extensions" / "lifecycle"
    for module in ("handoff.ts", "index.ts"):
        publish_link(source / module, target / module, environ)
    print(
        "  [ok]  Pi lifecycle extension wired (durable save-state and acknowledgement)"
    )


def _check_auth(checkout: Path, pi_home: Path, environ: Mapping[str, str]) -> None:
    auth = pi_home / ".pi" / "agent" / "auth.json"
    gate = checkout / "scripts" / "pi_pilot_auth_gate.py"
    if not auth.is_file():
        print(
            "  [skip] ~/.pi/agent/auth.json is absent; complete the one-time /login in "
            "docs/procedures/pi-cli-codex-lane-setup.md"
        )
        return
    if not gate.is_file():
        print(
            "  [skip] pi auth shape check unavailable: scripts/pi_pilot_auth_gate.py not found"
        )
        return
    try:
        passed = (
            subprocess.run(
                [sys.executable, str(gate)],
                stdin=subprocess.DEVNULL,
                env=dict(environ),
                check=False,
                timeout=_PROBE_TIMEOUT_S,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        passed = False
    if passed:
        print("  [ok]  ~/.pi/agent/auth.json passed shape verification")
    else:
        _warn(
            "  [warn] ~/.pi/agent/auth.json failed shape verification (advisory only)"
        )


def install_settings(
    checkout: Path, home: Path, ai_host: str, environ: Mapping[str, str]
) -> None:
    """install.sh's retired Step 5d (pi): settings, the lifecycle extension, the auth check."""
    if not (checkout / "pi").is_dir():
        print("  [skip] no pi/ directory in ai-harness (pi integration not set up)")
        return
    pi_home = _pi_home(home, environ)
    profile = environ.get("USERPROFILE")
    if profile and profile != str(home):
        _warn("  [warn] USERPROFILE and HOME differ; pi follows USERPROFILE on Windows")
        _warn(f"         USERPROFILE={profile}")
        _warn(f"         HOME={home}")
    agent_dir = pi_home / ".pi" / "agent"
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LinkError(f"could not create {agent_dir}: {exc}") from exc
    template = checkout / "pi" / f"settings.{ai_host}.json"
    if template.is_file():
        result = render_settings(template, agent_dir / "settings.json")
        if result is not None:
            print(
                f"  [ok]  ~/.pi/agent/settings.json {result} from "
                f"pi/settings.{ai_host or 'unset'}.json"
            )
            _wire_lifecycle(checkout, pi_home, environ)
    else:
        _warn(f"  [skip] no pi settings template for AI_HOST='{ai_host or 'unset'}'")
    _check_auth(checkout, pi_home, environ)


# --- pi-wq-link (the end of Step 5f, IN-53) -------------------------------------------------


def link_work_queue(checkout: Path, home: Path, environ: Mapping[str, str]) -> None:
    """The fleet's own work-queue extension (ISSUE-REF): a link, which pi auto-discovers.

    Deliberately not a ``settings.json`` ``extensions`` entry: an absolute checkout path in a
    tracked template is a migration hazard. The version gate performs the refusal, so below the
    floor the link is unreachable rather than created beside a warning.
    """
    source = checkout / "pi" / "extensions" / "wq" / "index.ts"
    link = _pi_home(home, environ) / ".pi" / "agent" / "extensions" / "wq.ts"
    if not source.is_file():
        print(f"  [skip] pi work-queue extension: {source} is absent")
        return
    pi = resolve_pi()
    if pi is None:
        print("  [skip] pi work-queue extension: pi not on PATH")
        return
    _code, report = _output([str(pi), "--version"])
    version = semver_token(report)
    if version is None:
        _warn(
            "  [warn] pi work-queue extension NOT installed: pi reported no version, so the"
        )
        _warn(
            f"         {PI_MIN_VERSION} floor its extension events need could not be checked."
        )
        return
    if not semver_at_least(version, PI_MIN_VERSION):
        _warn(
            f"  [warn] pi work-queue extension NOT installed: pi {version} is below the"
        )
        _warn(
            f"         {PI_MIN_VERSION} floor its extension events need (D-15). A full"
        )
        _warn("         install.sh upgrades pi, after which this step installs it.")
        return
    if not publish_link(source, link, environ):
        return
    print(
        f"  [ok]  ~/.pi/agent/extensions/wq.ts → pi/extensions/wq/index.ts (pi {version})"
    )


def install_work_queue(checkout: Path, home: Path, environ: Mapping[str, str]) -> None:
    """The work-queue link, on first-time installs only, inside Step 5f's ``--update`` gate."""
    if update_mode(environ):
        return
    link_work_queue(checkout, home, environ)
