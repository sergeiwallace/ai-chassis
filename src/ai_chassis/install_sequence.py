"""The full apply's install sequence: one policy entry per ``APPLY_STEPS`` step (T-3.6, R-1).

Each entry reads install.sh's hand-off line for its step (install.sh blob 4d37921, or the working
tree's install.sh for a step handed off after that blob) as data:

- ``kind``: what a failure does. ``defer`` is ``|| _defer_failure self ...`` (record it, run on,
  exit 1 at the end); ``stop`` is ``|| _exit_reporting_deferred 1 self`` (report the deferrals
  and exit 1 now); ``stop-on-3`` is editable-tools' arm (exit 3 stops, any other failure
  defers); ``record`` keeps the status only (``deferred-failures-report``, ``|| rc=$?``).
- ``stop_class``: for a stop, the class ``_exit_reporting_deferred`` is given (``self``, or
  ``unknown`` for the launcher declaration, whose two failure halves are not yet told apart).
- ``guard``: the ``_settings_writable`` label around the line, so a refused ``settings`` step
  skips it; ``sets_refusal`` marks the one step whose failure refuses (``settings``).
- ``mode_env``: each variable the line sets for the step alone, and where its value comes from.
- ``gate``: a shell condition around the line other than the settings guard.

A grouped line (``a,b,c``) gives each member the same policy, and the sequencer runs each member
on its own. The private half of the table is read at call time from ``[orchestrator].
install_sequence``: the defer prose (summary and remedy) for every step, which is fleet text,
and the mode variables only a private step reads, whose names the public core may not carry
(the private rows' parity assertions forbid them in ``src/ai_chassis``).

The second half of the module is what the full apply already did differently from install.sh,
kept as data so no step's place in the sequence is carried by code order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

DEFER = "defer"
STOP = "stop"
STOP_ON_3 = "stop-on-3"
RECORD = "record"
KINDS = (DEFER, STOP, STOP_ON_3, RECORD)

#: The right-hand side of a mode variable on a hand-off line.
FROM_UPDATE = "update"  # "$UPDATE_MODE": 1 under --update, else 0
FROM_HOOK = "hook"  # "$HOOK_MODE": 1 under --hook, else 0
FROM_LOCAL_ONLY = "local-only"  # "$LOCAL_ONLY": 1 under --local-only, else 0
PINNED_ZERO = "0"  # a literal 0
FROM_SETTINGS_REFUSAL = "settings-refused"  # "$_settings_step_refused"
FROM_MACHINE = "machine"  # "$MACHINE", the canonical machine
FROM_PYTHON_BIN = "python-bin"  # "$_PYTHON_BIN", the fleet-floor interpreter
FROM_BEDROCK_AUTH = "bedrock-auth"  # "$_bedrock_api_auth_enabled", its hand-back
FROM_INVOKED_DIR = (
    "invoked-dir"  # "$_INVOKED_DIR", the checkout install.sh was invoked from
)

#: install.sh's shell gates, each named by what it tests.
GATE_TOOL_ON_PATH = "ai-harness on PATH"
GATE_MANUAL_LEDGER = "manual-steps ledger set"
GATE_DEFERRAL_LEDGER = "deferral ledger non-empty"

UPDATE_ENV = "AIH_INSTALL_UPDATE_MODE"
SETTINGS_REFUSED_ENV = "AIH_SETTINGS_STEP_REFUSED"
#: The claude-readiness step's copy of bedrock-auth's verdict: install.sh's shell-only
#: `_bedrock_api_auth_enabled`, which it sets to 0 before that step and the step hands back.
BEDROCK_AUTH_ENV = "AIH_BEDROCK_API_AUTH_ENABLED"
BEDROCK_AUTH_HAND_BACK = "_bedrock_api_auth_enabled"
BEDROCK_AUTH_DEFAULT = "0"

_UPDATE = ((UPDATE_ENV, FROM_UPDATE),)


@dataclass(frozen=True)
class StepPolicy:
    name: str
    kind: str = DEFER
    stop_class: str = "self"
    guard: str = ""
    sets_refusal: bool = False
    mode_env: tuple[tuple[str, str], ...] = ()
    gate: str = ""


_P = StepPolicy

#: APPLY_STEPS entries that are not sequence steps, each with why. install.sh never hands it off.
NOT_SEQUENCED: dict[str, str] = {
    "agent-mail-notifier": "the private messaging step runs it in process, after deciding "
    "whether this machine installs or removes the notifier (systemd, launchd or Windows Task "
    "Scheduler); wake-sidecar runs it too",
    "core-mcp": "the plugins step runs it in process, right after the skills plugin install and "
    "before the settings merge and restart-mcp, so install.sh's hand-off line is unchanged",
}

#: Sequence steps install.sh has no hand-off line for, each with why: the golden derivation from
#: install.sh skips them, and the full apply runs them at their position.
NOT_HANDED_OFF: dict[str, str] = {
    "windows-native-flush": "install.sh runs Step 12b's `_flush_win_cmd` in its own shell; the "
    "full apply flushes its queue here, so skills-canary finds the skills junction (ISSUE-REF)",
}

#: The span install.sh holds its settings write lock across, first and last step (ISSUE-REF).
SETTINGS_LOCK_FROM = "plugins"
SETTINGS_LOCK_THROUGH = "skills-settings-env"

#: In APPLY_STEPS order, one entry per step except NOT_SEQUENCED.
SEQUENCE: tuple[StepPolicy, ...] = (
    # The worktree preflight (T6-2, ruling R-2) stops the install before any other step writes.
    _P("install-preflight", STOP),
    _P("projects-link"),
    _P("main-branch-check"),
    _P("skills-catalog", STOP),
    _P("run-context-cutover", STOP),
    _P("profile-migration"),
    _P("host-identity", STOP),
    _P("host-alias"),
    _P("machine-id"),
    _P("machine-registry-export", STOP),
    _P("billing-mode"),
    _P("xdg-state-home"),
    _P("machine-marker"),
    _P("codex-route"),
    _P("windows-path-order", STOP),
    _P("zsh"),
    _P("gitleaks"),
    _P("doppler"),
    _P("claude-code"),
    _P("gh"),
    _P("jq"),
    _P("durable-tmux"),
    _P("context-dir"),
    _P("real-git"),
    _P("aido-config"),
    _P("rendered-links"),
    _P("prune-stale-root"),
    _P("python-shim"),
    _P("plugins"),
    _P("settings", sets_refusal=True),
    _P("machine-settings", guard="machine settings fragment"),
    _P("session-model", guard="session model"),
    _P("user-mcp-servers"),
    _P(
        "context-supervisor-config",
        STOP,
        mode_env=(("MACHINE", FROM_MACHINE), ("AIH_PYTHON_BIN", FROM_PYTHON_BIN)),
    ),
    _P("idle-wake-config", STOP),
    _P("statusline-links"),
    _P("helper-bins"),
    _P("notifier-app"),
    _P("ai-core-config"),
    _P("trusted-aido", mode_env=(("AIH_PYTHON_BIN", FROM_PYTHON_BIN),)),
    _P("mdb-dependencies"),
    _P("tier-b-entrypoints"),
    _P("wrapper-publication"),
    _P("mdb-wrapper"),
    _P("script-shims"),
    _P("retire-wrappers"),
    _P("mcp-wrapper-link"),
    _P("mcp-tool-source"),
    _P("helper-path"),
    _P("bedrock-auth", mode_env=((SETTINGS_REFUSED_ENV, FROM_SETTINGS_REFUSAL),)),
    _P("model-catalog"),
    _P("worktree-drift"),
    _P("goal-waker"),
    _P("machine-contention"),
    _P(
        "messaging",
        mode_env=(("MACHINE", FROM_MACHINE), ("AIH_PYTHON_BIN", FROM_PYTHON_BIN)),
    ),
    _P("cross-pair-trust"),
    _P("tier-b-entrypoint-check", STOP),
    _P("wake-sidecar", mode_env=(("MACHINE", FROM_MACHINE),)),
    _P("shared-agents"),
    # The one-shot research-write profile migration (T6-3, ruling R-4), where install.sh ran it.
    _P("research-write-retire"),
    _P("node-toolchain", mode_env=((UPDATE_ENV, PINNED_ZERO),)),
    _P("codex", mode_env=_UPDATE),
    _P("codex-prefix-check", mode_env=_UPDATE),
    _P("pi-bootstrap", mode_env=_UPDATE),
    _P("pi-wrapper", mode_env=_UPDATE),
    _P("pi-settings", mode_env=_UPDATE),
    _P("pi-extensions", mode_env=_UPDATE),
    _P("pi-wq-link", mode_env=_UPDATE),
    _P("account-pr-template"),
    _P("github-https-auth"),
    _P("editable-tools", STOP_ON_3),
    _P("session-names", STOP),
    # The pinned workflow linter warm (T6-5a, IN-137), where install.sh ran its header-less block.
    _P("actionlint"),
    _P("storage", gate=GATE_TOOL_ON_PATH),
    _P("repo-venvs"),
    _P("ai-cli-remote"),
    _P("ssh-config-hosts"),
    _P("ai-cli-environment"),
    _P("workload-cli"),
    _P("terminal-session-restore"),
    _P("session-boot-relaunch"),
    _P("external-host-installers"),
    _P("held-hook"),
    _P("prose-tools"),
    _P("hook-links", STOP),
    _P("hooks-steering-check", STOP, guard="hooks manifest"),
    _P("hooks-manifest", STOP, guard="hooks manifest"),
    _P("skills-settings-env", STOP, guard="skill env switches (claude-settings)"),
    _P("tmp-cleanup-timer"),
    _P("aws-token-warm-scheduler"),
    _P("launch-resume-schedulers"),
    _P("codex-update-scheduler"),
    _P("macos-process-broker"),
    _P("vscode-server-evidence"),
    _P("vscode-reload-extension"),
    _P("worktree-lifecycle-scheduler"),
    _P("skill-drift-scheduler"),
    _P("vendored-skill-check", STOP),
    _P("fork-sync-timer"),
    _P("skills", STOP),
    _P("repo-hook-self-heal"),
    _P("merge-reap-trigger"),
    _P("sibling-push-guard"),
    _P("jq-wrapper"),
    _P("dictation-settings-link"),
    _P("terminal-keybindings", STOP),
    _P("tmux-extended-keys"),
    _P("terminal-mode-reset"),
    _P("screenshot-upload"),
    _P("optional-cli-install", mode_env=_UPDATE),
    _P("capabilities"),
    _P("notifier-helper", mode_env=_UPDATE),
    _P("restart-mcp"),
    _P("task-store-provision", mode_env=_UPDATE),
    _P("task-store-mirror-migration"),
    _P("task-store-publication"),
    _P("npm-dev-dependencies", mode_env=_UPDATE),
    _P("presentation-toolchain", mode_env=_UPDATE),
    _P("template-tool"),
    _P("ssm-plugin"),
    _P("cache-links"),
    _P("template-asset-sync", STOP),
    # install.sh's `_flush_win_cmd` ends `|| true`, so a failed flush never fails it; the core's
    # flush raises (IN-44) and the full apply has exited 1 for it since T6-0a, so it defers.
    _P("windows-native-flush"),
    _P("skills-canary", STOP),
    _P("git-guardrails"),
    _P("ctime-agent-git-config"),
    _P("harness-root"),
    _P("boot-check"),
    _P("claude-daemon"),
    _P("credential-scope"),
    _P("personal-url-rewrite"),
    _P("personal-owner-helper"),
    _P("lint-baseline-pointer"),
    _P("checkout-venvs"),
    _P("firstmate-fork"),
    _P("chief-identity"),
    _P("remote-sync"),
    _P("canonical-copy-sync"),
    _P("canonical-readme-sync"),
    _P("trust-projects"),
    # install.sh's epilogue (T6-1c): each check runs after every other step, before the reports.
    _P(
        "claude-readiness",
        mode_env=(
            ("AIH_PYTHON_BIN", FROM_PYTHON_BIN),
            (BEDROCK_AUTH_ENV, FROM_BEDROCK_AUTH),
        ),
    ),
    _P("compact-wake-readiness", mode_env=(("AIH_PYTHON_BIN", FROM_PYTHON_BIN),)),
    _P(
        "launcher-declaration",
        STOP,
        stop_class="unknown",
        mode_env=(("AIH_PYTHON_BIN", FROM_PYTHON_BIN),),
    ),
    _P("manual-steps-report", gate=GATE_MANUAL_LEDGER),
    _P("deferred-failures-report", RECORD, gate=GATE_DEFERRAL_LEDGER),
)

# --- what the full apply already did differently from install.sh, each with its reason --------

#: A step the full apply runs early, mapped to the step it runs right after.
AHEAD_OF_POSITION: dict[str, str] = {
    # A required capability that is absent refuses before anything else mutates (the
    # machine-install contract); install.sh reports it after optional-cli-install.
    "capabilities": "prune-stale-root",
}
#: Steps whose failure stops the full apply although install.sh's line defers it.
FULL_APPLY_STOPS: dict[str, str] = {
    "aido-config": "an unreadable fleet aido config stops before the generation is installed",
    "storage": "a storage placement that cannot converge is the install going red on purpose "
    "(D-4, hardened)",
    "capabilities": "a required capability that is absent refuses before anything mutates",
}
#: Stops that exit with the step's own status, as the full apply always has, not install.sh's 1:
#: host-identity's refusal carries the machine-install contract's unknown-machine status.
OWN_STATUS = frozenset({"host-identity", *FULL_APPLY_STOPS})
#: Steps the full apply skips silently when their opt-in is false (D-C13); ``--only`` runs each
#: and it prints its own ``[skip]`` line.
CONFIG_GATES: dict[str, str] = {
    "machine-id": "modify_shell_profile",
    "xdg-state-home": "modify_shell_profile",
    "helper-path": "modify_shell_profile",
    "terminal-mode-reset": "modify_shell_profile",
    "git-guardrails": "modify_global_git",
    "trust-projects": "mark_projects_trusted",
}
#: Steps the full apply runs only when the config declares a machine registry.
REGISTRY_GATED = frozenset({"host-identity", "billing-mode", "machine-settings"})


def policy(name: str) -> StepPolicy:
    """The entry for ``name``; a name with no entry is a programming error."""
    for entry in SEQUENCE:
        if entry.name == name:
            return entry
    raise KeyError(name)


def with_private_mode_env(
    entry: StepPolicy, pairs: tuple[tuple[str, str], ...]
) -> StepPolicy:
    """``entry`` with the mode variables the private table declares for it appended."""
    return replace(entry, mode_env=entry.mode_env + pairs) if pairs else entry


def full_apply_kind(entry: StepPolicy) -> str:
    """What a failure of ``entry`` does in the full apply: install.sh's kind, or a carried stop."""
    return STOP if entry.name in FULL_APPLY_STOPS else entry.kind


def settings_lock_span() -> frozenset[str]:
    """The full apply's steps from SETTINGS_LOCK_FROM through SETTINGS_LOCK_THROUGH."""
    names = [entry.name for entry in full_apply_order()]
    first = names.index(SETTINGS_LOCK_FROM)
    return frozenset(names[first : names.index(SETTINGS_LOCK_THROUGH) + 1])


def full_apply_order() -> tuple[StepPolicy, ...]:
    """SEQUENCE with each AHEAD_OF_POSITION step moved to right after the step it follows."""
    order = [entry for entry in SEQUENCE if entry.name not in AHEAD_OF_POSITION]
    for name, after in AHEAD_OF_POSITION.items():
        index = next(i for i, entry in enumerate(order) if entry.name == after)
        order.insert(index + 1, policy(name))
    return tuple(order)
