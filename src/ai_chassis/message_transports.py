"""Message transport ladder for cross-session communication (ISSUE-REF Phase 1).

Design: docs/designs/issue-ref-buzz-primary-cross-machine-messaging.md

This module implements a configurable ladder of message transports with availability probes.
Each rung is tested in order until an available one is found. Probes are pure functions of
injected inputs, so tests need no live sockets or network.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_chassis.session_address import WILDCARD, AddressError, board_name, parse_address

# Default configuration matching the design spec
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "ladders": {
        "cross-machine": ["native", "agent-mail", "buzz", "git-inbox", "fm-send"],
        "same-machine": ["native", "inbox-socket", "marker-hook"],
    },
    "rungs": {
        "native": {
            "probe": {
                "same-machine": "native_socket_env",
                "cross-machine": "native_cross_machine_reach",
            }
        },
        "inbox-socket": {"probe": "peer_socket_and_live"},
        "buzz": {"probe": "buzz_relay_configured", "heartbeat": True},
        "agent-mail": {"probe": "agent_mail_status_fresh", "heartbeat": False},
        "git-inbox": {"probe": "git_remote_reachable", "heartbeat": True},
        "fm-send": {"probe": "firstmate_present"},
        "marker-hook": {"probe": "always"},
    },
    "liveness": {"stale_after_s": 180},
}

# Slack allowed for ordinary clock jitter between hosts before a future-dated record is
# treated as clock skew rather than a fresh one.
CLOCK_SKEW_TOLERANCE_S = 5

# Pair transports (design D-9, T-2.7). A pair is the unit of relay + transport: its relay
# host reaches the relay on loopback, every other member over the pair's declared kind.
# `loopback` is accepted as a declared kind for a pair whose members all sit on the relay
# host's loopback (a test topology); the supervisor implements all three.
PAIR_TRANSPORT_KINDS: frozenset[str] = frozenset(
    {"loopback", "ssm-port-forward", "tailscale"}
)
# The transport kind no example-client-host-profile machine may be placed on (the maintainer, 2026-10-08: example-client-host
# machines never join a tailnet).
TAILNET_TRANSPORT = "tailscale"
# Where the pair-transport supervisor publishes its state file, under the harness's
# unprivileged state root (`$XDG_STATE_HOME`, else `~/.local/state`).
PAIR_TRANSPORT_STATE_RELATIVE = ("ai-harness", "pair-transport", "state.json")
# The `state` the supervisor publishes when it REFUSES to start rather than when a forward
# went down -- a missing overlay key, an unparseable config. Owned here because the probe
# reads it and `scripts/pair_transport.py` writes it.
PAIR_TRANSPORT_REFUSED = "refused"
# A refusal reason is a sentence naming the missing keys, not a token, so it is truncated for
# the resolver's one-line `evaluated` row. The full text is in the state file and the agent's
# stderr log; this is a display bound, never the record.
REFUSAL_REASON_MAX_CHARS = 200
# One TCP connect to the local relay port, on the resolve path. Cheap and local, so it is
# a probe this module may run itself when the caller did not inject the fact.
LOCAL_PORT_PROBE_TIMEOUT_S = 0.5

# This repo's root, from this file's own location: src/ai_chassis/<this file>.
REPO_ROOT = Path(__file__).resolve().parents[2]
# The tracked ladder and its untracked local overlay, relative to a repo root.
TRACKED_CONFIG_RELATIVE = Path("config") / "message-transports.json"
LOCAL_OVERLAY_RELATIVE = Path("config") / "message-transports.local.json"


class TransportConfigError(Exception):
    """Configuration is invalid or a rung referenced in a ladder is undefined."""


class TransportUsageError(Exception):
    """CLI argument is malformed (exit 2)."""


@dataclass(frozen=True)
class ProbeResult:
    """Result of an availability probe."""

    available: bool
    reason: str = ""


# ----------------------------------------------------------------------------
# Configuration loading
# ----------------------------------------------------------------------------


def load_config(  # noqa: C901, PLR0912
    config_path: Path | None = None,
    local_overlay: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load and validate the transport ladder configuration with optional local overlay.

    Precedence, highest first: explicit ``config_path`` (the CLI flag layer), the
    ``AIH_MESSAGE_TRANSPORTS`` path from ``env``, the built-in default. A present
    ``local_overlay`` then merges its ``ladders``, ``disabled`` and ``pairs`` keys on top
    (``pairs`` per pair, one level deep -- see the merge below; the docstring previously named
    only the first two, which understated what an overlay can carry).

    Args:
        config_path: Path to the tracked message-transports.json file. If None, falls back
                    to the env layer and then to DEFAULT_CONFIG.
        local_overlay: Path to message-transports.local.json. If present, its ladders,
                      disabled and pairs keys override the tracked config.
        env: Environment mapping used for the AIH_MESSAGE_TRANSPORTS layer. Defaults to
            os.environ.

    Returns:
        Validated config dict with version/ladders/rungs/liveness.

    Raises:
        TransportConfigError: If a config or overlay file exists but does not parse, if
            version != 1, or if a ladder references an undefined rung.
    """
    if env is None:
        env = dict(os.environ)

    # CLI-flag layer > AIH_MESSAGE_TRANSPORTS path layer
    if config_path is None:
        env_path = env.get("AIH_MESSAGE_TRANSPORTS", "").strip()
        if env_path:
            config_path = Path(env_path)

    # Load tracked config or fall back to default. A file that is absent falls back; a
    # file that exists but cannot be read or parsed fails closed (the operator edited it).
    if config_path is None or not config_path.exists():
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["_config_source"] = "default"
    else:
        try:
            tracked = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportConfigError(
                f"config file {config_path} could not be read: {exc}"
            ) from exc
        config = copy.deepcopy(tracked)
        config["_config_source"] = str(config_path)

    # Apply local overlay if present
    if local_overlay is not None and local_overlay.exists():
        try:
            overlay = json.loads(local_overlay.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportConfigError(
                f"overlay file {local_overlay} could not be read: {exc}"
            ) from exc
        if "ladders" in overlay and isinstance(overlay["ladders"], dict):
            if "ladders" not in config:
                config["ladders"] = {}
            config["ladders"].update(copy.deepcopy(overlay["ladders"]))
        if "disabled" in overlay and isinstance(overlay["disabled"], list):
            config["disabled"] = list(overlay["disabled"])
        # `pairs` merges PER PAIR, one level deep: the overlay is where a machine adds
        # the runtime facts the tracked file must never carry (the SSM target and the
        # AWS profile of the example-client-host pair, which name an account), so it extends a tracked
        # pair rather than replacing it.
        #
        # AN OVERLAY CANNOT INTRODUCE A PAIR THE TRACKED CONFIG DOES NOT DECLARE, and that
        # is a correction (ISSUE-REF). `setdefault` used to INVENT one from the runtime
        # facts alone, producing a pair with an `ssm` block and no `members`, `relay_host`
        # or `transport` -- which then failed validation with "'members' must list at least
        # two distinct machine names", a message about the TRACKED file for a defect that
        # came entirely from the overlay.
        #
        # This was latent until provisioning started writing the overlay: before that no
        # machine had one, so the path was unreachable. Measured 2026-10-08, with the
        # overlay present: four `test_message_transports.py::TestCli` cases went red,
        # because each writes a small tracked config with NO `pairs` at all and the
        # auto-detected machine overlay then grafted a memberless `example-client-host` onto it. The
        # tracked config is the authority on which pairs EXIST; a machine-local overlay
        # naming one it does not define is describing a different topology, so it is not
        # applicable here and is skipped rather than merged or raised over.
        if "pairs" in overlay and isinstance(overlay["pairs"], dict):
            merged_pairs = copy.deepcopy(config.get("pairs", {}))
            for pair_name, overlay_pair in overlay["pairs"].items():
                if pair_name not in merged_pairs:
                    continue
                if isinstance(overlay_pair, dict):
                    merged_pairs[pair_name].update(copy.deepcopy(overlay_pair))
                else:
                    merged_pairs[pair_name] = copy.deepcopy(overlay_pair)
            config["pairs"] = merged_pairs
        config["_overlay_path"] = str(local_overlay)
        # `source` is the field an operator reads to answer "which file produced this
        # order?", so it must name both layers when an overlay is in force.
        config["_config_source"] = (
            f"{config['_config_source']} + overlay:{local_overlay}"
        )

    # Validate version
    if config.get("version") != 1:
        raise TransportConfigError(
            f"unsupported version: {config.get('version')} (expected 1)"
        )

    # Validate that all rungs in ladders are defined
    rungs = config.get("rungs", {})
    for ladder_name, rung_list in config.get("ladders", {}).items():
        if not isinstance(rung_list, list):
            continue
        for rung in rung_list:
            if rung not in rungs:
                raise TransportConfigError(
                    f"rung '{rung}' in ladder '{ladder_name}' is not defined in rungs"
                )

    _validate_pairs(config)

    return config


def load_tracked_config(
    *,
    repo_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The TRACKED ladder with the local overlay merged -- the one resolution every entry
    point shares (audit F-25).

    `load_config()` with no arguments falls back to `DEFAULT_CONFIG`, which carries no
    `pairs` block at all: a caller that used it got `select_pair() -> None` on every
    machine, so the `buzz` rung was dead and its per-pair enable was read against an empty
    pair NAME. Every caller that means "this repo's configured ladder" must therefore come
    through here rather than call `load_config()` bare.

    `AIH_MESSAGE_TRANSPORTS` still wins, and is left to `load_config`'s own env layer so
    the precedence chain has one implementation.

    Args:
        repo_root: Repository root to resolve the tracked file and overlay against.
            Defaults to this module's own repo.
        env: Environment mapping for the `AIH_MESSAGE_TRANSPORTS` layer.

    Returns:
        The validated config, with `_config_source` naming the layers in force.

    Raises:
        TransportConfigError: As :func:`load_config`, for a file that exists but is bad.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    environment = dict(os.environ) if env is None else dict(env)
    config_path: Path | None
    if environment.get("AIH_MESSAGE_TRANSPORTS", "").strip():
        config_path = None  # the loader resolves the env layer itself
    else:
        tracked = root / TRACKED_CONFIG_RELATIVE
        config_path = tracked if tracked.exists() else None
    overlay = root / LOCAL_OVERLAY_RELATIVE
    return load_config(
        config_path, overlay if overlay.exists() else None, env=environment
    )


# ----------------------------------------------------------------------------
# Pairs (D-9, T-2.7): the unit of relay + transport
# ----------------------------------------------------------------------------


def _validate_pairs(config: dict[str, Any]) -> None:  # noqa: C901, PLR0912
    """Refuse a `pairs` block the supervisor could not run or policy forbids.

    Every refusal names the pair (and the member, where a member is the problem), so
    the operator can fix the row rather than hunt for it. A config with no `pairs` key
    is valid: the `buzz` rung is then skipped with `no-pair-for-machine` everywhere.
    """
    pairs = config.get("pairs")
    if pairs is None:
        return
    if not isinstance(pairs, dict):
        raise TransportConfigError("'pairs' must be an object keyed by pair name")
    bms_machines = config.get("bms_machines", [])
    if not isinstance(bms_machines, list) or not all(
        isinstance(name, str) for name in bms_machines
    ):
        raise TransportConfigError("'bms_machines' must be a list of machine names")
    seen: dict[str, str] = {}
    for pair_name, pair in pairs.items():
        if not isinstance(pair, dict):
            raise TransportConfigError(f"pair '{pair_name}' must be an object")
        members = pair.get("members")
        if (
            not isinstance(members, list)
            or len(members) < 2  # noqa: PLR2004  # a pair is two or more machines
            or not all(isinstance(name, str) and name for name in members)
            or len(set(members)) != len(members)
        ):
            raise TransportConfigError(
                f"pair '{pair_name}': 'members' must list at least two distinct "
                "machine names"
            )
        relay_host = pair.get("relay_host")
        if relay_host not in members:
            raise TransportConfigError(
                f"pair '{pair_name}': 'relay_host' {relay_host!r} is not one of its "
                f"members {members}"
            )
        kind = pair.get("transport")
        if kind not in PAIR_TRANSPORT_KINDS:
            raise TransportConfigError(
                f"pair '{pair_name}': transport kind {kind!r} is not implemented by "
                f"the pair-transport supervisor (known: {sorted(PAIR_TRANSPORT_KINDS)})"
            )
        if kind == TAILNET_TRANSPORT:
            for member in members:
                if member in bms_machines:
                    raise TransportConfigError(
                        f"pair '{pair_name}': machine '{member}' is a example-client-host-profile "
                        f"machine and may not be placed on a '{TAILNET_TRANSPORT}' "
                        "transport (example-client-host machines never join a tailnet)"
                    )
        relay_port = pair.get("relay_port")
        if not _is_port(relay_port):
            raise TransportConfigError(
                f"pair '{pair_name}': 'relay_port' must be an integer in 1..65535"
            )
        local_port = pair.get("local_port", relay_port)
        if not _is_port(local_port):
            raise TransportConfigError(
                f"pair '{pair_name}': 'local_port' must be an integer in 1..65535"
            )
        _validate_pair_forwards(pair_name, pair.get("forwards", {}), local_port)
        grace = pair.get("startup_grace_s")
        if grace is not None and (
            isinstance(grace, bool) or not isinstance(grace, int | float) or grace <= 0
        ):
            raise TransportConfigError(
                f"pair '{pair_name}': 'startup_grace_s' must be a positive number of "
                f"seconds, not {grace!r}"
            )
        for member in members:
            if member in seen:
                raise TransportConfigError(
                    f"machine '{member}' appears in both pair '{seen[member]}' and "
                    f"pair '{pair_name}'; a machine belongs to at most one pair"
                )
            seen[member] = pair_name


#: The forward name the supervisor gives the pair's relay itself; a declared extra forward may not
#: take it, or two children would publish under one key in the state file the `buzz` probe reads.
RELAY_FORWARD_NAME = "relay"


def _validate_pair_forwards(
    pair_name: str, forwards: object, relay_local_port: int
) -> None:
    """Refuse a pair's extra forwards (ISSUE-REF: the Agent Mail board) the supervisor could not run.

    Each is ``{"remote_port", "local_port"}``, forwarded only by an ``ssm-port-forward`` member
    beside the relay forward, so every local port must be distinct: two children binding one port
    is the race ``wq_tunnel``'s reap-before-replace rule exists to prevent.
    """
    if not isinstance(forwards, dict):
        raise TransportConfigError(
            f"pair '{pair_name}': 'forwards' must be an object keyed by forward name"
        )
    taken = {relay_local_port: RELAY_FORWARD_NAME}
    for name, spec in forwards.items():
        if not isinstance(name, str) or not name or name == RELAY_FORWARD_NAME:
            raise TransportConfigError(
                f"pair '{pair_name}': forward name {name!r} is empty or reserved for "
                f"the '{RELAY_FORWARD_NAME}' forward"
            )
        if not isinstance(spec, dict):
            raise TransportConfigError(
                f"pair '{pair_name}': forward '{name}' must be an object"
            )
        for key in ("remote_port", "local_port"):
            if not _is_port(spec.get(key)):
                raise TransportConfigError(
                    f"pair '{pair_name}': forward '{name}' needs '{key}' as an integer "
                    "in 1..65535"
                )
        port = spec["local_port"]
        if port in taken:
            raise TransportConfigError(
                f"pair '{pair_name}': forward '{name}' local_port {port} is already "
                f"used by forward '{taken[port]}'"
            )
        taken[port] = name


def _is_port(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535  # noqa: PLR2004  # the TCP port range
    )


def select_pair(config: dict[str, Any], machine: str) -> dict[str, Any] | None:
    """The pair containing ``machine``, resolved for that machine, or None.

    The returned view carries what the `buzz` probe and the supervisor need: the pair's
    name and members, the relay host, the EFFECTIVE transport kind for this machine
    (`loopback` on the relay host, the pair's declared kind elsewhere), the relay port,
    the local port the relay is reached on, this machine's role, the pair's runtime
    facts (`ssm`) when the local overlay supplied them, and the extra `forwards` this
    machine must keep beside the relay's (empty unless it is a port-forward member).
    """
    pairs = config.get("pairs") or {}
    for pair_name, pair in pairs.items():
        members = pair.get("members", [])
        if machine not in members:
            continue
        relay_host = pair["relay_host"]
        is_relay_host = machine == relay_host
        kind = "loopback" if is_relay_host else pair["transport"]
        relay_port = pair["relay_port"]
        return {
            "name": pair_name,
            "members": list(members),
            "relay_host": relay_host,
            "transport": kind,
            "declared_transport": pair["transport"],
            "relay_port": relay_port,
            # On the relay host and over a tailnet the relay is dialled directly on
            # its own port; a port-forward lands it on the local port instead.
            "local_port": (
                pair.get("local_port", relay_port)
                if kind == "ssm-port-forward"
                else relay_port
            ),
            "role": "relay-host" if is_relay_host else "member",
            "ssm": pair.get("ssm"),
            # A tailnet relay binds the relay host's tailnet IPv4, which only the local
            # overlay knows; every member, the relay host included, dials it (ISSUE-REF).
            "relay_address": pair.get("relay_address"),
            # Only a port-forward member needs them: on the relay host each service is on its
            # own loopback, and over a tailnet it is dialled directly.
            "forwards": (
                dict(pair.get("forwards") or {}) if kind == "ssm-port-forward" else {}
            ),
        }
    return None


def relay_probe_host(pair: dict[str, Any]) -> str:
    """The host a TCP probe of the pair's relay connects to from this machine (ISSUE-REF).

    The same place `buzz_channel.derive_relay_endpoint` dials: a `tailscale` pair's relay
    binds only the relay host's tailnet IPv4, so every member, the relay host included,
    reaches it at `relay_address`; a loopback or port-forward pair's relay is on
    127.0.0.1. Probing loopback on a tailnet pair, or the pair's `relay_host` machine
    NAME (a profile name, not a tailnet DNS name), measured a port nothing listens on.
    "" when a tailnet pair's local overlay carries no `relay_address`.
    """
    if (pair.get("declared_transport") or pair.get("transport")) == TAILNET_TRANSPORT:
        return str(pair.get("relay_address") or "").strip()
    return "127.0.0.1"


def pair_transport_state_path(env: dict[str, str] | None = None) -> Path:
    """Where the pair-transport supervisor publishes, and where the probe reads."""
    environment = os.environ if env is None else env
    root = environment.get("XDG_STATE_HOME", "").strip()
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base.joinpath(*PAIR_TRANSPORT_STATE_RELATIVE)


def read_pair_transport_state(path: Path) -> dict[str, Any] | None:
    """The supervisor's state document, or None when there is none to read.

    An unreadable or unparsable file is also None: the probe then reports the state
    missing, which is the fail-closed answer for a rung that must not be chosen on a
    guess.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def local_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something accepts a TCP connection on ``host:port`` right now."""
    try:
        with socket.create_connection((host, port), timeout=LOCAL_PORT_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------------
# Probes: pure functions of injected inputs
# ----------------------------------------------------------------------------


def native_socket_env(
    env: dict[str, str],
    slug: str = "",
    registered_names: set[str] | frozenset[str] | None = None,
) -> ProbeResult:
    """Check that this session can send natively and that the target can be found by name.

    `SendMessage` reaches a same-machine peer by name only through that peer's live
    `~/.claude/sessions/<pid>.json` record. A live peer whose record is gone keeps its socket
    but is unreachable by name, and Claude Code never writes the record back (ISSUE-REF), so
    the caller's own socket alone is not evidence the target can be reached.

    Args:
        env: Environment mapping (usually os.environ)
        slug: The target session's name.
        registered_names: Names carried by registry records that provably describe a live
            process. None means the fact was not gathered, and only the caller's socket is
            judged.

    Returns:
        Available when the caller has a messaging socket and, where the registry was read,
        a live record names the target.
    """
    socket_path = env.get("CLAUDE_CODE_MESSAGING_SOCKET", "").strip()
    if not socket_path:
        return ProbeResult(available=False, reason="no-messaging-socket")
    if registered_names is not None and slug not in registered_names:
        return ProbeResult(available=False, reason="native-target-unregistered")
    return ProbeResult(available=True)


def native_cross_machine_reach(
    env: dict[str, str],
    remote_control_connected: bool | None = None,
) -> ProbeResult:
    """Check whether native messaging can reach a session on another machine.

    Native cross-machine messaging needs claude.ai plus a connected Remote Control session;
    it is unavailable on a third-party model provider. A local messaging socket says nothing
    about cross-machine reach, so it is deliberately not consulted here.

    Args:
        env: Environment mapping (usually os.environ).
        remote_control_connected: Injected fact — True only when a Remote Control
            connection to the target machine is known to be up.

    Returns:
        Unavailable naming the provider when a third-party-provider marker is set;
        available when no marker is set and remote control is known connected; otherwise
        unavailable with `native-cross-machine-unknown` (fail closed).
    """
    for var, provider in (
        ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
        ("CLAUDE_CODE_USE_VERTEX", "vertex"),
        ("CLAUDE_CODE_USE_FOUNDRY", "foundry"),
    ):
        if env.get(var, "").strip():
            return ProbeResult(
                available=False,
                reason=f"native-cross-machine-unavailable-on-{provider}",
            )

    if remote_control_connected is True:
        return ProbeResult(available=True)

    return ProbeResult(available=False, reason="native-cross-machine-unknown")


def peer_socket_and_live(
    slug: str,
    socket_dir: Path,
    liveness_map: dict[str, str],
    registered_slugs: set[str] | list[str] | None = None,
) -> ProbeResult:
    """Check if a peer session's socket exists, the session is live, and hooks are installed.

    Delivery on this rung is hook-mediated (design D-6), so a live peer with no registered
    hooks marker cannot receive the post. Reporting `no-hooks` is what lets the resolver
    fall through to marker-hook delivery instead of silently losing the message.

    Args:
        slug: Session slug
        socket_dir: Directory containing session sockets (e.g., /tmp/cc-socks-1000)
        liveness_map: Map of slug -> liveness state ("live", "idle", etc.)
        registered_slugs: Slugs whose hook markers are registered. None means the fact was
            not injected, which fails closed as `no-hooks`.

    Returns:
        Available only if the socket exists, liveness reports "live", and the slug is
        registered; otherwise unavailable naming the condition that failed.
    """
    socket_path = socket_dir / f"{slug}.sock"
    if not socket_path.exists():
        return ProbeResult(available=False, reason="socket-not-found")

    liveness = liveness_map.get(slug, "unknown")
    if liveness != "live":
        return ProbeResult(available=False, reason=f"liveness-not-live ({liveness})")

    if registered_slugs is None or slug not in set(registered_slugs):
        return ProbeResult(available=False, reason="no-hooks")

    return ProbeResult(available=True)


def buzz_relay_configured(  # noqa: C901, PLR0911, PLR0913  # one return per named refusal; every input injected
    buzz_config: dict[str, Any],
    *,
    pair: dict[str, Any] | None = None,
    transport_state: dict[str, Any] | None = None,
    port_open: bool | None = None,
    now: str | None = None,
    stale_after_s: int = 180,
    channel_state: dict[str, Any] | None = None,
) -> ProbeResult:
    """Whether the `buzz` rung can carry a message from this machine right now (T-2.8).

    Available only when ALL of these hold: `[buzz_notify]` is configured and enabled,
    this machine is in a pair (T-2.7), the pair-transport supervisor's state file reads
    `up` for the relay forward and is fresher than ``stale_after_s``, the local relay
    port accepts a connection, and -- when a ``channel_state`` is supplied -- the daemon's
    own record shows a live WSS + AUTH + subscription no older than ``stale_after_s``.
    Each refusal names the condition that failed, so the resolver's `evaluated` row says
    why the ladder fell through.

    ``channel_state`` discriminates THREE cases, and callers must keep them apart
    (audit F-28):

    * ``None`` -- the fact was not injected. The protocol checks are skipped entirely,
      which is what preserves the behaviour of every caller that predates them.
    * ``{}`` -- there IS no record. `buzz-state-missing`: the daemon has never written
      one, so nothing proves the relay is more than a TCP listener.
    * populated -- its fields decide.

    Args:
        buzz_config: The [buzz_notify] table with `relay_url` and `enabled`.
        pair: :func:`select_pair`'s view for this machine, or None when it is in none.
        transport_state: The supervisor's state document, or None when there is none.
        port_open: Whether the relay's local port accepted a TCP connection.
        now: Current time as ISO 8601; used to age ``transport_state['updated_at']``.
        stale_after_s: Staleness bound for the state file, from `liveness`.
        channel_state: The Buzz daemon's protocol-health record; see above.
    """
    relay_url = buzz_config.get("relay_url", "").strip()
    enabled = buzz_config.get("enabled", False)
    if not relay_url or enabled is not True:
        return ProbeResult(available=False, reason="buzz-not-configured")

    if pair is None:
        return ProbeResult(available=False, reason="no-pair-for-machine")

    # THE RELAY HOST HAS NO TRANSPORT TO SUPERVISE (ISSUE-REF). install.sh installs the
    # pair-transport supervisor on members only (T-4.2), so on a relay host no state file is
    # ever written, and requiring one read `transport-state-missing` there forever. Its relay
    # is on this machine: the port check below and the daemon's protocol record decide.
    if pair.get("role") != "relay-host":
        transport = _buzz_transport_health(
            transport_state, now=now, stale_after_s=stale_after_s
        )
        if not transport.available:
            return transport

    if port_open is not True:
        return ProbeResult(
            available=False,
            reason=f"local-port-refused ({pair.get('local_port')})",
        )

    if channel_state is None:
        return ProbeResult(available=True)
    return _buzz_protocol_health(channel_state, now=now, stale_after_s=stale_after_s)


def _buzz_transport_health(  # noqa: PLR0911  # one return per named transport refusal
    transport_state: dict[str, Any] | None,
    *,
    now: str | None,
    stale_after_s: int,
) -> ProbeResult:
    """The SUPERVISOR half of the `buzz` probe: is the port forward up and fresh?

    Extracted when F-28 added the protocol half, so each half is one readable sequence of
    named refusals rather than one function with two unrelated jobs. The refusal words are
    byte-unchanged; `tests/test_message_transports.py::TestBuzzProbeRequiresTransport`
    pins them.
    """
    if transport_state is None:
        return ProbeResult(available=False, reason="transport-state-missing")

    if transport_state.get("event_driven") is True:
        from ai_chassis.wake.tunnel import owner_alive

        # This only permits the actual first-use port check below. A live
        # supervisor is never itself proof of a working forward.
        return ProbeResult(
            available=owner_alive(transport_state), reason="event-supervisor-observed"
        )

    updated_at = transport_state.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return ProbeResult(available=False, reason="transport-state-missing")
    try:
        updated_dt = dt.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        now_dt = dt.datetime.fromisoformat(
            (now or dt.datetime.now(dt.timezone.utc).isoformat()).replace("Z", "+00:00")
        )
    except ValueError:
        return ProbeResult(available=False, reason="transport-state-invalid-timestamp")
    age_s = (now_dt - updated_dt).total_seconds()
    if age_s > stale_after_s:
        return ProbeResult(
            available=False, reason=f"transport-state-stale (age={int(age_s)}s)"
        )

    # A CONFIGURATION REFUSAL IS NOT A FORWARD THAT WENT DOWN (ISSUE-REF AC-2). The supervisor
    # publishes `state: refused` with the reason when it will not start at all -- a missing
    # `ssm.target`/`ssm.profile` in the local overlay, say. Without this branch such a document
    # has no `forwards.relay` and so read as a bare `transport-down`, pointing a reader at the
    # tunnel, the credential or the network when the actual answer is a missing config key and
    # the reason was sitting in the file all along.
    if transport_state.get("state") == PAIR_TRANSPORT_REFUSED:
        detail = str(transport_state.get("reason") or "").strip()
        if len(detail) > REFUSAL_REASON_MAX_CHARS:
            detail = f"{detail[:REFUSAL_REASON_MAX_CHARS].rstrip()}..."
        return ProbeResult(
            available=False,
            reason=f"transport-refused ({detail})" if detail else "transport-refused",
        )

    forwards = transport_state.get("forwards")
    relay = forwards.get("relay") if isinstance(forwards, dict) else None
    if not isinstance(relay, dict) or relay.get("state") != "up":
        detail = (relay or {}).get("last_error") if isinstance(relay, dict) else None
        reason = "transport-down"
        if detail:
            reason = f"transport-down ({detail})"
        return ProbeResult(available=False, reason=reason)

    return ProbeResult(available=True)


def _buzz_protocol_health(  # noqa: PLR0911  # one return per named protocol refusal
    channel_state: dict[str, Any],
    *,
    now: str | None,
    stale_after_s: int,
) -> ProbeResult:
    """The WSS / AUTH / subscription half of the `buzz` probe. T-2.8, audit F-28.

    A TCP listener returning an HTTP error, rejecting AUTH, or closing the REQ still made
    `resolve()` choose Buzz, so callers stopped above agent mail for a rung that could not
    deliver. Ordered most-fundamental first, so the reason names the LOWEST thing that is
    broken rather than a symptom of it.
    """
    if not channel_state:
        return ProbeResult(available=False, reason="buzz-state-missing")

    detail = str(channel_state.get("last_error") or "unknown")
    if channel_state.get("reachable") is not True:
        return ProbeResult(available=False, reason=f"buzz-wss-failed ({detail})")
    if channel_state.get("auth_ok") is not True:
        return ProbeResult(available=False, reason=f"buzz-auth-refused ({detail})")
    if channel_state.get("subscribed") is not True:
        return ProbeResult(
            available=False, reason="buzz-subscription-stale (not-subscribed)"
        )

    last_ok_at = channel_state.get("last_ok_at")
    if not isinstance(last_ok_at, str) or not last_ok_at:
        return ProbeResult(
            available=False, reason="buzz-subscription-stale (no-last-ok-at)"
        )
    try:
        last_ok_dt = dt.datetime.fromisoformat(last_ok_at.replace("Z", "+00:00"))
        now_dt = dt.datetime.fromisoformat(
            (now or dt.datetime.now(dt.timezone.utc).isoformat()).replace("Z", "+00:00")
        )
    except ValueError:
        return ProbeResult(
            available=False, reason="buzz-subscription-stale (invalid-last-ok-at)"
        )
    age_s = (now_dt - last_ok_dt).total_seconds()
    if age_s > stale_after_s:
        return ProbeResult(
            available=False, reason=f"buzz-subscription-stale (age={int(age_s)}s)"
        )

    return ProbeResult(available=True)


def agent_mail_status_fresh(
    status: dict[str, Any],
    now: str,
    stale_after_s: int,
) -> ProbeResult:
    """Check that the notifier daemon's last poll is fresh and reached the board.

    Args:
        status: The daemon's `inbox.json` record (`generated_at`, `board.reachable`,
            `board.last_error`). It never carried the `registered` key this probe once read,
            so a real record could not pass (ISSUE-REF).
        now: Current time as ISO 8601 string.
        stale_after_s: Staleness threshold in seconds.

    Returns:
        Available if generated_at is within stale_after_s and the board was reachable.
    """
    generated_at = status.get("generated_at")
    board = status.get("board") if isinstance(status.get("board"), dict) else {}

    if not generated_at:
        return ProbeResult(available=False, reason="status-missing")

    if board.get("reachable") is not True:
        detail = str(board.get("last_error") or "unknown")
        # ISSUE-REF's `configured: false` is a machine with no board config or the rung switched
        # off, not an outage, and its `last_error` already names which.
        reason = (
            detail
            if board.get("configured") is False
            else f"board-unreachable ({detail})"
        )
        return ProbeResult(available=False, reason=reason)

    try:
        generated_dt = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        now_dt = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
        age_s = (now_dt - generated_dt).total_seconds()

        # A future-dated record is clock skew, not freshness: treating it as fresh stops
        # the ladder here and never reaches the zero-dependency git-inbox floor below.
        if age_s < -CLOCK_SKEW_TOLERANCE_S:
            return ProbeResult(
                available=False, reason=f"clock-skew (age={int(age_s)}s)"
            )

        if age_s > stale_after_s:
            return ProbeResult(available=False, reason=f"stale (age={int(age_s)}s)")

        return ProbeResult(available=True)
    except (ValueError, AttributeError):
        return ProbeResult(available=False, reason="invalid-timestamp")


def git_remote_reachable(git_result: dict[str, Any]) -> ProbeResult:
    """Check if git ls-remote succeeded.

    Args:
        git_result: Dict with exit_status and stdout keys.

    Returns:
        Available if exit_status is 0.
    """
    exit_status = git_result.get("exit_status")
    if exit_status == 0:
        return ProbeResult(available=True)

    return ProbeResult(available=False, reason=f"git-failed (exit {exit_status})")


def always() -> ProbeResult:
    """Always available (marker-hook rung)."""
    return ProbeResult(available=True)


# The maintained Firstmate fork is checked out by the installer (the private firstmate-fork
# step, formerly install.sh Step 14b) under the projects root
# at the commit named in projects-wide-session-config/firstmate-fork.pin; its send entry point
# is bin/fm-send.sh (design: issue-ref § Phase 4, issue-ref § Transport-config detail).
FIRSTMATE_SEND_RELATIVE = Path("firstmate") / "bin" / "fm-send.sh"


def discover_firstmate_binary(env: dict[str, str] | None = None) -> str | None:
    """Locate the Firstmate send entry point, or None when the fork is not installed.

    Looks first at the fork checkout install.sh maintains under the projects root
    (``$PROJECTS_DIR``, else ``~/projects``), then for an ``fm-send`` on ``PATH``. Only an
    executable file counts: a checkout whose script is missing or not executable is
    "not installed", so the rung is skipped rather than chosen and then failing to send.

    Args:
        env: Environment to read ``PROJECTS_DIR`` and ``PATH`` from; the process
            environment when omitted. Tests inject a dict so discovery never reaches the
            host's real checkout.

    Returns:
        Absolute path of the executable, or None.
    """
    environment = os.environ if env is None else env
    projects_dir = environment.get("PROJECTS_DIR", "").strip() or str(
        Path.home() / "projects"
    )
    candidate = Path(projects_dir).expanduser() / FIRSTMATE_SEND_RELATIVE
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    # An explicitly empty PATH means "look nowhere", never "fall back to the real PATH".
    return shutil.which("fm-send", path=environment.get("PATH", ""))


def firstmate_present(firstmate_binary: str | None = None) -> ProbeResult:
    """Check if the Firstmate send entry point is present.

    Args:
        firstmate_binary: Path to the ``fm-send`` entry point (see
            :func:`discover_firstmate_binary`), or None if not found.

    Returns:
        Available if the binary exists; otherwise unavailable with reason
        ``firstmate-not-installed``.
    """
    if firstmate_binary:
        return ProbeResult(available=True)

    return ProbeResult(available=False, reason="firstmate-not-installed")


# ----------------------------------------------------------------------------
# Resolver
# ----------------------------------------------------------------------------


def resolve(
    target: str,
    scope: str | None = None,
    config: dict[str, Any] | None = None,
    probe_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the first available transport rung for a target.

    Args:
        target: Target identity in <session>@<host> format.
        scope: "same-machine" or "cross-machine". If None, inferred from target.
        config: Loaded config dict. If None, loads from default location.
        probe_context: Injected context for probes (env, buzz_config, etc.).

    Returns:
        Dict with keys:
        - target: The target identity
        - scope: Resolved scope
        - rung: First available rung name, or None if none available
        - evaluated: List of {rung, available, reason} for each rung checked
        - source: Config source description

    Raises:
        TransportUsageError: If target format is invalid.
    """
    target_machine, target_slug = _target_parts(target)

    if config is None:
        config = load_config()

    if probe_context is None:
        probe_context = {}

    # Infer scope if not provided
    machine_label = _machine_label(probe_context)
    if scope is None:
        scope = "same-machine" if target_machine == machine_label else "cross-machine"

    # Get ladder for scope
    ladders = config.get("ladders", {})
    ladder = ladders.get(scope, [])

    # Probes are pure functions of injected inputs, so the resolver supplies the facts it
    # owns — the target and its slug. An explicit caller value wins.
    probe_context = dict(probe_context)
    probe_context.setdefault("target", target)
    probe_context.setdefault("slug", target_slug)

    # Check each rung in order
    evaluated: list[dict[str, Any]] = []
    chosen_rung: str | None = None
    disabled = set(config.get("disabled", []))

    for rung in ladder:
        # Skip disabled rungs without running probe
        if rung in disabled:
            evaluated.append(
                {
                    "rung": rung,
                    "available": False,
                    "reason": "disabled-by-overlay",
                }
            )
            continue

        # Run probe
        probe_result = _run_probe(rung, config, probe_context, scope)
        evaluated.append(
            {
                "rung": rung,
                "available": probe_result.available,
                "reason": probe_result.reason,
            }
        )

        # First available rung wins: stop so no rung below the winner is probed. Phase 2
        # wires real I/O behind these probes, where probing the rest is the cost this
        # ladder exists to avoid.
        if probe_result.available:
            chosen_rung = rung
            break

    # The pair this machine belongs to (T-2.7), exposed so a caller sees which relay
    # endpoint and transport kind the `buzz` probe was judged against.
    pair = select_pair(config, machine_label)
    return {
        "target": target,
        "scope": scope,
        "rung": chosen_rung,
        "evaluated": evaluated,
        "source": config.get("_config_source", "unknown"),
        "pair": None
        if pair is None
        else {
            "name": pair["name"],
            "relay_host": pair["relay_host"],
            "transport": pair["transport"],
            "relay_port": pair["relay_port"],
            "local_port": pair["local_port"],
            "role": pair["role"],
        },
    }


def _probe_native_socket_env(
    _config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    return native_socket_env(
        context.get("env", os.environ),
        context.get("slug", ""),
        context.get("registered_session_names"),
    )


def _probe_native_cross_machine_reach(
    _config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    return native_cross_machine_reach(
        context.get("env", os.environ),
        context.get("remote_control_connected"),
    )


def _probe_peer_socket_and_live(
    _config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    slug = context.get("slug", "")
    if not slug:
        target = context.get("target", "")
        slug = _target_parts_or_empty(target)[1]
    return peer_socket_and_live(
        slug,
        context.get("socket_dir", Path("/tmp/cc-socks-1000")),
        context.get("liveness_map", {}),
        context.get("registered_slugs"),
    )


def _machine_label(context: dict[str, Any]) -> str:
    return context.get("machine_label") or os.environ.get("AI_HOST", "")


def _buzz_config_for_pair(
    context: dict[str, Any], pair: dict[str, Any] | None
) -> dict[str, Any]:
    """The `[buzz_notify]` facts the probe needs, for THIS machine's pair (T-2.R).

    Two derivations, both so the probe answers the same question the client does:

    * `relay_url` is DERIVED from the pair when the table leaves it empty, which is the shipped
      default -- otherwise the probe would refuse `buzz-not-configured` on every correctly
      configured machine, and the rung could never be chosen at all.
    * `enabled` is the PER-PAIR enable, because enabling Buzz is a per-pair act (a pair only
      works once its transport does). A probe reading the table-level flag would report the
      rung available on a machine whose pair was never declared live.

    An injected `buzz_config` in the context stays authoritative and is only FILLED IN, never
    overridden: that is the seam the resolver's tests and the daemon both pass facts through.
    """
    buzz_config = dict(context.get("buzz_config", {}))
    pair_name = str((pair or {}).get("name") or "")
    if (
        "enabled" not in buzz_config
        or not str(buzz_config.get("relay_url", "")).strip()
    ):
        try:
            from ai_chassis.buzz_channel import derive_relay_url, load_buzz_config
        except ImportError:
            return buzz_config
        if "enabled" not in buzz_config:
            try:
                loaded = load_buzz_config(pair=pair_name, env=context.get("env"))
            except Exception:  # noqa: BLE001 - an unreadable table is "not configured"
                loaded = {}
            buzz_config.setdefault("enabled", loaded.get("enabled", False))
            buzz_config.setdefault("relay_url", loaded.get("relay_url", ""))
        if not str(buzz_config.get("relay_url", "")).strip() and pair is not None:
            try:
                buzz_config["relay_url"] = derive_relay_url(pair)
            except Exception:  # noqa: BLE001 - an underivable URL is "not configured"
                buzz_config["relay_url"] = ""
    return buzz_config


def _target_parts(target: str) -> tuple[str, str]:
    """(machine, slug) of a concrete `<session>@<host>` target (ISSUE-REF), else a usage error."""
    machine, slug = _target_parts_or_empty(target)
    if not machine:
        raise TransportUsageError(
            f"target must be in <session>@<host> format, got: {target}"
        )
    return machine, slug


def _target_parts_or_empty(target: str) -> tuple[str, str]:
    try:
        address = parse_address(target)
    except AddressError:
        return "", ""
    if WILDCARD in (address.slug, address.machine):
        return "", ""
    return address.machine, address.slug


def target_machine_of(target: str) -> str:
    """The host half of a `<session>@<host>` target, or "" when there is none."""
    return _target_parts_or_empty(target)[0]


def _refuse_cross_pair_target(
    config: dict[str, Any],
    pair: dict[str, Any] | None,
    context: dict[str, Any],
) -> ProbeResult | None:
    """`target-not-in-pair` unless the target's machine shares the sender's pair. F-27.

    None means "nothing to object to here", which is also the answer when the context
    carries no target: this guard narrows a rung for a NAMED target, and a caller probing
    availability in general has not named one. The sender's own pair being absent is left
    to `buzz_relay_configured`'s `no-pair-for-machine`, which says it better.
    """
    target_machine = target_machine_of(str(context.get("target") or ""))
    if not target_machine or pair is None:
        return None
    # Same machine is trivially the same pair, and stays so even for a machine that is in
    # no pair at all -- `same-machine` scope never reaches this rung anyway.
    if target_machine == pair.get("relay_host") or target_machine in pair.get(
        "members", []
    ):
        return None
    target_pair = select_pair(config, target_machine)
    if target_pair is not None and target_pair.get("name") == pair.get("name"):
        return None
    return ProbeResult(available=False, reason=f"target-not-in-pair ({target_machine})")


def _probe_buzz_relay_configured(
    config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    """Assemble the `buzz` probe's inputs: injected facts first, cheap local reads else.

    `pair_transport_state` and `pair_transport_port_open` are authoritative when the
    context carries them (tests, and a daemon that already holds the file). Without
    them the probe reads the supervisor's state file and makes one loopback connect to
    the pair's local port -- both local, both bounded, so the resolve path stays cheap.
    """
    pair = select_pair(config, _machine_label(context))

    # D-9, audit F-27: a relay only reaches its OWN pair. The probe used to check the
    # sender's pair alone, so a target on another pair's machine selected `buzz`, the
    # local relay answered `OK true` for a publish no member of the target's pair would
    # ever see, and the ladder stopped above `git-inbox` -- a silent non-delivery that
    # read as success at both ends.
    #
    # FIRST, before any I/O: this needs no state file and no TCP connect, and it is the
    # most specific thing that can be wrong about this rung for this target.
    refusal = _refuse_cross_pair_target(config, pair, context)
    if refusal is not None:
        return refusal

    buzz_config = _buzz_config_for_pair(context, pair)
    if "pair_transport_state" in context:
        transport_state = context["pair_transport_state"]
    else:
        transport_state = read_pair_transport_state(
            Path(context["pair_transport_state_path"])
            if "pair_transport_state_path" in context
            else pair_transport_state_path(context.get("env"))
        )
    if "pair_transport_port_open" in context:
        port_open: bool | None = context["pair_transport_port_open"]
    elif pair is not None and pair.get("role") == "relay-host":
        # No supervisor here (see `buzz_relay_configured`): probe the relay itself, where the
        # daemon dials it.
        host = relay_probe_host(pair)
        port_open = bool(host) and local_port_open(int(pair["relay_port"]), host=host)
    elif pair is not None and transport_state is not None:
        # Extract local_port from the state file's forwards.relay.local_port
        forwards = transport_state.get("forwards", {})
        relay = forwards.get("relay", {}) if isinstance(forwards, dict) else {}
        state_port = relay.get("local_port") if isinstance(relay, dict) else None
        # N-11: validate state_port before probing
        if state_port is not None:
            # Accept only non-boolean integers in valid port range (1..65535)
            if not isinstance(state_port, int) or isinstance(state_port, bool):
                return ProbeResult(
                    available=False, reason="transport-state-invalid-port"
                )
            if state_port < 1 or state_port > 65535:  # noqa: PLR2004  # standard TCP port range
                return ProbeResult(
                    available=False, reason="transport-state-invalid-port"
                )
            host = relay_probe_host(pair)
            port_open = bool(host) and local_port_open(state_port, host=host)
        else:
            port_open = None
    else:
        port_open = None
    failure = _report_transport_use(context, transport_state, port_open)
    if failure:
        return failure
    return buzz_relay_configured(
        buzz_config,
        pair=pair,
        transport_state=transport_state,
        port_open=port_open,
        now=context.get("now"),
        stale_after_s=config.get("liveness", {}).get("stale_after_s", 180),
        channel_state=_buzz_channel_state(context),
    )


def _report_transport_use(context, state, port_open):
    if not state or state.get("event_driven") is not True:
        return None
    if "pair_transport_state" in context and "pair_transport_state_path" not in context:
        return None
    from ai_chassis.wake.tunnel import used

    path = (
        Path(context["pair_transport_state_path"])
        if "pair_transport_state_path" in context
        else pair_transport_state_path(context.get("env"))
    )
    try:
        used(path, "relay", port_open is True)
    except OSError:
        return ProbeResult(available=False, reason="transport-use-handoff-failed")
    return None


def _buzz_channel_state(context: dict[str, Any]) -> dict[str, Any]:
    """The Buzz daemon's protocol-health record, as a dict the probe can judge. F-28.

    Always a dict, never None: on this production path an unreadable or absent record IS a
    fact -- `buzz-state-missing` -- whereas `None` means "the caller injected nothing", and
    conflating the two is what would let the rung be chosen on a guess again.

    An injected `buzz_channel_state` stays authoritative, so a test can supply a record
    without a file and the daemon can pass one it already holds.
    """
    if "buzz_channel_state" in context:
        injected = context["buzz_channel_state"]
        return dict(injected) if isinstance(injected, dict) else {}
    try:
        from ai_chassis.buzz_channel import (
            buzz_key_dir,
            channel_state_path,
            read_channel_state,
        )
    except ImportError:
        return {}
    if "buzz_channel_state_path" in context:
        path = Path(context["buzz_channel_state_path"])
    else:
        path = channel_state_path(buzz_key_dir(context.get("env")))
    return read_channel_state(path) or {}


def _probe_agent_mail_status_fresh(
    config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    health = agent_mail_status_fresh(
        context.get("agent_mail_status", {}),
        context.get("now") or dt.datetime.now(dt.timezone.utc).isoformat(),
        config.get("liveness", {}).get("stale_after_s", 180),
    )
    if not health.available or "agent_mail_targets" not in context:
        return health
    # CLI callers inject an active directory, or None on lookup failure. Never perform
    # board I/O in a probe. Legacy library callers may still request health alone.
    names = context["agent_mail_targets"]
    if names is None:
        return ProbeResult(False, "board-directory-unavailable")
    # `resolve` only accepts a concrete `<session>@<host>` (broadcast targets are a usage
    # error before any probe runs), so the directory lookup is an exact board-name match.
    active = {name.casefold() for name in names}
    found = board_name(parse_address(context["target"])).casefold() in active
    return ProbeResult(found, "" if found else "target-not-on-board")


def _probe_git_remote_reachable(
    _config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    return git_remote_reachable(context.get("git_result", {"exit_status": 1}))


def _probe_firstmate_present(
    _config: dict[str, Any], context: dict[str, Any]
) -> ProbeResult:
    # An injected `firstmate_binary` (even None) is authoritative; without one, discover the
    # fork from the injected env, or the process environment when the context has no env.
    if "firstmate_binary" in context:
        installed = firstmate_present(context["firstmate_binary"])
    else:
        installed = firstmate_present(discover_firstmate_binary(context.get("env")))
    if not installed.available:
        return installed
    # ISSUE-REF: fm-send.sh delivers only to a Firstmate-registered secondmate, so the fork being
    # installed HERE says nothing about the target. The caller that knows the registry (the chief)
    # injects `firstmate_secondmates`; with none injected no target is one.
    target = str(context.get("target") or "")
    if target not in (context.get("firstmate_secondmates") or ()):
        return ProbeResult(
            available=False, reason=f"firstmate-no-secondmate ({target})"
        )
    return installed


def _probe_always(_config: dict[str, Any], _context: dict[str, Any]) -> ProbeResult:
    return always()


# Probe name as written in the config -> adapter that pulls its injected inputs out of the
# probe context. Adding a probe is a row here, never a branch in the resolver.
PROBE_DISPATCH: dict[str, Any] = {
    "native_socket_env": _probe_native_socket_env,
    "native_cross_machine_reach": _probe_native_cross_machine_reach,
    "peer_socket_and_live": _probe_peer_socket_and_live,
    "buzz_relay_configured": _probe_buzz_relay_configured,
    "agent_mail_status_fresh": _probe_agent_mail_status_fresh,
    "git_remote_reachable": _probe_git_remote_reachable,
    "firstmate_present": _probe_firstmate_present,
    "always": _probe_always,
}


def _run_probe(
    rung: str, config: dict[str, Any], context: dict[str, Any], scope: str
) -> ProbeResult:
    """Run the probe for a given rung.

    Args:
        rung: Rung name
        config: Full config dict
        context: Probe context with injected inputs
        scope: The scope being resolved, used to select from a per-scope probe map.

    Returns:
        ProbeResult from the appropriate probe function.
    """
    probe_spec = config.get("rungs", {}).get(rung, {}).get("probe", "")

    # A rung's `probe` may be one name or a per-scope map (design § Transport ladder).
    if isinstance(probe_spec, dict):
        if scope not in probe_spec:
            return ProbeResult(available=False, reason="no-probe-for-scope")
        probe_name = probe_spec[scope]
    else:
        probe_name = probe_spec

    probe = PROBE_DISPATCH.get(probe_name)
    if probe is None:
        return ProbeResult(available=False, reason=f"unknown-probe: {probe_name}")

    return probe(config, context)


# ----------------------------------------------------------------------------
# Machine messaging profile (D-10; T-4.P / T-4.1 / T-4.2) and the Firstmate
# derived view (D-1 amendment F-06; T-4.D)
# ----------------------------------------------------------------------------

# The three keys `[machine.<name>.messaging]` must declare. Role and transport kind are
# absent on purpose: they are DERIVED from the ladder's `pairs` block via select_pair(), so
# a pair's relay host and transport have exactly one source of truth (T-4.P).
MESSAGING_PROFILE_KEYS: tuple[str, ...] = (
    "pair",
    "buzz_primary",
    "agent_mail_fallback",
)

# Optional, non-secret, read by the installer when present: the board URL and the directory
# holding this machine's client identity. Never a credential -- client.env carries PATHS.
# relay_deploy (D-15): operator opt-in per relay host, default false.
MESSAGING_OPTIONAL_KEYS: tuple[str, ...] = (
    "agent_mail_url",
    "agent_mail_cert_dir",
    "relay_deploy",
)

# The relay's bind address is decided by the PAIR, not by the machine: a example-client-host pair's relay
# binds loopback (the SSM port-forward delivers to it), a personal pair's binds the tailnet
# address Tailscale hands out (D-8/D-9).
RELAY_BIND_LOOPBACK = "loopback"
RELAY_BIND_TAILNET = "tailnet"

# Rungs Firstmate can actually drive, in ladder order. `git-inbox` is excluded by the F-06
# amendment: it is the fleet's zero-dependency floor and Firstmate has no adapter for it, so
# listing it would name a fallback the consumer cannot take.
FIRSTMATE_DRIVABLE_RUNGS: tuple[str, ...] = ("buzz", "agent-mail", "fm-send")

# Carried over unchanged from the literal install.sh seeded before this derivation existed,
# so the derived file differs from that literal in `primary`/`fallbacks` only (F-06: "the
# timeouts carried over unchanged").
FIRSTMATE_OFFLINE_PENDING_TIMEOUT_S = 600
FIRSTMATE_NATIVE_ACTIVATION_TIMEOUT_S = 600


def machine_messaging_profile(
    site_config: dict[str, Any], machine: str
) -> dict[str, Any] | None:
    """``[machine.<machine>.messaging]`` from a parsed ai-harness.toml, or None.

    None means the profile declares no messaging table at all, which the installer reports
    as ``messaging: not declared``. That is a DIFFERENT state from a table whose
    capabilities are all off, and keeping the two distinguishable is why every machine gets
    a table rather than only the ones with something switched on.
    """
    table = (site_config.get("machine") or {}).get(machine) or {}
    messaging = table.get("messaging")
    return messaging if isinstance(messaging, dict) else None


def messaging_declares_a_capability(table: dict[str, Any] | None) -> bool:
    """Whether a per-machine capability table activates anything.

    THE ACTIVATION RULE, stated once here and shared with the activation-manifest guard: any
    truthy value activates. A machine declaring only configuration (a URL, a directory)
    therefore counts as activated, which is the SAFE direction for a coverage guard -- it
    demands a written decision rather than silently dropping the machine from the covered
    set, and silent dropping is the exact defect that manifest exists to forbid.
    """
    if not table:
        return False
    return any(bool(value) for value in table.values())


def messaging_install_plan(
    site_config: dict[str, Any], config: dict[str, Any], machine: str
) -> dict[str, Any]:
    """What install.sh must do for ``machine``, derived from the profile plus the ladder.

    The profile supplies membership and the two on/off switches; ``select_pair()`` supplies
    the role, the effective transport kind and the ports. Nothing here touches the host, so
    the installer's whole decision is a pure function of two tracked files and is testable
    without a machine to install onto.

    ``error`` is non-empty when the two files CONTRADICT each other -- a declared pair the
    ladder does not define, or one whose ``members`` omits this machine. The installer must
    refuse rather than act on a membership the ladder denies.
    """
    profile = machine_messaging_profile(site_config, machine)
    plan: dict[str, Any] = {
        "machine": machine,
        "declared": profile is not None,
        "pair": "",
        "buzz_primary": False,
        "agent_mail_fallback": False,
        "agent_mail_url": "",
        "agent_mail_cert_dir": "",
        "relay_deploy": False,
        "pair_role": "",
        "pair_transport": "",
        "pair_declared_transport": "",
        "pair_relay_port": "",
        "pair_local_port": "",
        "relay_bind_kind": "",
        "error": "",
    }
    if profile is None:
        return plan

    plan["pair"] = str(profile.get("pair", "") or "")
    plan["buzz_primary"] = bool(profile.get("buzz_primary", False))
    plan["agent_mail_fallback"] = bool(profile.get("agent_mail_fallback", False))
    plan["agent_mail_url"] = str(profile.get("agent_mail_url", "") or "")
    plan["agent_mail_cert_dir"] = str(profile.get("agent_mail_cert_dir", "") or "")
    plan["relay_deploy"] = bool(profile.get("relay_deploy", False))

    if not plan["pair"]:
        return plan

    pairs = config.get("pairs") or {}
    declared = pairs.get(plan["pair"])
    if declared is None:
        plan["error"] = (
            f"machine {machine!r} declares pair {plan['pair']!r}, which "
            "config/message-transports.json does not define"
        )
        return plan
    if machine not in declared.get("members", []):
        plan["error"] = (
            f"machine {machine!r} declares pair {plan['pair']!r}, but that pair's members "
            f"are {sorted(declared.get('members', []))}"
        )
        return plan

    selected = select_pair(config, machine)
    if selected is None:  # pragma: no cover - membership is already checked above
        plan["error"] = f"machine {machine!r} resolved to no pair"
        return plan

    plan["pair_role"] = selected["role"]
    plan["pair_transport"] = selected["transport"]
    plan["pair_declared_transport"] = selected["declared_transport"]
    plan["pair_relay_port"] = str(selected["relay_port"])
    plan["pair_local_port"] = str(selected["local_port"])
    if selected["role"] == "relay-host":
        loopback_machines = set(config.get("bms_machines") or [])
        plan["relay_bind_kind"] = (
            RELAY_BIND_LOOPBACK if machine in loopback_machines else RELAY_BIND_TAILNET
        )
    return plan


def firstmate_view(config: dict[str, Any]) -> dict[str, Any]:
    """The Firstmate chief-home ``message-transports.json``, DERIVED from the ladder.

    D-1's F-06 amendment: the tracked ``config/message-transports.json`` is the single
    source of truth and this consumer-shaped file becomes a derived view -- ``primary`` is
    the first rung of ``cross-machine``, ``fallbacks`` are the remaining cross-machine rungs
    Firstmate can drive, and the two timeouts carry over unchanged. Before this, install.sh
    wrote a hard-coded literal of the same shape, and the two files were free to drift into
    two sources of truth for one question.

    Raises:
        TransportConfigError: if the config declares no non-empty ``cross-machine`` ladder.
            A derived view with no primary rung is a silently empty configuration, and the
            installer's own failure path leaves the existing file untouched on this error.
    """
    ladder = (config.get("ladders") or {}).get("cross-machine")
    if not isinstance(ladder, list) or not ladder:
        raise TransportConfigError(
            "cannot derive the Firstmate view: the config declares no non-empty "
            "'cross-machine' ladder"
        )
    drivable = set(FIRSTMATE_DRIVABLE_RUNGS)
    return {
        "schema_version": 1,
        "primary": ladder[0],
        "fallbacks": [rung for rung in ladder[1:] if rung in drivable],
        "offline_pending_timeout_seconds": FIRSTMATE_OFFLINE_PENDING_TIMEOUT_S,
        "native_activation_timeout_seconds": FIRSTMATE_NATIVE_ACTIVATION_TIMEOUT_S,
    }


def messaging_activation(  # noqa: C901, PLR0912, PLR0915
    plan: dict[str, Any], artifacts: dict[str, Any], probes: dict[str, Any]
) -> dict[str, Any]:
    """Runtime activation evaluator for the messaging class (T-4.3, F-22).

    A declared capability passes when its artifact is installed AND its probe reports
    available or down-with-reason. An absent artifact never passes, regardless of probe.
    If a declared capability has no artifact, status is fail and failing names it.

    Args:
        plan: messaging_install_plan() output, declaring what the profile enables.
        artifacts: Installed artifacts checked by the installer:
            - agent_mail_client_env: bool (client.env exists)
            - agent_mail_unit_installed: bool
            - agent_mail_platform_skip_reason: str (non-empty when the platform cannot install)
            - buzz_relay_env: bool (relay.env exists)
            - buzz_supervisor_unit: bool
            - buzz_supervisor_state_readable: bool
        probes: Runtime probe results:
            - agent_mail: ProbeResult
            - buzz: ProbeResult

    Returns:
        dict with keys:
            - class: str = "messaging"
            - status: str = "pass" | "fail"
            - capabilities: list[dict] with name, declared, installed, probe, reason
            - failing: list[str] of capability names that failed
    """
    capabilities: list[dict[str, Any]] = []
    failing: list[str] = []

    # agent_mail_fallback capability
    if plan.get("agent_mail_fallback"):
        name = "agent_mail"
        declared = True
        platform_skip = artifacts.get("agent_mail_platform_skip_reason", "")
        client_env = artifacts.get("agent_mail_client_env", False)
        unit_installed = artifacts.get("agent_mail_unit_installed", False)
        probe = probes.get("agent_mail")

        installed = False
        reason = ""
        passed = False

        if not plan.get("agent_mail_url") and not client_env:
            reason = "no relay configured"
            passed = True
        elif platform_skip:
            # Platform cannot install the capability, counts as down-with-reason
            installed = True
            reason = f"platform-skip: {platform_skip}"
            passed = True
        elif client_env and unit_installed:
            installed = True
            if probe and probe.available:
                reason = "available"
                passed = True
            elif probe and probe.reason:
                reason = f"down-with-reason: {probe.reason}"
                passed = True
            else:
                reason = "unavailable"
                passed = False
        else:
            reason = "artifact-missing"
            installed = False
            passed = False

        capabilities.append(
            {
                "name": name,
                "declared": declared,
                "installed": installed,
                "probe": "available"
                if probe and probe.available
                else (probe.reason if probe and probe.reason else "unavailable"),
                "reason": reason,
            }
        )

        if not passed:
            failing.append(name)

    # pair capability (buzz_primary must be true for activation)
    if plan.get("pair"):
        name = "buzz"
        declared = True
        buzz_primary = plan.get("buzz_primary", False)
        pair_role = plan.get("pair_role", "")
        relay_deploy = plan.get("relay_deploy", False)

        installed = False
        reason = ""
        passed = False
        probe = probes.get("buzz")

        if not buzz_primary:
            # buzz_primary = false means the capability is declared but not activated
            reason = "buzz_primary=false"
            installed = False
            passed = False
        elif pair_role == "relay-host":
            relay_env = artifacts.get("buzz_relay_env", False)
            if not relay_deploy:
                # D-15 relay_deploy = false skip, counts as down-with-reason
                installed = True
                reason = "down-with-reason: relay_deploy=false"
                passed = True
            elif relay_env:
                installed = True
                if probe and probe.available:
                    reason = "available"
                    passed = True
                elif probe and probe.reason:
                    reason = f"down-with-reason: {probe.reason}"
                    passed = True
                else:
                    reason = "unavailable"
                    passed = False
            else:
                reason = "artifact-missing (relay.env)"
                installed = False
                passed = False
        elif pair_role == "member":
            supervisor_unit = artifacts.get("buzz_supervisor_unit", False)
            state_readable = artifacts.get("buzz_supervisor_state_readable", False)
            if supervisor_unit and state_readable:
                installed = True
                if probe and probe.available:
                    reason = "available"
                    passed = True
                elif probe and probe.reason:
                    reason = f"down-with-reason: {probe.reason}"
                    passed = True
                else:
                    reason = "unavailable"
                    passed = False
            else:
                reason = "artifact-missing (supervisor unit or state file)"
                installed = False
                passed = False
        else:
            reason = f"unknown pair_role: {pair_role}"
            installed = False
            passed = False

        capabilities.append(
            {
                "name": name,
                "declared": declared,
                "installed": installed,
                "probe": "available"
                if probe and probe.available
                else (probe.reason if probe and probe.reason else "unavailable"),
                "reason": reason,
            }
        )

        if not passed and buzz_primary:
            failing.append(name)

    status = "fail" if failing else "pass"
    return {
        "class": "messaging",
        "status": status,
        "capabilities": capabilities,
        "failing": failing,
    }
