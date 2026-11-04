"""Node, npm and the two npm toolchains: install.sh's retired Node/npm helper block and Steps 11e and
11f (ISSUE-REF batch C-C1, inventory rows IN-61 and IN-69).

``ensure_npm`` is the shell's ``_aih_ensure_npm``, a three-rung provisioner: a user-local version
manager, then Homebrew, both unelevated; then a system package manager under ``sudo -n`` where this
account already holds password-free elevation for it; else a ``node-toolchain`` manual step with one
runnable line per option. A Node above the validated major is replaced on PATH by the validated one
where Homebrew can supply it, because playwright's browser install deadlocks above it.

``provision`` is the ``node-toolchain`` step: it runs the ladder and hands any PATH change back to
install.sh through ``AI_HARNESS_EXPORTS_FILE``, so a Node it provisions is on the PATH every later
step inherits (D-C12, G11). ``install_repo_dependencies`` is Step 11e (the checkout's own npm
devDependencies) and ``install_presentation_toolchain`` is Step 11f (the presentation skill's own
manifest, its Chromium download and, on Linux with apt-get, Chromium's OS libraries).

Every arm warns and continues; nothing here fails the install. Every subprocess runs through
``dependency_bootstrap.run_bounded``: a wall-clock bound, its own process group, stdin closed. The
output a shell step showed on the terminal is relayed with credential shapes redacted.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import sys
from collections.abc import MutableMapping
from pathlib import Path

from . import dependency_bootstrap, manual_steps
from .pi_integration import update_mode

#: playwright's browser install deadlocks above this major (measured on example-client-host-mac 2026-09-20: Node
#: 26.9.0 parked indefinitely, Node 24.21.0 finished in 25 s). ``AIH_NODE_MAX_MAJOR`` overrides it.
NODE_MAX_MAJOR = "24"
NODE_MAX_MAJOR_ENV = "AIH_NODE_MAX_MAJOR"
#: What ``skills/presentation/scripts/presentation-toolchain.mjs`` reads; defaults to the ceiling.
PRESENTATION_MAX_ENV = "PRESENTATION_MAX_NODE_MAJOR"
PRESENTATION_BOUND_ENV = "PRESENTATION_INSTALL_BOUND_SECS"
#: About seven times the measured 162 MiB Chromium download on example-client-host-mac.
PRESENTATION_BOUND_SECONDS = "900"
#: The toolchain script's own exit status for a Node major it has not validated.
TOOLCHAIN_REFUSED_NODE = 4
EXPORTS_FILE_ENV = "AI_HARNESS_EXPORTS_FILE"

NODE_INSTALL_SECONDS = 900.0
NODE_INSTALL_GRACE_SECONDS = 15.0
PLUGIN_ADD_SECONDS = 300.0
SHELL_ENV_SECONDS = 60.0
NPM_INSTALL_SECONDS = 900.0
DEPS_INSTALL_SECONDS = 900.0
RETIRE_SECONDS = 120.0
PROBE_SECONDS = 60.0

NODE_MANUAL_STEP = "node-toolchain"
BROWSER_DEPS_MANUAL_STEP = "presentation-browser-deps"
NODE_SUDOERS = "/etc/sudoers.d/ai-harness-node"
APT_SUDOERS = "/etc/sudoers.d/ai-harness-apt"
_VERSION_MANAGERS = ("mise", "fnm", "volta", "asdf")
_PACKAGE_MANAGERS = ("apt-get", "dnf", "yum", "zypper", "pacman", "apk")
#: install.sh's `eval "$(fnm env)" && fnm use N`, run in a bash child that prints back only PATH and
#: the FNM_* variables fnm sets, NUL-separated.
_FNM_ENV_SCRIPT = (
    'eval "$(fnm env)" || exit 1\n'
    'fnm use "$1" >/dev/null 2>&1\n'
    "for _name in $(compgen -e); do\n"
    '  case "$_name" in PATH | FNM_*) printf \'%s=%s\\0\' "$_name" "${!_name}" ;; esac\n'
    "done\n"
)


def _out(line: str) -> None:
    print(line)


def _err(line: str) -> None:
    print(line, file=sys.stderr)


def _relay(result: dependency_bootstrap.Bounded) -> None:
    dependency_bootstrap.relay_redacted(result.stdout, sys.stdout)
    dependency_bootstrap.relay_redacted(result.stderr, sys.stderr)


def max_major(environ: MutableMapping[str, str]) -> str:
    """``${AIH_NODE_MAX_MAJOR:-24}``."""
    return environ.get(NODE_MAX_MAJOR_ENV) or NODE_MAX_MAJOR


def presentation_max_major(environ: MutableMapping[str, str]) -> str:
    """``${PRESENTATION_MAX_NODE_MAJOR:-$AIH_NODE_MAX_MAJOR}``, so the two cannot disagree."""
    return environ.get(PRESENTATION_MAX_ENV) or max_major(environ)


def _toolchain_env(environ: MutableMapping[str, str]) -> dict[str, str]:
    """The environment of every npm and node child: install.sh exported the ceiling to them all."""
    return {**environ, PRESENTATION_MAX_ENV: presentation_max_major(environ)}


def node_major(environ: MutableMapping[str, str]) -> int | None:
    """The major of the ``node`` on PATH, or None when it is absent or prints no ``vN.``."""
    result = dependency_bootstrap.run_bounded(
        ["node", "--version"], seconds=PROBE_SECONDS, env=environ, capture=True
    )
    if result.returncode != 0:
        return None
    major = result.stdout.strip().removeprefix("v").partition(".")[0]
    return int(major) if major.isdigit() else None


def _major_is_supported(environ: MutableMapping[str, str]) -> bool:
    major = node_major(environ)
    if major is None:
        return False
    try:
        return major <= int(max_major(environ))
    except ValueError:
        return False


def _hand_back(line: str, environ: MutableMapping[str, str]) -> None:
    """Append ``line`` to the exports file install.sh sources after this step exits 0."""
    target = environ.get(EXPORTS_FILE_ENV)
    if not target:
        return
    try:
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")
    except OSError as exc:
        _err(
            f"  [warn] could not hand the Node toolchain back to install.sh's PATH: {exc}"
        )


def _prepend_path(directory: str, environ: MutableMapping[str, str]) -> None:
    """``PATH="<directory>:$PATH"; export PATH``, here and in install.sh.

    The exports line keeps bash's ``:`` and ``$PATH``: install.sh sources it into its own PATH, so
    every later step, private or core, inherits the provisioned Node.
    """
    current = environ.get("PATH", "")
    environ["PATH"] = f"{directory}{os.pathsep}{current}" if current else directory
    _hand_back(f'export PATH={shlex.quote(directory)}:"$PATH"', environ)


def use_supported_node(environ: MutableMapping[str, str]) -> bool:
    """``_aih_use_supported_node``: the validated major's keg-only Homebrew formula, in front on PATH.

    Homebrew does not link ``node@N`` into its prefix, so it can be installed and still invisible to
    ``command -v node``; its own ``bin`` goes in front of PATH for the rest of the run.
    """
    if shutil.which("brew", path=environ.get("PATH")) is None:
        return False
    formula = f"node@{max_major(environ)}"
    prefix = _brew_prefix(formula, environ)
    if not prefix or not os.access(Path(prefix) / "bin" / "node", os.X_OK):
        _out(f"  [node] provisioning {formula} (the validated major)...")
        dependency_bootstrap.run_bounded(
            ["brew", "install", formula],
            seconds=NODE_INSTALL_SECONDS,
            grace=NODE_INSTALL_GRACE_SECONDS,
            env=environ,
        )
        prefix = _brew_prefix(formula, environ)
    if not prefix or not os.access(Path(prefix) / "bin" / "node", os.X_OK):
        return False
    _prepend_path(str(Path(prefix) / "bin"), environ)
    version = dependency_bootstrap.run_bounded(
        ["node", "--version"], seconds=PROBE_SECONDS, env=environ, capture=True
    )
    shown = version.stdout.strip() if version.returncode == 0 else ""
    _out(f"  [ok]  using Node {shown} from {prefix} for the rest of this run")
    return True


def _brew_prefix(formula: str, environ: MutableMapping[str, str]) -> str:
    result = dependency_bootstrap.run_bounded(
        ["brew", "--prefix", formula],
        seconds=PROBE_SECONDS,
        env=environ,
        capture=True,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def package_manager(environ: MutableMapping[str, str]) -> str:
    """``_aih_node_pkg_manager``: the first system package manager on PATH, or ``""``."""
    path = environ.get("PATH")
    return next((m for m in _PACKAGE_MANAGERS if shutil.which(m, path=path)), "")


def install_command(manager: str) -> str:
    """``_aih_node_install_command``: the one line the operator runs to install Node by hand."""
    return {
        "apt-get": "sudo apt-get update && sudo apt-get install -y nodejs npm",
        "dnf": "sudo dnf install -y nodejs npm",
        "yum": "sudo yum install -y nodejs npm",
        "zypper": "sudo zypper --non-interactive install nodejs npm",
        "pacman": "sudo pacman -S --noconfirm nodejs npm",
        "apk": "sudo apk add --no-cache nodejs npm",
    }.get(manager, "")


def _install_argvs(manager: str) -> list[list[str]]:
    """``install_command`` as argv lists run in order, each stopping the chain when it fails."""
    return {
        "apt-get": [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "nodejs", "npm"],
        ],
        "dnf": [["dnf", "install", "-y", "nodejs", "npm"]],
        "yum": [["yum", "install", "-y", "nodejs", "npm"]],
        "zypper": [["zypper", "--non-interactive", "install", "nodejs", "npm"]],
        "pacman": [["pacman", "-S", "--noconfirm", "nodejs", "npm"]],
        "apk": [["apk", "add", "--no-cache", "nodejs", "npm"]],
    }[manager]


def _npm_on_path(environ: MutableMapping[str, str]) -> bool:
    return shutil.which("npm", path=environ.get("PATH")) is not None


def _bounded_install(argv: list[str], environ: MutableMapping[str, str]) -> int:
    return dependency_bootstrap.run_bounded(
        argv,
        seconds=NODE_INSTALL_SECONDS,
        grace=NODE_INSTALL_GRACE_SECONDS,
        env=environ,
    ).returncode


def _fnm(major: str, environ: MutableMapping[str, str]) -> None:
    """``fnm install N && eval "$(fnm env)" && fnm use N``, the evaluated variables kept."""
    if _bounded_install(["fnm", "install", major], environ) != 0:
        return
    result = dependency_bootstrap.run_bounded(
        ["bash", "-c", _FNM_ENV_SCRIPT, "fnm-env", major],
        seconds=SHELL_ENV_SECONDS,
        env=environ,
        capture=True,
    )
    dependency_bootstrap.relay_redacted(result.stderr, sys.stderr)
    if result.returncode != 0:
        return
    for record in result.stdout.split("\0"):
        name, found, value = record.partition("=")
        if not found:
            continue
        if name == "PATH":
            _adopt_path(value, environ)
            continue
        environ[name] = value
        _hand_back(f"export {name}={shlex.quote(value)}", environ)


def _adopt_path(value: str, environ: MutableMapping[str, str]) -> None:
    """Take the PATH ``fnm env`` produced: its new leading entries are prepended, as eval did."""
    current = environ.get("PATH", "")
    if value == current:
        return
    if current and value.endswith(f"{os.pathsep}{current}"):
        added = value[: -len(current) - 1].split(os.pathsep)
        for directory in reversed([entry for entry in added if entry]):
            _prepend_path(directory, environ)
        return
    environ["PATH"] = value
    _hand_back(f"export PATH={shlex.quote(value)}", environ)


def _version_manager(manager: str, environ: MutableMapping[str, str]) -> None:
    major = max_major(environ)
    if manager == "mise":
        _bounded_install(["mise", "use", "-g", f"node@{major}"], environ)
    elif manager == "fnm":
        _fnm(major, environ)
    elif manager == "volta":
        _bounded_install(["volta", "install", f"node@{major}"], environ)
    else:
        # `asdf global` is not decoration: without it the shim resolves to nothing.
        dependency_bootstrap.run_bounded(
            ["asdf", "plugin", "add", "nodejs"], seconds=PLUGIN_ADD_SECONDS, env=environ
        )
        if (
            _bounded_install(["asdf", "install", "nodejs", f"latest:{major}"], environ)
            == 0
        ):
            _bounded_install(["asdf", "global", "nodejs", f"latest:{major}"], environ)


def _elevated(argv: list[str]) -> list[str]:
    return argv if dependency_bootstrap._is_root() else ["sudo", "-n", *argv]


def _register_node_manual_step(manager: str, command: str) -> None:
    step = manual_steps.ManualStep(
        id=NODE_MANUAL_STEP,
        title="Provision Node: npm is absent and sudo needs a password",
        why=(
            "npm is required and this account cannot elevate without a password. Run ONE "
            f"command: OPTION A (the first) grants password-free sudo for {manager} only, then "
            "re-run install.sh, which provisions Node itself; OPTION B (the second) installs "
            "Node yourself."
        ),
        commands=[
            f'printf \'%s ALL=(root) NOPASSWD: %s\\n\' "$USER" "$(command -v {manager})" '
            f"| sudo tee {NODE_SUDOERS} >/dev/null && sudo chmod 0440 {NODE_SUDOERS} "
            f"&& sudo visudo -cf {NODE_SUDOERS}",
            command,
        ],
        follow=(
            "re-run bash install.sh; the step is done when it stops registering node-toolchain"
        ),
        source_step=NODE_MANUAL_STEP,
    )
    try:
        if manual_steps.register(step):
            _err(manual_steps.notice(step, manual_steps.ledger_path()))
    except (OSError, ValueError, TypeError):
        _err(
            f"  [warn] manual step {NODE_MANUAL_STEP} not recorded (the manual-steps ledger "
            f"could not be written); install Node with: {command}"
        )


def _present_npm(environ: MutableMapping[str, str]) -> bool:
    """npm is on PATH: True, with a Node above the ceiling swapped for the validated one where
    Homebrew can supply it, and otherwise a warning."""
    ceiling = max_major(environ)
    if _major_is_supported(environ):
        return True
    major = node_major(environ)
    shown = "?" if major is None else str(major)
    _out(f"  [node] Node {shown} exceeds the validated major ({ceiling})")
    if use_supported_node(environ):
        return True
    # Not a failure: npm still works, so Step 11e's `npm ci` must still run.
    for line in (
        "  [warn] no supported Node could be provisioned, and the one on PATH is above the",
        "         validated major. npm still works, so this is not fatal -- but the presentation",
        "         toolchain will REFUSE to install (playwright's browser install deadlocks there)",
        "         and the presentation tests will fail until it is installed.",
        f"         Install Node {ceiling}, or set PRESENTATION_MAX_NODE_MAJOR once a",
        "         newer major has been VERIFIED to complete the browser install.",
    ):
        _err(line)
    return True


def _unelevated(environ: MutableMapping[str, str]) -> bool:
    """Rung 1, a user-local version manager pinned to the validated major, then Homebrew, which
    writes only under its own prefix; True once npm resolves. No elevation."""
    ceiling = max_major(environ)
    for manager in _VERSION_MANAGERS:
        if shutil.which(manager, path=environ.get("PATH")) is None:
            continue
        _out(
            f"  [node] npm absent; installing Node {ceiling} through {manager} "
            "(no elevation needed)..."
        )
        _version_manager(manager, environ)
        if _npm_on_path(environ):
            return True
    # `node@N`, never the unversioned formula, which resolves to the newest major.
    if shutil.which("brew", path=environ.get("PATH")) is None:
        return False
    _out("  [node] npm absent; installing Node through Homebrew...")
    return use_supported_node(environ) and _npm_on_path(environ)


def _with_held_sudo(manager: str, environ: MutableMapping[str, str]) -> bool:
    """Rung 2: elevation this account ALREADY holds for THIS package manager (ISSUE-REF)."""
    _out(
        f"  [node] npm absent; installing via {manager} with existing passwordless sudo..."
    )
    for argv in _install_argvs(manager):
        result = dependency_bootstrap.run_bounded(
            _elevated(argv),
            seconds=NODE_INSTALL_SECONDS,
            grace=NODE_INSTALL_GRACE_SECONDS,
            env=environ,
            capture=True,
        )
        _relay(result)
        if result.returncode != 0:
            _err(f"  [warn] Node install via {manager} failed")
            break
    if _npm_on_path(environ):
        return True
    # Elevation worked, so blaming it would name a cause this branch has just disproved.
    _err(
        f"  [warn] {manager} ran under passwordless sudo but npm is still not on PATH;"
    )
    _err(
        "         install Node manually and re-run, or check the package manager's output."
    )
    return False


def ensure_npm(environ: MutableMapping[str, str] = os.environ) -> bool:
    """``_aih_ensure_npm``: True when npm is usable, having provisioned it where it can."""
    if _npm_on_path(environ):
        return _present_npm(environ)
    if _unelevated(environ):
        return True
    manager = package_manager(environ)
    if not manager:
        _err(
            "  [warn] npm is absent and no supported installer was found on this machine."
        )
        return False
    if dependency_bootstrap.sudo_can_run(manager, "--version", env=environ):
        return _with_held_sudo(manager, environ)
    # Rung 3: genuinely blocked. One runnable line per option (ISSUE-REF D-5).
    _register_node_manual_step(manager, install_command(manager))
    return False


def provision(environ: MutableMapping[str, str] = os.environ) -> None:
    """The ``node-toolchain`` step: the ladder, nothing under ``--update``; never fails."""
    if update_mode(environ):
        return
    ensure_npm(environ)


def _newer(path: Path, than: Path) -> bool:
    """bash's ``[[ path -nt than ]]``: path exists and is newer, or than does not exist."""
    try:
        mine = path.stat().st_mtime_ns
    except OSError:
        return False
    try:
        return mine > than.stat().st_mtime_ns
    except OSError:
        return True


def install_repo_dependencies(
    checkout: Path, environ: MutableMapping[str, str] = os.environ
) -> None:
    """Step 11e: the checkout's own npm devDependencies (knip, jscpd); never fails.

    Nothing under ``--update``: the post-merge hook runs ``install.sh --update --hook``, where a slow
    npm install once killed git mid-hook (ISSUE-REF). ``npm ci`` when a lockfile is present, and only
    when the lockfile is newer than the installed tree or no tree exists.
    """
    if update_mode(environ) or not (checkout / "package.json").is_file():
        return
    if not ensure_npm(environ):
        _err("  [warn] ai-harness devDependencies not installed: npm is unavailable.")
        _err(
            "         knip, jscpd and the presentation pipeline will FAIL until Node is present."
        )
        return
    modules = checkout / "node_modules"
    lockfile = checkout / "package-lock.json"
    if modules.is_dir() and not _newer(lockfile, modules):
        _out("  [ok]  ai-harness devDependencies already installed")
        return
    verb = "ci" if lockfile.is_file() else "install"
    if verb == "ci":
        _out("  [npm] Installing ai-harness devDependencies (npm ci)...")
    else:
        _out(
            "  [npm] Installing ai-harness devDependencies (npm install, no lockfile)..."
        )
    result = dependency_bootstrap.run_bounded(
        ["npm", verb],
        seconds=NPM_INSTALL_SECONDS,
        env=_toolchain_env(environ),
        capture=True,
        cwd=checkout,
    )
    _relay(result)
    if result.returncode != 0:
        note = dependency_bootstrap.expiry_note(result, NPM_INSTALL_SECONDS)
        _err(
            f"  [warn] npm {verb} failed{note} — knip/jscpd/slidev tests will fail until it "
            "succeeds"
        )


def _presentation_bound(environ: MutableMapping[str, str]) -> tuple[str, float]:
    text = environ.get(PRESENTATION_BOUND_ENV) or PRESENTATION_BOUND_SECONDS
    try:
        return text, float(text)
    except ValueError:
        return PRESENTATION_BOUND_SECONDS, float(PRESENTATION_BOUND_SECONDS)


def _register_browser_deps_manual_step(skill: Path) -> None:
    by_hand = f"(cd {skill} && npx playwright install-deps chromium)"
    step = manual_steps.ManualStep(
        id=BROWSER_DEPS_MANUAL_STEP,
        title="Install Chromium's system libraries: sudo needs a password",
        why=(
            "Chromium needs OS shared libraries no npm install provides, and this account cannot "
            "elevate without a password; Slidev's HTML build works without them, PDF and PNG "
            "export does not. Run ONE command: OPTION A (the first) grants password-free sudo for "
            "apt-get only, then re-run install.sh, which installs them itself; OPTION B (the "
            "second) installs them yourself."
        ),
        commands=[
            'printf \'%s ALL=(root) NOPASSWD: %s\\n\' "$USER" "$(command -v apt-get)" '
            f"| sudo tee {APT_SUDOERS} >/dev/null && sudo chmod 0440 {APT_SUDOERS} "
            f"&& sudo visudo -cf {APT_SUDOERS}",
            by_hand,
        ],
        follow=(
            "re-run bash install.sh; the step is done when it stops registering "
            "presentation-browser-deps"
        ),
        source_step="presentation-toolchain",
    )
    try:
        if manual_steps.register(step):
            _err(manual_steps.notice(step, manual_steps.ledger_path()))
    except (OSError, ValueError, TypeError):
        _err(
            f"  [warn] manual step {BROWSER_DEPS_MANUAL_STEP} not recorded (the manual-steps "
            f"ledger could not be written); run: {by_hand}"
        )


def browser_dependencies(skill: Path, environ: MutableMapping[str, str]) -> None:
    """``_aih_presentation_browser_deps``: Chromium's OS libraries, on Linux with apt-get only.

    Elevation is used only where ``sudo -n apt-get --version`` already succeeds; otherwise a
    ``presentation-browser-deps`` manual step. ``playwright install-deps`` runs apt-get itself.
    """
    if platform.system() != "Linux":
        return
    if shutil.which("apt-get", path=environ.get("PATH")) is None:
        _err(
            "  [skip] Chromium system libraries: no apt-get (playwright install-deps is "
            "Debian/Ubuntu only)."
        )
        _err(
            "         Install Chromium's libraries with this distribution's package manager to render."
        )
        return
    if dependency_bootstrap.sudo_can_run("apt-get", "--version", env=environ):
        _out(
            "  [deps] Installing Chromium's system libraries (passwordless sudo already held)..."
        )
        result = dependency_bootstrap.run_bounded(
            ["npx", "playwright", "install-deps", "chromium"],
            seconds=DEPS_INSTALL_SECONDS,
            env=_toolchain_env(environ),
            capture=True,
            cwd=skill,
        )
        _relay(result)
        if result.returncode != 0:
            _err(
                "  [warn] Chromium system libraries not installed; PDF/PNG export will fail"
            )
        return
    _register_browser_deps_manual_step(skill)


def install_presentation_toolchain(
    checkout: Path, environ: MutableMapping[str, str] = os.environ
) -> None:
    """Step 11f: the presentation skill's own npm manifest and Chromium; never fails.

    First, under every mode, a dependency tree at the pre-fix non-hidden path is relocated by the
    toolchain script (ISSUE-REF). Then, outside ``--update``: the toolchain's own ``probe`` decides
    whether to install, the install is bounded, and the OS-library rung runs whatever the probe
    said. It CHECKS for node and npm rather than running the ladder again: Step 11e ran it
    immediately before and has already said why it failed.
    """
    skill = checkout / "skills" / "presentation"
    toolchain = skill / "scripts" / "presentation-toolchain.mjs"
    child_env = _toolchain_env(environ)
    path = environ.get("PATH")
    if (
        toolchain.is_file()
        and shutil.which("node", path=path)
        and (skill / "node_modules").is_dir()
    ):
        _out(
            "  [npm] Retiring the presentation skill's pre-fix node_modules (out of codex's "
            "skills walk)..."
        )
        retired = dependency_bootstrap.run_bounded(
            ["node", "scripts/presentation-toolchain.mjs", "retire-legacy-modules"],
            seconds=RETIRE_SECONDS,
            env=child_env,
            capture=True,
            cwd=skill,
        )
        _relay(retired)
        if retired.returncode != 0:
            _err(
                f"  [warn] could not retire {skill}/node_modules — codex's skills-catalog walk "
                "will keep descending into it and may produce no output at all"
            )
    if update_mode(environ) or not toolchain.is_file():
        return
    if not (shutil.which("node", path=path) and shutil.which("npm", path=path)):
        _err(
            "  [warn] presentation toolchain not installed: node or npm is unavailable."
        )
        _err(
            "         The presentation pipeline and its tests will FAIL until Node is present."
        )
        return
    probe = dependency_bootstrap.run_bounded(
        ["node", "scripts/presentation-toolchain.mjs", "probe"],
        seconds=PROBE_SECONDS,
        env=child_env,
        cwd=skill,
    )
    if probe.returncode == 0:
        _out("  [ok]  presentation toolchain already installed")
    else:
        _install_toolchain(skill, child_env, environ)
    # Outside the probe branch: `probe` knows nothing about OS libraries.
    browser_dependencies(skill, environ)


def _install_toolchain(
    skill: Path, child_env: dict[str, str], environ: MutableMapping[str, str]
) -> None:
    _out("  [npm] Installing presentation toolchain (Slidev + Playwright Chromium)...")
    shown, seconds = _presentation_bound(environ)
    result = dependency_bootstrap.run_bounded(
        ["node", "scripts/presentation-toolchain.mjs", "install"],
        seconds=seconds,
        grace=NODE_INSTALL_GRACE_SECONDS,
        env=child_env,
        capture=True,
        cwd=skill,
    )
    _relay(result)
    if result.expired:
        for line in (
            f"  [warn] presentation toolchain install exceeded {shown}s and was",
            "         stopped so the rest of the install could proceed. Nothing was banked -- the",
            "         retry re-downloads in full, so run it where you can watch it:",
            f"           (cd {skill} && node scripts/presentation-toolchain.mjs install)",
            "         The presentation tests will fail until it succeeds.",
        ):
            _err(line)
    elif result.returncode == TOOLCHAIN_REFUSED_NODE:
        for line in (
            "  [warn] presentation toolchain install refused this machine's Node major (see its",
            "         message above for the supported one). The presentation tests will fail until",
            "         the install is re-run under a Node it accepts.",
        ):
            _err(line)
    elif result.returncode != 0:
        _err(
            "  [warn] presentation toolchain install failed — the presentation tests will fail "
            "until it succeeds"
        )
