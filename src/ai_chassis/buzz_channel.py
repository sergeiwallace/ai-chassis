"""The `buzz` rung's channel client: keys, one NIP-42 relay connection, publish, presence.

ISSUE-REF, Phase 2 of design `docs/designs/issue-ref-buzz-primary-cross-machine-messaging.md`.

This module is a CHANNEL, not a daemon. `agent_mail_notifier` owns the one daemon, the one
lock and the one `notice.id` writer on a machine (D-2); everything here is called from inside
that daemon's existing tick, and `BuzzChannel.poll()` hands back parsed messages for the
notifier's own marker path rather than writing a marker itself. A second writer would break the
single-writer atomicity the three wake hooks rely on, and `FileChanged` is not de-duplicated
upstream -- so the absence of a write call in this file is load-bearing, and
`tests/test_buzz_channel.py::TestSingleWriter` pins it structurally over `src/`.

Three layers, bottom up:

* **Identity** -- `ensure_identity` mints or loads one Schnorr keypair per fleet identity
  (`<slug>@<machine>`, plus `daemon@<machine>`) under the per-user config dir at `0600`.
  A looser mode is a REFUSAL naming the octal, never a silent chmod: the key is the identity,
  and a world-readable identity is one any local process can speak as.
* **Wire** -- `RelayConnection` speaks just enough NIP-01/NIP-42 over one WebSocket:
  `AUTH` challenge -> signed `kind 22242`, `REQ`/`EVENT`/`CLOSE`, and `OK` results read back
  rather than assumed. D-11 chose nostr-sdk's PRIMITIVES plus this fleet-owned wire layer
  exactly so the challenge, the `OK` and the reconnect are visible and testable; the SDK's own
  relay client hides all three.
* **Channel** -- `BuzzChannel` is the façade: one connection, the fleet's channel conventions
  (`#fleet`, `#m-<machine>`, DM to a session key -- D-3), presence, NIP-29 group lifecycle, and
  a `status()` view. Every method is non-raising by contract: the rung reporting unavailable
  with a reason is a delivery outcome, and the daemon must keep serving agent mail either way.

Both third-party imports are LAZY and their absence is a reported state, not a crash
(`BUZZ_IMPORT_MISSING_REASON`): one daemon serves every rung, so a machine whose venv predates
the `buzz` extra has to keep delivering agent mail.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_chassis.session_address import (
    WILDCARD,
    Address,
    AddressError,
    parse_address,
    session_address,
)

# ---------------------------------------------------------------------------------------------
# Protocol constants. Every one is a Nostr/Buzz wire fact, sourced in the design's § Buzz channel.
# ---------------------------------------------------------------------------------------------

#: NIP-42 client authentication event (VERIFIED from Buzz's NOSTR.md).
#: NIP-01 `kind 0` profile metadata. D-16 uses it as the fleet's name -> pubkey directory:
#: each identity publishes `{"name": "<slug>@<machine>"}` and the daemon subscribes to it, so
#: a sender can learn a REMOTE session's public key without ever touching a private key file.
KIND_METADATA = 0

KIND_AUTH = 22242
#: NIP-29 group chat message. Buzz channels are relay-enforced groups addressed by an `h` tag.
KIND_CHAT = 9
#: NIP-59 gift wrap. The only kind a DM is visible as on the wire.
KIND_GIFT_WRAP = 1059
#: NIP-17 chat rumor, sealed inside the wrap. Never published directly.
KIND_PRIVATE_MESSAGE = 14
#: Buzz presence. Redis-leased relay-side; the fleet never trusts that lease (D-4).
KIND_PRESENCE = 20001
#: NIP-29 group create / add-member (OQ-8, resolved by ISSUE-REF). The pinned relay reads an
#: `h` tag ONLY as a channel UUID (`handlers/ingest.rs:612-623`), requires a `name` tag on a
#: create (`:3005-3026`) and admits the FIRST `p` of an add (`side_effects.rs:1435-1442`).
KIND_GROUP_CREATE = 9007
KIND_GROUP_ADD_USER = 9000
#: The relay's answer to a create of a UUID that already exists (`ingest.rs:3091-3096`): the
#: channel is THERE, which is what the create was for, so it is not a refusal.
GROUP_EXISTS_PREFIX = "duplicate:"
#: `publish`'s reason when no `OK` for the event arrived within `ok_timeout_s`.
NO_OK_REASON = "no-ok-from-relay"
#: D-21's rotation attestation: an event signed by the key a name is CURRENTLY bound to,
#: asserting the name's next key. It is NEVER published -- it travels inside the new key's
#: `kind 0` as a `rotation` tag. A dedicated kind is what stops an attestation being forged
#: out of some other event the bound key happened to sign: `rotation_authorizes` requires
#: this kind, so no `kind 0`, `kind 9` or `AUTH` event the bound key ever signed can stand in.
KIND_NAME_ROTATION = 20002
#: issue-ref D-20: a remote session-launch request, addressed to `daemon@<machine>`, and its
#: reply to the requester. The kind is VISIBLE on the wire (not sealed inside a `kind 1059`), so
#: an OFF machine can decline to subscribe to it at all; the content is NIP-44 encrypted to the
#: addressee and the event is Schnorr-signed by the sender's own session key, so F-30's check on
#: the outer event is the authentication. Ephemeral range: a relay forwards, never stores.
KIND_LAUNCH_REQUEST = 20003
KIND_LAUNCH_RESULT = 20004

#: Buzz's own documented tolerance on an `AUTH` event's `created_at`: +/- 60 seconds.
AUTH_TIMESTAMP_TOLERANCE_S = 60

#: Mode a key file must have, and the mode a minted one is created with.
KEY_FILE_MODE = 0o600
#: Mode the key DIRECTORY is created with. Never widened to repair a key's own mode.
KEY_DIR_MODE = 0o700

#: The fleet's channel conventions (D-3). Channel NAMES, so no leading `#`; on the wire a
#: channel is its UUID (:func:`derive_channel_id`), never its name.
DEFAULT_FLEET_CHANNEL = "fleet"
DEFAULT_MACHINE_CHANNEL_PREFIX = "m-"
#: Namespace of the fleet's deterministic channel UUIDs. Every member of a pair derives the
#: SAME UUID for the same name, so the first daemon's create and every later one converge on
#: one `#fleet` rather than each minting its own channel nobody else reads.
CHANNEL_UUID_NAMESPACE = uuid.UUID("5f0c7a3e-1d2b-4e6a-9c8d-7b3a2f1e0d4c")
#: The persisted name -> channel UUID map, beside the keys. A persisted entry wins over the
#: derivation, so an operator can point a name at a channel the relay already has.
CHANNEL_MAP_FILE = "channels.json"
#: What this daemon has already handed to delivery (ISSUE-REF): per identity and per event family,
#: a high-water mark and a bounded set of event ids, so a relay that replays its stored history on
#: every REQ delivers each chat message and gift wrap ONCE, across ticks and restarts.
DELIVERED_FILE = "delivered.json"
DELIVERED_SCHEMA = 1
#: How far behind the high-water mark a REQ's `since` reaches. Events from two machines' clocks,
#: or published out of order, land a little behind the newest one already seen; the seen-set drops
#: what that overlap replays, so the window costs a replay and never a duplicate.
DELIVERY_SKEW_S = 600
#: The seen-set's size bound per identity and family. An overflow evicts the oldest ids and raises
#: that family's `since` floor past them, so an evicted id cannot be replayed and re-delivered.
DELIVERED_SEEN_CAP = 2048
DELIVERY_FAMILY_DM = "dm"
DELIVERY_FAMILY_CHAT = "chat"

#: Sender-side size cap. The relay does not enforce rate limiting, so the sender does.
DEFAULT_MAX_BYTES = 4096
#: Inbound wire-size cap for the raw event content BEFORE decryption (N-09, ISSUE-REF).
#: Gift wraps add ~2.74x overhead (measured: 100 → 1456, 3000 → 8284, 4096 → 11012 bytes),
#: so 4x plaintext cap accommodates legitimate messages while stopping malicious payloads.
DEFAULT_MAX_WIRE_BYTES = 4 * DEFAULT_MAX_BYTES
#: Presence cadence. 60 s against a documented `EX 180` lease; Buzz issue #3795 reports the
#: implementation at 90 s, which 60 s also covers (OQ-7).
DEFAULT_PRESENCE_INTERVAL_S = 60

#: The three liveness words. `undetermined` is never `gone` -- the existing D-8 discipline.
LIVENESS_LIVE = "live"
LIVENESS_IDLE = "idle"
LIVENESS_UNDETERMINED = "undetermined"

#: `delivered-via` words `buzz send --to` prints and `status()` counts.
DELIVERED_VIA_DM = "dm"
DELIVERED_VIA_MENTION = "mention"

#: D-16's refusal: `--to <name>` whose pubkey the kind-0 directory does not carry publishes
#: NOTHING. The alternative this replaces published an UNTAGGED `kind 9` into the recipient's
#: machine channel, which every live session on that machine then received (audit F-26).
RECIPIENT_KEY_UNKNOWN_REASON = "recipient-key-unknown"

#: The name -> pubkey directory the daemon keeps from the `kind 0` subscription (D-16), beside
#: the keys in the Buzz config directory. Private: it maps fleet identities to public keys.
PUBKEY_DIRECTORY_FILE = "pubkeys.json"

#: D-21's tag name on a `kind 0`, carrying the rotation attestation's event JSON.
ROTATION_TAG = "rotation"
#: A NIP-01 tag carrying a value is `[<name>, <value>]`: two parts, at least.
MIN_VALUE_TAG_PARTS = 2

#: D-21's refusal: a verified `kind 0` claiming a name ALREADY bound to a different key, with
#: no rotation co-signed by that key. Logged with both pubkeys, because the operator reading it
#: has to tell a lost-key rotation (reset the entry) from a takeover attempt (do not).
NAME_ALREADY_BOUND_REASON = "name-already-bound"

#: How much of a pubkey a log line carries. Enough to tell two keys apart in a refusal, and
#: not a full key: a refusal line is read by a human comparing it against `buzz status`.
PUBKEY_LOG_CHARS = 8

#: Where the daemon publishes the PROTOCOL health the resolver's probe needs (audit F-28):
#: a TCP listener proves a socket, not a working WSS + AUTH + subscription.
CHANNEL_STATE_FILE = "state.json"
CHANNEL_STATE_SCHEMA = 1

#: Peer presence records (`kind 20001`) the daemon receives and persists (Phase 3, ISSUE-REF).
#: Aged by `generated_at` with `liveness.stale_after_s`, never by the relay's own lease (OQ-7).
PRESENCE_FILE = "presence.json"

#: How a missing third-party import is reported. A reason, not an exception: the rung is
#: unavailable and the daemon carries on.
BUZZ_IMPORT_MISSING_REASON = "buzz-not-configured (nostr-sdk missing)"
WEBSOCKETS_IMPORT_MISSING_REASON = "buzz-not-configured (websockets missing)"

#: Reconnect backoff, capped. A relay that is down must not become a hot loop in a 10 s tick.
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
BACKOFF_FACTOR = 2.0

#: One network wait for a dial or a send, so a tick cannot hang the daemon that serves the
#: other rungs.
DEFAULT_TIMEOUT_S = 5.0
#: The bound on a read that waits for an ANSWER the client asked for: the AUTH challenge after a
#: dial, a REQ's EOSE or CLOSED, a publish's OK. A drain (`pump`) never waits at all (issue-ref
#: D-26): at 0.25 s per quiet socket it cost ~4 s of every tick on nine sockets. The relay is on
#: loopback or a local port-forward, so an answer arrives in microseconds; a quarter second is
#: three orders of magnitude of headroom.
DEFAULT_READ_TIMEOUT_S = 0.25
#: How long `publish` waits for an event's `OK`, which is a THIRD question: not how long a frame
#: takes in flight but how long the relay takes to PROCESS the event. The pinned relay answers a
#: `kind 9000` self-add 0.5-1.6 s after the send (ISSUE-REF, example-project-ec2 2026-10-11): it stores
#: the membership, signs and fans out a `kind 44100`, then answers. Bounding that wait by two
#: quiet reads (0.5 s) reported every accepted self-add as `no-ok-from-relay`. An integer
#: because `[buzz_notify]` merges ints, bools and strings only (`merge_typed_section`).
DEFAULT_OK_TIMEOUT_S = 3
#: issue-ref D-25: a live subscription is renewed with one replacement REQ after this many
#: presence intervals, whatever else the socket carried, so a relay that drops it silently
#: (no CLOSED) costs at most one lease of DMs rather than the rest of the connection.
LEASE_PRESENCE_INTERVALS = 2
#: Why `RelayConnection.subscription_due` says a REQ is owed; "" when none is.
DUE_OPEN = "open"
DUE_CLOSED = "closed"
DUE_FILTERS = "filters"
DUE_LEASE = "lease"

#: `message_id` is an int because `Message` is shared with agent mail, whose board ids are ints.
#: 48 bits of the event id: wide enough that a collision is not a practical concern and small
#: enough to stay inside a JSON-safe integer on every fleet runtime.
EVENT_ID_BITS = 48


class BuzzError(RuntimeError):
    """A refusal this module names. Its text is a classified reason, never key material."""


# ---------------------------------------------------------------------------------------------
# Lazy third-party imports
# ---------------------------------------------------------------------------------------------


def nostr_sdk_module() -> Any:
    """The `nostr_sdk` module, or a :class:`BuzzError` naming its absence.

    Imported through a function so every caller shares one failure word and the test suite can
    block the import by substituting this function -- the import-failure degrade path is then
    exercised against the real call sites rather than described.
    """
    try:
        import nostr_sdk
    except ImportError as exc:
        raise BuzzError(BUZZ_IMPORT_MISSING_REASON) from exc
    return nostr_sdk


def websockets_connect() -> Any:
    """`websockets.asyncio.client.connect`, or a :class:`BuzzError` naming its absence."""
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise BuzzError(WEBSOCKETS_IMPORT_MISSING_REASON) from exc
    return connect


def dial_kwargs(dial: tuple[str, int] | None) -> dict[str, Any]:
    """`connect()`'s TCP-destination keywords for a dial target, or none to dial the URI."""
    if dial is None:
        return {}
    return {"host": dial[0], "port": int(dial[1])}


# ---------------------------------------------------------------------------------------------
# Relay readiness (ISSUE-REF)
# ---------------------------------------------------------------------------------------------

#: How much of a refused upgrade's body a probe reason carries: enough for the relay's one-line
#: refusal, never a page of HTML.
PROBE_BODY_CHARS = 120


@dataclass
class RelayProbeResult:
    """One WebSocket upgrade attempt against a relay, as the relay answered it."""

    url: str
    ok: bool
    status: int | None
    reason: str


async def probe_relay_upgrade(
    url: str,
    *,
    dial: tuple[str, int] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    connect_factory: Any = None,
) -> RelayProbeResult:
    """Perform a real WebSocket upgrade against ``url`` and report what the relay said.

    This, not `GET /_readiness`, is the relay's health for the fleet: `/_readiness` answers
    200 on a relay that 404s every upgrade because no community is mapped to the Host the
    client sends, and that relay carries no message (ISSUE-REF, measured 2026-10-10). The
    Host header is the URL's own authority, so the probe asks the question a member's dial
    asks; `dial` lands the socket where the member's own does (:class:`RelayEndpoint`).
    """
    factory = connect_factory or websockets_connect()
    try:
        socket = await asyncio.wait_for(
            factory(url, **dial_kwargs(dial)), timeout=timeout_s
        )
    except BuzzError:
        raise
    except Exception as exc:  # noqa: BLE001 - every refusal is reported, never raised
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            body = bytes(getattr(response, "body", b"") or b"")
            text = body.decode("utf-8", "replace").strip()[:PROBE_BODY_CHARS]
            return RelayProbeResult(
                url, False, int(status), f"upgrade-refused ({status}: {text})"
            )
        return RelayProbeResult(
            url, False, None, f"relay-unreachable ({type(exc).__name__})"
        )
    with contextlib.suppress(Exception):
        await socket.close()
    return RelayProbeResult(url, True, 101, "upgraded")


# ---------------------------------------------------------------------------------------------
# T-2.1 Identity
# ---------------------------------------------------------------------------------------------


def buzz_key_dir(env: dict[str, str] | None = None) -> Path:
    """Where this user's Buzz keys live: `$XDG_CONFIG_HOME/ai-harness/buzz`.

    XDG rather than `platformdirs` so the directory is a pure function of the injected
    environment and a test never writes into the real one.
    """
    environment = os.environ if env is None else env
    root = (environment.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(root) if root else Path.home() / ".config"
    return base / "ai-harness" / "buzz"


#: The machine daemon's reserved session half: `daemon@<machine>`.
DAEMON_SLUG = "daemon"


def identity_name(machine: str, slug: str) -> str:
    """`<slug>@<machine>`, the one session identifier every rung shares (ISSUE-REF)."""
    return session_address(machine, slug)


def daemon_identity_name(machine: str) -> str:
    """The machine daemon's own identity, which publishes for sessions with no key yet."""
    return session_address(machine, DAEMON_SLUG)


@dataclass(frozen=True)
class BuzzIdentity:
    """One fleet identity's keypair. `secret_bech32` is never logged, printed or serialised."""

    name: str
    pubkey_hex: str
    secret_bech32: str
    path: Path

    def keys(self) -> Any:
        """The nostr-sdk `Keys` for this identity, reconstructed on demand."""
        return nostr_sdk_module().Keys.parse(self.secret_bech32)


def _key_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.key"


def legacy_identity_name(name: str) -> str:
    """The pre-ISSUE-REF `<machine>--<slug>` name of `name`, or "" when it does not parse."""
    _, machine, slug = parse_identity_parts(name)
    return f"{machine}--{slug or DAEMON_SLUG}" if machine else ""


def _adopt_legacy_key(directory: Path, name: str, path: Path) -> None:
    """Move `<machine>--<slug>.key` to `name`'s key path, once, so the key survives the rename.

    Before ISSUE-REF an identity was named `<machine>--<slug>`. Peers' `allow_senders` and every
    channel membership are keyed by the PUBKEY, so a fresh key under the new name would silently
    lock this session out of both; the old key is moved, never copied, so exactly one file
    holds it. A rename that loses a race with another process is left to that process.
    """
    legacy_name = legacy_identity_name(name)
    if not legacy_name:
        return
    legacy = _key_path(directory, legacy_name)
    with contextlib.suppress(FileNotFoundError, FileExistsError):
        os.rename(legacy, path)


def _unreadable_key(path: Path, fault: str) -> BuzzError:
    return BuzzError(
        f"key-unreadable ({path.name} {fault}; move {path} aside and restart the notifier "
        "to mint a new key)"
    )


def _load_identity(name: str, path: Path) -> BuzzIdentity:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & ~KEY_FILE_MODE:
        raise BuzzError(
            f"key-permissions ({path.name} is 0o{mode:o}, expected 0o{KEY_FILE_MODE:o}; "
            "fix with chmod 600 and restart the notifier)"
        )
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise _unreadable_key(path, "is empty")
    sdk = nostr_sdk_module()
    try:
        keys = sdk.Keys.parse(secret)
    except Exception:  # noqa: BLE001  # whatever the SDK raises, and never its text: it may quote the key
        raise _unreadable_key(path, "is not a key") from None
    return BuzzIdentity(
        name=name,
        pubkey_hex=keys.public_key().to_hex(),
        secret_bech32=secret,
        path=path,
    )


def ensure_identity(name: str, *, directory: Path) -> BuzzIdentity:
    """Load `<name>.key`, minting one at `0600` when absent. T-2.1.

    A key file whose mode is LOOSER than `0600` is refused naming the octal mode, and nothing
    is read from it. The alternative -- repairing the mode and carrying on -- would silently
    erase the only evidence that some other process could read, or has already read, this
    machine's identity. Group and other bits are what "looser" means; the owner-execute bit is
    not a disclosure, so it is not what this guard is for.

    A new key is written whole to a `0600` temp file beside it and published with `os.link`,
    which refuses an existing name: a crash mid-write leaves no key, and the loser of a
    creation race loads the winner's key (ISSUE-REF).
    """
    path = _key_path(directory, name)
    if not path.exists():
        _adopt_legacy_key(directory, name, path)
    if path.exists():
        return _load_identity(name, path)

    keys = nostr_sdk_module().Keys.generate()
    secret = keys.secret_key().to_bech32()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, KEY_DIR_MODE)
    # mkstemp creates the file at 0600, so the key is never readable by anyone else, even
    # briefly; the mode guard above would never see a key that was.
    handle, staged = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=directory
    )
    try:
        try:
            os.write(handle, f"{secret}\n".encode())
            os.fsync(handle)
        finally:
            os.close(handle)
        try:
            os.link(staged, path)
        except FileExistsError:
            return _load_identity(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged)
    return BuzzIdentity(
        name=name,
        pubkey_hex=keys.public_key().to_hex(),
        secret_bech32=secret,
        path=path,
    )


# ---------------------------------------------------------------------------------------------
# T-2.2 Relay URL derivation
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayEndpoint:
    """Which community a member joins, and where its TCP connection goes (ISSUE-REF).

    The relay serves ONE community, keyed by the Host header's authority, and checks the
    NIP-42 `relay` tag against the same authority (ISSUE-REF). `identity_url` is that
    authority, and it is the same string on every member of a pair. `dial` is where the
    socket actually lands when that differs, e.g. a port forward on another local port:
    `websockets` takes `host=`/`port=` as the TCP destination and still sends the URI's
    Host (docs/research/buzz-relay-community-configuration.md, option B).
    """

    identity_url: str
    dial: tuple[str, int] | None = None


def derive_relay_endpoint(
    pair: dict[str, Any] | None,
    *,
    explicit: str = "",
) -> RelayEndpoint:
    """The community this machine joins for its pair's relay, and where it dials. T-2.2.

    An explicit `[buzz_notify].relay_url` wins -- that is the per-machine override the ops
    procedure documents -- and is dialled as written. Otherwise the endpoint is DERIVED from
    the pair, because the pair already carries the only facts that decide it (D-9). The
    identity is the authority the relay seeds its community from (`RELAY_URL` =
    `ws://<bind>:<relay_port>` in `infra/buzz/compose.yaml`):

    * `ssm-port-forward` or `loopback` pair -> the relay binds loopback, so the identity is
      `ws://127.0.0.1:<relay_port>` on every member. A port-forward member dials its
      `local_port` when that differs.
    * `tailscale` pair -> the relay binds the relay host's tailnet IPv4, so the identity is
      `ws://<relay_address>:<relay_port>` on every member, the relay host included (nothing
      listens on its loopback). Only the local overlay knows that address: its absence is a
      configuration refusal naming the key, and a hostname is refused too, because the relay
      never seeds a community for one.

    Raises:
        BuzzError: `buzz-not-configured (...)` naming what is missing.
    """
    if explicit.strip():
        return RelayEndpoint(explicit.strip())
    if pair is None:
        raise BuzzError("buzz-not-configured (no-pair-for-machine)")
    # `select_pair` reports `loopback` as the relay host's EFFECTIVE transport; where the
    # relay binds is the pair's declared one.
    transport = pair.get("declared_transport") or pair.get("transport")
    relay_port = int(pair["relay_port"])
    if transport in ("ssm-port-forward", "loopback"):
        identity = RelayEndpoint(f"ws://127.0.0.1:{relay_port}")
        local_port = int(pair.get("local_port") or relay_port)
        if pair.get("role") == "relay-host" or local_port == relay_port:
            return identity
        return RelayEndpoint(identity.identity_url, dial=("127.0.0.1", local_port))
    if transport == "tailscale":
        address = str(pair.get("relay_address") or "").strip()
        if not address:
            raise BuzzError("buzz-not-configured (relay_address)")
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            raise BuzzError(
                "buzz-not-configured (relay_address must be the relay host's tailnet IPv4)"
            ) from None
        return RelayEndpoint(f"ws://{address}:{relay_port}")
    raise BuzzError(f"buzz-not-configured (transport {transport!r})")


def derive_relay_url(
    pair: dict[str, Any] | None,
    *,
    explicit: str = "",
) -> str:
    """The pair's relay identity URL: what every member's Host and NIP-42 tag name."""
    return derive_relay_endpoint(pair, explicit=explicit).identity_url


# ---------------------------------------------------------------------------------------------
# Event construction, from nostr-sdk primitives
# ---------------------------------------------------------------------------------------------


def _timestamp(sdk: Any, when: int) -> Any:
    return sdk.Timestamp.from_secs(int(when))


def build_event(
    keys: Any,
    kind: int,
    content: str,
    tags: list[list[str]],
    *,
    created_at: int,
) -> Any:
    """One signed event of any kind, from the generic builder.

    nostr-sdk's Python FFI exposes `EventBuilder(kind, content)` with `.tags()`,
    `.custom_created_at()` and `.finalize(signer)` -- and NO per-NIP named constructors for
    `kind 22242`, `9`, `20001`, `9007` or `9000` (probed against 0.45.1, 2026-10-08). Those are
    therefore built HERE from the primitive, which is what D-11 chose.
    """
    sdk = nostr_sdk_module()
    builder = sdk.EventBuilder(sdk.Kind(kind), content)
    if tags:
        builder = builder.tags([sdk.Tag.parse(tag) for tag in tags])
    builder = builder.custom_created_at(_timestamp(sdk, created_at))
    return builder.finalize(keys)


def build_auth_event(keys: Any, relay_url: str, challenge: str, *, now: int) -> Any:
    """The NIP-42 response to `["AUTH", "<challenge>"]`: `kind 22242`, `relay` + `challenge`.

    `created_at` is NOW and nothing else: Buzz documents a +/-60 s tolerance, so a stamp from
    anywhere but the clock is a rejection waiting to happen.
    """
    return build_event(
        keys,
        KIND_AUTH,
        "",
        [["relay", relay_url], ["challenge", challenge]],
        created_at=now,
    )


def build_chat_event(
    keys: Any,
    channel: str,
    text: str,
    *,
    now: int,
    mention_pubkeys: list[str] | None = None,
) -> Any:
    """A `kind 9` group message: `["h", <channel>]`, plus a `p` tag per mention."""
    tags = [["h", channel]]
    for pubkey in mention_pubkeys or []:
        tags.append(["p", pubkey])
    return build_event(keys, KIND_CHAT, text, tags, created_at=now)


def build_metadata_event(keys: Any, name: str, *, now: int, rotation: str = "") -> Any:
    """A NIP-01 `kind 0` profile announcing `name` for this key. D-16.

    `{"name": "<slug>@<machine>"}` and nothing else: the directory's only job is to answer
    "which pubkey is this identity?", and every extra field would be fleet metadata published
    to a relay for no gain.

    `rotation` is D-21's attestation JSON, carried as a `rotation` tag when this key is
    REPLACING the one the name is currently bound to. Omitted on a first binding and on a
    same-key refresh, which is every announcement in steady state.
    """
    return build_event(
        keys,
        KIND_METADATA,
        json.dumps({"name": name}, sort_keys=True),
        [[ROTATION_TAG, rotation]] if rotation else [],
        created_at=now,
    )


def build_rotation_attestation(
    bound_keys: Any, name: str, new_pubkey: str, *, now: int
) -> str:
    """The JSON of a `KIND_NAME_ROTATION` event by the BOUND key, naming the next key. D-21.

    Signed by the key the name is currently bound to, over BOTH the name and the incoming
    pubkey. Both, because an attestation naming only the new key could be replayed to move a
    DIFFERENT name onto that key, and one naming only the name could be replayed to move it
    onto any key -- and either replay is the redirect D-21 exists to close.

    Never published to a relay: the new key's own `kind 0` carries this as a tag, so the
    attestation and the claim it authorises arrive together or not at all.
    """
    return build_event(
        bound_keys,
        KIND_NAME_ROTATION,
        json.dumps({"name": name, "pubkey": new_pubkey}, sort_keys=True),
        [],
        created_at=now,
    ).as_json()


def rotation_attestation(event: RelayEvent) -> str:
    """The `rotation` tag's attestation JSON on a `kind 0`, or "" when it carries none."""
    for tag in event.tags:
        if len(tag) >= MIN_VALUE_TAG_PARTS and tag[0] == ROTATION_TAG:
            return tag[1]
    return ""


def _decoded_object(raw: object) -> dict[str, Any] | None:
    """A JSON object decoded from `raw`, or None when `raw` is not one. Never raises."""
    if not isinstance(raw, str):
        return None
    try:
        node = json.loads(raw)
    except ValueError:
        return None
    return node if isinstance(node, dict) else None


def rotation_authorizes(
    attestation_json: str,
    *,
    name: str,
    new_pubkey: str,
    bound_pubkey: str,
) -> bool:
    """Whether the currently bound key authorised `name -> new_pubkey`. D-21.

    Authenticity is checked by `validate_event_node`, the SAME primitive the inbound-safety
    boundary uses (audit F-30), rather than by a detached Schnorr check: nostr-sdk 0.45.1
    exposes `Keys.sign_schnorr` but NO detached verify (probed 2026-10-08), so a bare
    `(pubkey, sig)` pair would be unverifiable with the dependency D-11 chose. Wrapping the
    attestation in an event makes the existing verifier the whole check.

    Fail-closed at every step, including a missing SDK: `validate_event_node` answers
    `unverifiable (nostr-sdk missing)` there, which is not "" and so refuses. An unverifiable
    rotation must never rebind a name.
    """
    node = _decoded_object(attestation_json) if bound_pubkey else None
    if node is None:
        return False
    # The signer and the kind BEFORE the signature check: a valid signature by the wrong key,
    # or by the right key over some other kind of event, is not an authorisation.
    if node.get("pubkey") != bound_pubkey or node.get("kind") != KIND_NAME_ROTATION:
        return False
    if validate_event_node(node) != "":
        return False
    claim = _decoded_object(node.get("content"))
    return (
        claim is not None
        and claim.get("name") == name
        and claim.get("pubkey") == new_pubkey
    )


def metadata_name(event: RelayEvent) -> str:
    """The `name` a `kind 0` announces, or "" when it announces none we can use."""
    try:
        document = json.loads(event.content)
    except ValueError:
        return ""
    if not isinstance(document, dict):
        return ""
    name = document.get("name")
    return name.strip() if isinstance(name, str) else ""


def pubkey_directory_path(directory: Path) -> Path:
    """Where the name -> pubkey directory lives, beside this user's keys."""
    return directory / PUBKEY_DIRECTORY_FILE


def channel_state_path(directory: Path) -> Path:
    """Where the daemon publishes protocol health for the resolver's probe. F-28."""
    return directory / CHANNEL_STATE_FILE


def presence_file_path(directory: Path) -> Path:
    """Where the daemon persists peer presence records from `kind 20001` events. ISSUE-REF."""
    return directory / PRESENCE_FILE


def channel_map_path(directory: Path) -> Path:
    """Where the name -> channel UUID map lives (OQ-8, ISSUE-REF)."""
    return directory / CHANNEL_MAP_FILE


def delivered_path(directory: Path) -> Path:
    """Where the per-identity delivered high-water marks and seen ids live (ISSUE-REF)."""
    return directory / DELIVERED_FILE


def load_delivered(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """identity -> family -> `{high_water, floor, seen}` as persisted; `{}` when absent.

    A malformed entry is dropped rather than raised on: the cost is one replay of that family's
    history, delivered once more, and a raise here would take down the tick that serves agent mail.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    identities = document.get("identities") if isinstance(document, dict) else None
    if not isinstance(identities, dict):
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, families in identities.items():
        if not isinstance(name, str) or not isinstance(families, dict):
            continue
        for family, record in families.items():
            if not isinstance(record, dict):
                continue
            seen = record.get("seen")
            out.setdefault(name, {})[str(family)] = {
                "high_water": _int_or_zero(record.get("high_water")),
                "floor": _int_or_zero(record.get("floor")),
                "seen": {
                    str(event_id): _int_or_zero(created_at)
                    for event_id, created_at in (
                        seen.items() if isinstance(seen, dict) else []
                    )
                },
            }
    return out


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def derive_channel_id(pair_name: str, channel: str) -> str:
    """The channel UUID every member of `pair_name` derives for `channel`."""
    return str(uuid.uuid5(CHANNEL_UUID_NAMESPACE, f"{pair_name}/{channel}"))


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def load_channel_map(path: Path) -> dict[str, str]:
    """name -> channel UUID as persisted. `{}` when absent; an entry that is no UUID is dropped."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        name: value
        for name, value in document.items()
        if isinstance(name, str) and _is_uuid(value)
    }


def read_channel_state(path: Path) -> dict[str, Any] | None:
    """The daemon's recorded protocol health, or None when there is NO record at all.

    None and `{}` are different answers and callers must keep them apart: None means this
    file does not exist or could not be read, which the probe reports as
    `buzz-state-missing`; a dict means the daemon wrote something and its fields decide.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _write_private_json(path: Path, document: dict[str, Any]) -> bool:
    """The ONLY disk write in this module, atomic and `0600`. True when the bytes changed.

    Funnelled through one function on purpose. This module must never write a notifier MARKER
    (D-2's single-writer rule), and `tests/test_buzz_channel.py::TestSingleWriter` enforces
    that by scanning this source -- so every write being one named helper, whose callers pass
    only paths under the Buzz config directory, is what makes that control checkable instead of
    a guess about a symbol's name.

    `write_if_changed` is the notifier's own writer, REUSED rather than mirrored: it already
    does temp-file + `os.replace` + `fchmod(0600)` + `fsync`, and a second atomic-write
    primitive in this repo is a second one to get wrong.
    """
    from ai_chassis.agent_mail_notifier import write_if_changed

    return write_if_changed(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def load_pubkey_directory(path: Path) -> dict[str, dict[str, Any]]:
    """The directory, or `{}` when there is none to read.

    `{}` rather than a raise for an unreadable file: an empty directory makes `--to` REFUSE,
    which is the fail-closed answer. A raise here would instead take down the daemon tick that
    also serves agent mail.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        name: entry
        for name, entry in document.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


@dataclass(frozen=True)
class DirectoryUpdate:
    """What `record_pubkey` did, and why it refused when it refused. D-21.

    A bool was enough while the rule was last-writer-wins, because the only two answers were
    "wrote" and "stale". D-21 adds a REFUSAL that an operator has to be able to act on, and a
    `False` indistinguishable from "already current" is a redirect attempt nobody can see.
    """

    changed: bool
    #: "" when the announcement was accepted or merely stale; a classified reason on a refusal.
    reason: str = ""
    #: The pubkey holding the name, on a `name-already-bound` refusal.
    bound_pubkey: str = ""


def forgotten_path(path: Path) -> Path:
    """Where `forget` records each forgotten binding's `created_at`, beside the directory."""
    return path.with_name(path.stem + ".forgotten.json")


def _forgotten_watermark(path: Path, name: str) -> int | None:
    try:
        document = json.loads(forgotten_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = document.get(name) if isinstance(document, dict) else None
    return value if isinstance(value, int) else None


def _entry_pubkey(entry: dict[str, Any]) -> str:
    pubkey = entry.get("pubkey")
    return pubkey.strip() if isinstance(pubkey, str) else ""


def record_pubkey(  # noqa: PLR0913, PLR0917  # one keyword per binding input
    path: Path,
    name: str,
    pubkey: str,
    *,
    created_at: int,
    now: int,
    rotation: str = "",
) -> DirectoryUpdate:
    """Bind `name -> pubkey`, under D-21's first-binding-wins rule. D-16 for the ordering.

    THREE outcomes, in the order they are decided:

    1. The name is unbound -- it binds to this pubkey. First verified binding wins.
    2. The name is bound to a DIFFERENT pubkey -- REFUSED (`name-already-bound`) unless
       `rotation` carries an attestation co-signed by the bound key over this exact
       `(name, pubkey)` pair. A verified signature proves the advertised key signed its own
       claim; it proves nothing about who OWNS `<slug>@<machine>`, so last-writer-wins let
       any authenticated participant on the pair relay publish a newer `kind 0` for another
       session's name and silently redirect its DMs (audit Round 6 N-10).
    3. The name is bound to THIS pubkey, or a rotation authorised the rebind -- the entry
       updates only if this announcement is genuinely newer. Ordering is on the event's own
       `created_at`, not on arrival: a relay replays stored events in no guaranteed order, so
       keying on arrival would let a rotated-away key win on every reconnect.

    `created_at` is stored because the rule needs it; `seen_at` is this machine's own clock and
    exists for the operator reading the file.
    """
    if not name or not pubkey:
        return DirectoryUpdate(False)
    # A forgotten binding's own `kind 0` is still stored on the relay and is replayed on every
    # resubscribe; only an announcement NEWER than the one forgotten may bind again (ISSUE-REF).
    forgotten = _forgotten_watermark(path, name)
    if forgotten is not None and int(created_at) <= forgotten:
        return DirectoryUpdate(False)
    directory = load_pubkey_directory(path)
    existing = directory.get(name)
    if isinstance(existing, dict):
        bound = _entry_pubkey(existing)
        if (
            bound
            and bound != pubkey
            and not rotation_authorizes(
                rotation, name=name, new_pubkey=pubkey, bound_pubkey=bound
            )
        ):
            return DirectoryUpdate(False, NAME_ALREADY_BOUND_REASON, bound)
        stored = existing.get("created_at")
        if isinstance(stored, int) and stored >= created_at:
            return DirectoryUpdate(False)
    directory[name] = {
        "pubkey": pubkey,
        "seen_at": _iso_now(now),
        "created_at": int(created_at),
    }
    # Atomic, because a torn directory reads as empty and would turn every `--to` into a
    # refusal until the next `kind 0` arrived.
    return DirectoryUpdate(_write_private_json(path, directory))


def forget_pubkey(path: Path, name: str, *, now: int) -> int | None:
    """Drop `name`'s binding so only a `kind 0` NEWER than the forget may bind it. D-21.

    The operator's half of first-binding-wins: a session that LOST its key cannot rotate on
    its own, because it no longer holds the key an attestation needs. Returns the watermark
    recorded, or None when a write failed.

    The watermark is written for an UNBOUND name too, and reaches `now`, not just the stored
    `created_at`: the relay keeps a name's latest `kind 0` and replays it on every resubscribe,
    so a name absent here today, or one whose stored entry is older than the relay's copy,
    would otherwise bind again from that replay (ISSUE-REF, measured live 2026-10-12).
    """
    directory = load_pubkey_directory(path)
    entry = directory.pop(name, None)
    stored = entry.get("created_at") if isinstance(entry, dict) else None
    # On disk, not in memory: the replay arrives on a restarted daemon's first resubscribe.
    try:
        watermarks = json.loads(forgotten_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        watermarks = {}
    if not isinstance(watermarks, dict):
        watermarks = {}
    previous = watermarks.get(name)
    mark = max(
        int(now),
        stored if isinstance(stored, int) else 0,
        previous if isinstance(previous, int) else 0,
    )
    # The writer reports "bytes changed", so an unchanged mark is not a failed write.
    if previous != mark:
        watermarks[name] = mark
        if not _write_private_json(forgotten_path(path), watermarks):
            return None
    if entry is not None and not _write_private_json(path, directory):
        return None
    return mark


def identity_key_exists(directory: Path, name: str) -> bool:
    """Whether THIS machine holds a private key for `name`.

    Narrower than "that session is live", and named for what it measures. It is the guard
    `buzz identity forget` needs: forgetting a name this machine can still sign for makes a
    locally addressable session unaddressable until it republishes, and that is a mistake,
    not an operator decision.
    """
    return _key_path(directory, name).is_file()


def resolve_pubkey(path: Path, name: str) -> str:
    """The pubkey the directory holds for `name`, or "" -- which callers must treat as a refusal."""
    entry = load_pubkey_directory(path).get(name)
    if not isinstance(entry, dict):
        return ""
    return _entry_pubkey(entry)


def load_presence_records(path: Path) -> dict[str, dict[str, Any]]:
    """Peer presence records, or `{}` when there is none to read.

    `{}` rather than a raise for an unreadable file: an empty presence set means no peers are
    known to be live, which is the fail-closed answer. A raise here would instead take down the
    daemon tick that also serves agent mail.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        identity: entry
        for identity, entry in document.items()
        if isinstance(identity, str) and isinstance(entry, dict)
    }


def parse_presence_liveness(event: RelayEvent) -> str:
    """The liveness tri-state from a `kind 20001` presence event.

    Reads from the `status` tag first (OQ-7: whether the relay accepts an arbitrary status
    string is undocumented), falling back to `content` when the tag is absent. Returns
    `undetermined` when neither is a recognized liveness word.
    """
    for tag in event.tags:
        if len(tag) >= 2 and tag[0] == "status":  # noqa: PLR2004  # ["status", value]
            liveness = tag[1].strip()
            if liveness in {LIVENESS_LIVE, LIVENESS_IDLE, LIVENESS_UNDETERMINED}:
                return liveness
    content = event.content.strip()
    if content in {LIVENESS_LIVE, LIVENESS_IDLE, LIVENESS_UNDETERMINED}:
        return content
    return LIVENESS_UNDETERMINED


#: The optional heartbeat-record field naming the publisher's agent-mail board name (D-22 (a)).
AGENT_MAIL_NAME_KEY = "agent_mail_name"


def parse_presence_board_name(event: RelayEvent, identity: str) -> str | None:
    """The `agent_mail_name` a presence event's record carries for `identity`, or None.

    `identity` is the AUTHENTICATED one, from the pubkey directory. The content's own
    `identity` is never trusted to name the record; when it names a DIFFERENT identity the
    board name belongs to that other identity, so it is not re-attributed to the signer.
    Only a non-empty, non-blank string is a name: anything else is no name at all.
    """
    try:
        record = json.loads(event.content)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    claimed = record.get("identity")
    if claimed is not None and claimed != identity:
        return None
    name = record.get(AGENT_MAIL_NAME_KEY)
    if not isinstance(name, str) or not name.strip():
        return None
    return name


def parse_presence_change(event: RelayEvent, identity: str) -> dict[str, Any] | None:
    """Retain only signed, identity-bound lifecycle metadata from a presence event."""
    try:
        record = json.loads(event.content)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("identity") != identity:
        return None
    if record.get("source") != "presence-event" or record.get("state") not in {
        "live",
        "ended",
        "exited",
    }:
        return None
    return {
        key: record.get(key) for key in ("state", "session", "since", "pid_start_stamp")
    }


def parse_identity_parts(identity: str) -> tuple[str, str, str | None]:
    """The (identity, machine, slug) from a fleet identity name, or ("", "", None).

    The identity is `<slug>@<machine>` or `daemon@<machine>`. Returns empty strings when the
    name does not parse as a concrete fleet identity.
    """
    try:
        address = parse_address(identity)
    except AddressError:
        return "", "", None
    if WILDCARD in (address.slug, address.machine):
        return "", "", None
    # `daemon` is a sentinel, not a slug: it is the machine's own daemon key.
    if address.slug == DAEMON_SLUG:
        return identity, address.machine, None
    return identity, address.machine, address.slug


def record_presence(  # noqa: PLR0913, PLR0917  # presence record has many fields
    path: Path,
    identity: str,
    machine: str,
    slug: str | None,
    liveness: str,
    pubkey: str,
    *,
    generated_at: int,
    now: int,
    agent_mail_name: str | None = None,
    presence_event: dict[str, Any] | None = None,
) -> bool:
    """Record one peer's presence when this event is NEWER than the stored one. ISSUE-REF.

    True when the file changed. Last-writer-wins on the event's own `generated_at` (which is
    the `created_at` field of the presence event), not on arrival order: a relay replays stored
    events in no guaranteed order, so keying on arrival would let a stale presence overwrite
    the current one on every reconnect.

    `generated_at` is stored because the aging rule needs it; `seen_at` is this machine's own
    clock and exists for the operator reading the file. `agent_mail_name` (D-22 (a)) is stored
    only when it is a non-blank string, and the whole record is replaced, so a newer presence
    that carries no name leaves none behind.
    """
    if not identity or not pubkey:
        return False
    records = load_presence_records(path)
    existing = records.get(identity)
    if isinstance(existing, dict):
        stored = existing.get("generated_at")
        if isinstance(stored, int) and stored >= generated_at:
            return False
    entry: dict[str, Any] = {
        "identity": identity,
        "machine": machine,
        "slug": slug,
        "liveness": liveness,
        "pubkey": pubkey,
        "generated_at": int(generated_at),
        "seen_at": _iso_now(now),
    }
    if isinstance(agent_mail_name, str) and agent_mail_name.strip():
        entry[AGENT_MAIL_NAME_KEY] = agent_mail_name
    if presence_event is not None:
        entry["presence_event"] = presence_event
    records[identity] = entry
    # Atomic, because a torn presence file reads as empty and would hide every peer's liveness.
    return _write_private_json(path, records)


def build_presence_event(keys: Any, liveness: str, detail: str, *, now: int) -> Any:
    """A `kind 20001` presence event. The tri-state is in BOTH `content` and a `status` tag.

    Both, because OQ-7 is open: whether the relay accepts an arbitrary `status` string is
    undocumented, so a reader that can only see one of the two still gets the word.
    """
    return build_event(
        keys,
        KIND_PRESENCE,
        detail,
        [["status", liveness]],
        created_at=now,
    )


def build_addressed_event(
    keys: Any,
    kind: int,
    recipient_pubkey_hex: str,
    text: str,
    *,
    now: int,
) -> Any:
    """A `kind` event whose content is NIP-44 v2 encrypted to one recipient, `p`-tagged to it.

    Signed by `keys` -- the SENDER's own key, unlike a gift wrap's ephemeral one -- because the
    launch envelope's authentication is exactly "this key signed it" (issue-ref D-20, T1/T2).
    """
    sdk = nostr_sdk_module()
    content = sdk.nip44_encrypt(
        keys.secret_key(),
        sdk.PublicKey.parse(recipient_pubkey_hex),
        text,
        sdk.Nip44Version.V2,
    )
    return build_event(
        keys, kind, content, [["p", recipient_pubkey_hex]], created_at=now
    )


def build_launch_request_event(
    keys: Any, daemon_pubkey_hex: str, content: str, *, now: int
) -> Any:
    """A `KIND_LAUNCH_REQUEST` carrying `content` (the D-20 envelope JSON) to a daemon key."""
    return build_addressed_event(
        keys, KIND_LAUNCH_REQUEST, daemon_pubkey_hex, content, now=now
    )


def build_launch_result_event(  # noqa: PLR0913  # one keyword per reply field
    keys: Any,
    request_id: str | None,
    decision: str,
    reason: str,
    launched_identity: str | None,
    *,
    now: int,
    recipient_pubkey_hex: str,
    extra: dict[str, Any] | None = None,
) -> Any:
    """The `KIND_LAUNCH_RESULT` reply, encrypted to the key that SIGNED the request.

    The signer, never the envelope's `requester` field: replying to a name the request merely
    claimed would let a request reflect a reply onto a third party. `extra` carries a verb's
    own reply fields (an `inject-goal` verdict, ISSUE-REF); it cannot override the four here.
    """
    payload = json.dumps(
        {
            **(extra or {}),
            "request_id": request_id,
            "decision": decision,
            "reason": reason,
            "launched_identity": launched_identity,
        },
        sort_keys=True,
    )
    return build_addressed_event(
        keys, KIND_LAUNCH_RESULT, recipient_pubkey_hex, payload, now=now
    )


def open_addressed_event(recipient_keys: Any, event: RelayEvent) -> str | None:
    """The plaintext of an addressed event, or None when this key cannot decrypt it."""
    sdk = nostr_sdk_module()
    try:
        return str(
            sdk.nip44_decrypt(
                recipient_keys.secret_key(),
                sdk.PublicKey.parse(event.pubkey),
                event.content,
            )
        )
    except Exception:  # noqa: BLE001 - an undecryptable payload is not addressed to us
        return None


async def build_gift_wrap(
    sender_keys: Any,
    recipient_pubkey_hex: str,
    text: str,
    *,
    now: int,
) -> Any:
    """A NIP-59 gift wrap (`kind 1059`) whose `created_at` is NOW. D-13.

    MEASURED, and the reason this is hand-composed (nostr-sdk 0.45.1, 2026-10-08): the FFI's
    own `nip17_make_private_msg` backdates the wrap's `created_at` by a random 11-37 hours, per
    NIP-59's timestamp-tweak guidance. Buzz issue #4192 reports the relay REJECTING backdated
    wraps, so that helper cannot be used as-is and D-13 forbids the backdate outright. The seal
    is still built by the SDK (`nip59_make_seal`) -- its own randomised stamp is ENCRYPTED
    inside the wrap and never reaches the relay's clock check, so it is not a wire fact.

    The wrap is signed by a one-shot ephemeral key, as NIP-59 requires: the sender's identity
    lives in the sealed layer, and reusing the sender's key here would publish the social graph
    the gift wrap exists to hide.
    """
    sdk = nostr_sdk_module()
    rumor = (
        sdk.EventBuilder(sdk.Kind(KIND_PRIVATE_MESSAGE), text)
        .tags([sdk.Tag.parse(["p", recipient_pubkey_hex])])
        .custom_created_at(_timestamp(sdk, now))
        .finalize_unsigned(sender_keys.public_key())
    )
    recipient = sdk.PublicKey.parse(recipient_pubkey_hex)
    seal = await sdk.nip59_make_seal_async(sender_keys, recipient, rumor)
    ephemeral = sdk.Keys.generate()
    content = sdk.nip44_encrypt(
        ephemeral.secret_key(), recipient, seal.as_json(), sdk.Nip44Version.V2
    )
    return build_event(
        ephemeral,
        KIND_GIFT_WRAP,
        content,
        [["p", recipient_pubkey_hex]],
        created_at=now,
    )


async def unwrap_gift(recipient_keys: Any, event_json: str) -> tuple[str, str] | None:
    """`(sender_pubkey_hex, text)` from a `kind 1059` wrap, or None when it is not ours.

    None rather than a raise: a subscription sees every wrap addressed to the key, and one this
    key cannot decrypt is a routine event, not an error.
    """
    sdk = nostr_sdk_module()
    try:
        wrap = sdk.Event.from_json(event_json)
        unwrapped = await sdk.UnwrappedGift.from_gift_wrap_async(recipient_keys, wrap)
    except Exception:  # noqa: BLE001 - an undecryptable wrap is not addressed to us
        return None
    return unwrapped.sender().to_hex(), unwrapped.rumor().content()


def verify_event_json(event_json: str) -> bool:
    """Whether an event's id and Schnorr signature both check out.

    MEASURED against nostr-sdk 0.45.1 (2026-10-08), because the whole inbound-safety
    boundary rests on what this rejects: a pristine event verifies, and a tampered
    `content`, a tampered `id`, a tampered `sig` and a forged `pubkey` are each rejected.
    `Event.verify()` therefore recomputes the canonical id AND checks the signature, which
    is what T-2.3's sender allow-list needs to mean anything.
    """
    sdk = nostr_sdk_module()
    try:
        return bool(sdk.Event.from_json(event_json).verify())
    except Exception:  # noqa: BLE001 - malformed input is an invalid event, not a crash
        return False


#: Hex lengths NIP-01 fixes: a 32-byte id and pubkey, a 64-byte Schnorr signature.
HEX_ID_CHARS = 64
HEX_SIG_CHARS = 128
_HEX_RE = re.compile(r"\A[0-9a-f]+\Z")


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and bool(_HEX_RE.match(value.lower()))
    )


def _is_whole_number(value: object) -> bool:
    """A non-negative int, and NOT a bool -- `True` is an `int` and is not a kind."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_tag_list(value: object) -> bool:
    """NIP-01 tags: a list of lists of strings, and nothing else."""
    return isinstance(value, list) and all(
        isinstance(tag, list) and all(isinstance(part, str) for part in tag)
        for tag in value
    )


def validate_event_node(node: object) -> str:
    """ "" when `node` is a well-formed, AUTHENTIC event; a classified reason otherwise.

    T-2.3's allow-list and D-2's inbound-safety contract both key on the sender's pubkey, so
    they mean nothing unless the pubkey is proved to have signed the event. Before this, the
    client took the relay's `id`, `pubkey`, `content` and `tags` on trust and rendered them
    through the CC-session wake path -- a faulty or compromised relay could forge an
    allow-listed sender and bypass the hold rule entirely (audit F-30).

    Field TYPES are checked before the signature, and the reasons are classified words only:
    a rejection reason is logged, and it must never carry the pubkey or the content.
    """
    if not isinstance(node, dict):
        return "not-an-object"
    # A TABLE rather than a chain of returns: one row per NIP-01 field, in the order a
    # reader checks them, so adding a field is a row. Every predicate is total and cheap,
    # so evaluating them all costs nothing and cannot raise on a hostile shape.
    for reason, ok in (
        ("bad-id-field", _is_hex(node.get("id"), HEX_ID_CHARS)),
        ("bad-pubkey-field", _is_hex(node.get("pubkey"), HEX_ID_CHARS)),
        ("bad-sig-field", _is_hex(node.get("sig"), HEX_SIG_CHARS)),
        ("bad-kind-field", _is_whole_number(node.get("kind"))),
        ("bad-created-at-field", _is_whole_number(node.get("created_at"))),
        ("bad-content-field", isinstance(node.get("content"), str)),
        ("bad-tags-field", _is_tag_list(node.get("tags"))),
    ):
        if not ok:
            return reason
    try:
        authentic = verify_event_json(json.dumps(node))
    except BuzzError:
        # The SDK is absent. Unverifiable is not authentic: a rung that cannot check a
        # signature must not deliver, because the allow-list would be unenforceable.
        return "unverifiable (nostr-sdk missing)"
    return "" if authentic else "id-or-signature-invalid"


def event_message_id(event_id_hex: str) -> int:
    """A stable positive int for `Message.message_id`, derived from the event id. T-2.3.

    Stable is the whole requirement: `notice.id` is a digest of the rendered notice, so the
    SAME event has to render the same integer on every tick or a redelivered event would move
    the id and wake every session again. Derived by digest rather than by truncating the id's
    own hex so the value does not depend on which end of a hex string the relay varies.
    """
    digest = hashlib.sha256(event_id_hex.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") >> (256 - EVENT_ID_BITS)


# ---------------------------------------------------------------------------------------------
# Wire layer
# ---------------------------------------------------------------------------------------------


@dataclass
class RelayEvent:
    """One event this client received, in the shape the delivery adapter consumes."""

    event_id: str
    pubkey: str
    kind: int
    content: str
    created_at: int
    tags: list[list[str]]
    #: Kept so a gift wrap can be handed back to the SDK verbatim: `UnwrappedGift` verifies the
    #: seal, and an event reassembled without its signature fails that check rather than
    #: decrypting -- which would read as "not addressed to us" and drop every DM silently.
    signature: str = ""
    #: `fleet`, `m-<machine>`, or the session slug a DM was decrypted for.
    channel: str = ""
    #: Set for a decrypted gift wrap: the SEALED sender, not the wrap's ephemeral pubkey.
    sender_pubkey: str = ""
    #: N-09: marks an event that was rejected after initial filing (e.g., oversized after decrypt).
    rejected: bool = False

    @property
    def h_tag(self) -> str:
        for tag in self.tags:
            if len(tag) >= 2 and tag[0] == "h":  # noqa: PLR2004  # ["h", value]
                return tag[1]
        return ""

    @property
    def p_tags(self) -> list[str]:
        return [tag[1] for tag in self.tags if len(tag) >= 2 and tag[0] == "p"]  # noqa: PLR2004  # ["p", value]

    @property
    def author_pubkey(self) -> str:
        """Who actually sent this: the sealed sender for a DM, the signer otherwise."""
        return self.sender_pubkey or self.pubkey


@dataclass
class PublishResult:
    """A relay `["OK", <id>, <bool>, <message>]`, read back rather than assumed."""

    event_id: str
    accepted: bool
    message: str = ""


@dataclass
class RelayCounters:
    """REQs sent and quiet reads made, shared by every socket of one channel (T-6.4).

    One object per channel, reset in place at the start of each pass, so a socket replaced by a
    reconnect mid-pass still counts towards the pass it served.
    """

    reqs_sent: int = 0
    quiet_reads: int = 0

    def reset(self) -> None:
        self.reqs_sent = 0
        self.quiet_reads = 0


def _filters_key(filters: list[dict[str, Any]]) -> str:
    """The subscription a REQ asked for, `since` aside: `since` moves every pass, the ask does not."""
    return json.dumps(
        [
            {key: value for key, value in entry.items() if key != "since"}
            for entry in filters
        ],
        sort_keys=True,
    )


def _parse_frame(raw: Any) -> list[Any] | None:
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return frame if isinstance(frame, list) and frame else None


#: websockets' `State` names in which a socket can still carry frames.
_OPEN_STATES = frozenset({"CONNECTING", "OPEN"})


@dataclass
class RelayConnection:
    """One WebSocket to one relay, with the NIP-42 handshake and `OK` reads explicit.

    `connect_factory` is the seam the fake relay is injected through; it defaults to
    `websockets.asyncio.client.connect`, so no test needs the real library's network path and
    nothing here reaches a network when a factory was supplied.

    Two kinds of read, never mixed (issue-ref D-26). `pump` files what the relay has ALREADY
    sent and returns the moment nothing is buffered. A read for an ANSWER this client asked for
    (the AUTH challenge, a REQ's EOSE/CLOSED, a publish's OK) waits for it, bounded by
    `read_timeout_s`. Every frame either kind reads goes through `_dispatch`, and so does every
    frame `BuzzChannel.readable` reads.
    """

    relay_url: str
    keys: Any
    #: Where the socket lands when that is not `relay_url`'s authority (ISSUE-REF).
    dial: tuple[str, int] | None = None
    clock: Any = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    ok_timeout_s: float = DEFAULT_OK_TIMEOUT_S
    connect_factory: Any = None
    max_bytes: int = DEFAULT_MAX_BYTES
    max_wire_bytes: int = DEFAULT_MAX_WIRE_BYTES
    #: Challenges answered, newest last. The fake relay asserts against this; `status()` does not
    #: expose it, because a challenge is single-use and publishing one teaches nothing.
    challenges: list[str] = field(default_factory=list)
    authenticated: bool = False
    #: The relay's own `OK false` for our `kind 22242`, when it sent one (audit F-28). NIP-42
    #: has the relay answer the AUTH event like any other; nothing read that answer before, so
    #: an AUTH the relay REFUSED was indistinguishable from one it accepted.
    auth_error: str = ""
    #: Subscription ids the relay sent `CLOSED` for. A closed subscription is a silent
    #: non-delivery: the socket is up, the client is authenticated, and nothing arrives.
    closed_subscriptions: list[str] = field(default_factory=list)
    #: sub_id -> the relay's CLOSED reason, sanitised by :func:`relay_reason`. The reason is
    #: the diagnosis: the pinned relay names the exact gate that refused (ISSUE-REF).
    closed_reasons: dict[str, str] = field(default_factory=dict)
    #: Events refused by :func:`validate_event_node` before reaching delivery (F-30), and the
    #: distinct classified reasons seen. Reasons only -- never a pubkey, never any content.
    rejected: int = 0
    rejections: list[str] = field(default_factory=list)
    #: How many of `rejected` the channel has already logged, so a tick reports each refusal
    #: once without the channel having to subtract one connection's count from another's.
    rejections_reported: int = 0
    #: Whether this socket's last REQ is live on the relay (not CLOSED), and the last reason
    #: it was not: the per-identity state `status()` reports (ISSUE-REF).
    subscribed: bool = False
    last_error: str = ""
    #: The channel's per-pass counters (T-6.4); a private one when used alone.
    counters: RelayCounters = field(default_factory=RelayCounters)
    #: Monotonic clock for the D-25 lease; `time.monotonic` when None.
    monotonic: Any = None
    #: sub_id -> monotonic time its last REQ was SENT, and the subscription it asked for. A
    #: sub_id absent here has never been REQ'd on THIS socket, which is what a reconnect means.
    req_at: dict[str, float] = field(default_factory=dict)
    req_filters: dict[str, str] = field(default_factory=dict)
    #: sub_ids whose stored events have ended (`EOSE`) since their last REQ.
    eosed: set[str] = field(default_factory=set)
    #: sub_ids whose CLOSED the channel has already logged, so a re-REQ does not log it twice.
    closed_reported: set[str] = field(default_factory=set)
    _auth_event_id: str = ""
    _socket: Any = None
    #: Set when a read raised: the socket is dead even though the object is still held, so
    #: `publish` stops waiting for an `OK` that can no longer arrive (ISSUE-REF).
    _lost: bool = False
    _pending: list[RelayEvent] = field(default_factory=list)
    _ok: dict[str, PublishResult] = field(default_factory=dict)

    def _now(self) -> int:
        if self.clock is not None:
            return int(self.clock())
        return int(dt.datetime.now(dt.timezone.utc).timestamp())

    def _monotonic(self) -> float:
        return float((self.monotonic or time.monotonic)())

    async def open(self) -> None:
        """Dial the relay and settle the handshake. Raises :class:`BuzzError` on failure.

        The relay sends `["AUTH", <challenge>]` immediately on connect, so the handshake is
        settled by waiting (bounded) for that challenge and answering it before anything is
        subscribed or published. A relay needing no AUTH costs the bound once per dial.
        """
        factory = self.connect_factory or websockets_connect()
        try:
            self._socket = await asyncio.wait_for(
                factory(self.relay_url, **dial_kwargs(self.dial)),
                timeout=self.timeout_s,
            )
        except BuzzError:
            raise
        except (OSError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - every dial failure is one reason word
            raise BuzzError(f"relay-unreachable ({type(exc).__name__})") from exc
        self._lost = False
        self.subscribed = False
        self.req_at.clear()
        self.req_filters.clear()
        self.eosed.clear()
        await self._read_until(lambda: bool(self.challenges), self.read_timeout_s)

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        self.authenticated = False
        self._lost = False
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.close()

    @property
    def connected(self) -> bool:
        """Whether this socket can still carry frames: dialled, not lost, not closing."""
        if self._socket is None or self._lost:
            return False
        state = getattr(self._socket, "state", None)
        return state is None or getattr(state, "name", "OPEN") in _OPEN_STATES

    def _lose(self, exc: BaseException) -> None:
        """A read raised: the socket is gone. Recorded once, here, for every read path."""
        self._lost = True
        self.authenticated = False
        self.subscribed = False
        self.last_error = f"relay-recv-failed ({type(exc).__name__})"

    async def _send(self, message: list[Any]) -> None:
        if self._socket is None:
            raise BuzzError("relay-not-connected")
        try:
            await asyncio.wait_for(
                self._socket.send(json.dumps(message)), timeout=self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - a dead socket is one reason word
            reason = f"relay-send-failed ({type(exc).__name__})"
            # A failed send is a dead socket, so the next pass redials it (ISSUE-REF).
            self._lost = True
            self.authenticated = False
            self.subscribed = False
            self.last_error = reason
            raise BuzzError(reason) from exc

    async def receive(self) -> Any:
        """One raw frame, waiting as long as it takes. Raises what the socket raises.

        `BuzzChannel.readable` owns the only caller: it bounds the wait itself, and a frame read
        here must go to `dispatch` exactly once.
        """
        if self._socket is None:
            raise BuzzError("relay-not-connected")
        return await self._socket.recv()

    async def _recv(self, wait_s: float) -> Any:
        """One raw frame, or None when none arrived within `wait_s` or the socket went away.

        `wait_s == 0` is the non-blocking read (D-26): `asyncio.timeout(0)` cancels the read on
        the loop's next turn, so a buffered frame is returned at once and a quiet socket costs one
        turn. Cancelling `recv()` is safe in websockets 15: a frame not yet returned stays queued.
        """
        if not self.connected:
            return None
        try:
            async with asyncio.timeout(wait_s):
                return await self._socket.recv()
        except TimeoutError:
            if not wait_s:
                self.counters.quiet_reads += 1
            return None
        except Exception as exc:  # noqa: BLE001 - a closed socket is one reason word
            self._lose(exc)
            return None

    async def _read_until(self, done: Callable[[], bool], timeout_s: float) -> None:
        """Read and dispatch frames until `done()` holds, the socket goes away, or `timeout_s`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            raw = await self._recv(remaining)
            if raw is None:
                return
            await self.dispatch(raw)

    async def _answer_auth(self, challenge: str) -> None:
        """Sign the challenge and send `["AUTH", <event>]`. Resubscription is the caller's.

        `authenticated` is set OPTIMISTICALLY, and stays that way for a relay that answers
        nothing: a NIP-01 relay needing no AUTH is legitimate, so silence must not make the
        rung unavailable. An explicit `OK false` for this event id is what demotes it, and
        `_auth_event_id` is remembered here so the pump can recognise that answer.
        """
        self.challenges.append(challenge)
        event = build_auth_event(self.keys, self.relay_url, challenge, now=self._now())
        document = json.loads(event.as_json())
        self._auth_event_id = str(document.get("id", ""))
        await self._send(["AUTH", document])
        self.authenticated = True
        self.auth_error = ""

    async def pump(self, *, limit: int = 64) -> None:
        """File what the relay has ALREADY sent, answering `AUTH` and filing `EVENT`/`OK`.

        Never waits on a quiet socket (D-26): the daemon's one wait is `BuzzChannel.readable`,
        between passes. Bounded by `limit` rather than draining to quiet: this runs inside the
        daemon's pass, and a chatty relay must not be able to hold it. Stops after a `CLOSED`, as
        it always has, so the caller sees the refusal before anything else is read.
        """
        for _ in range(limit):
            raw = await self._recv(0)
            if raw is None:
                return
            if not await self.dispatch(raw):
                return

    async def dispatch(self, raw: Any) -> bool:
        """File ONE raw frame: the single dispatcher behind `pump`, the bounded reads and
        `BuzzChannel.readable`. False after a `CLOSED`. A malformed frame is dropped."""
        frame = _parse_frame(raw)
        if frame is None:
            return True
        verb = frame[0]
        if verb == "AUTH" and len(frame) >= 2:  # noqa: PLR2004  # ["AUTH", challenge]
            await self._answer_auth(str(frame[1]))
        elif verb == "EVENT" and len(frame) >= 3:  # noqa: PLR2004  # ["EVENT", sub, event]
            self._file_event(frame[2])
        elif verb == "OK" and len(frame) >= 3:  # noqa: PLR2004  # ["OK", id, bool, msg]
            event_id = str(frame[1])
            result = PublishResult(
                event_id=event_id,
                accepted=bool(frame[2]),
                message=str(frame[3]) if len(frame) > 3 else "",  # noqa: PLR2004  # optional 4th member
            )
            if event_id and event_id == self._auth_event_id:
                # NIP-42's answer to our AUTH, read rather than assumed (F-28).
                self.authenticated = result.accepted
                self.auth_error = (
                    "" if result.accepted else (result.message or "auth-refused")
                )
                return True
            self._ok[event_id] = result
        elif verb == "EOSE" and len(frame) >= 2:  # noqa: PLR2004  # ["EOSE", sub_id]
            self.eosed.add(str(frame[1]))
        elif verb == "CLOSED":
            if len(frame) >= 2:  # noqa: PLR2004  # ["CLOSED", sub_id, msg]
                self.closed_subscriptions.append(str(frame[1]))
                self.closed_reasons[str(frame[1])] = relay_reason(
                    frame[2] if len(frame) > 2 else ""  # noqa: PLR2004  # optional msg
                )
            return False
        return True

    def _file_event(self, node: Any) -> None:
        """File one received event, AFTER proving it authentic and within size cap (T-5.4). F-30, N-09.

        The verification is here, in the one place every inbound event passes through, so no
        future caller can reach `_pending` without it. N-09: wire size checked first for ALL
        events; plaintext size checked for kind-9 here and for kind-1059 after unwrapping.
        """
        reason = validate_event_node(node)
        if reason:
            self.rejected += 1
            if reason not in self.rejections:
                self.rejections.append(reason)
            return
        content = str(node.get("content", ""))
        wire_size = len(content.encode("utf-8"))
        # N-09: wire-size cap for malicious oversized payloads, checked before decryption
        if wire_size > self.max_wire_bytes:
            self.rejected += 1
            reason = "oversized-wire"
            if reason not in self.rejections:
                self.rejections.append(reason)
            return
        kind = int(node.get("kind", 0))
        # Plaintext cap for kind-9 (already plaintext); kind-1059 checked after unwrapping
        if kind == KIND_CHAT and wire_size > self.max_bytes:
            self.rejected += 1
            reason = "oversized"
            if reason not in self.rejections:
                self.rejections.append(reason)
            return
        self._pending.append(
            RelayEvent(
                event_id=str(node.get("id", "")),
                pubkey=str(node.get("pubkey", "")),
                kind=kind,
                content=content,
                created_at=int(node.get("created_at", 0)),
                signature=str(node.get("sig", "")),
                tags=[
                    [str(part) for part in tag]
                    for tag in node.get("tags", [])
                    if isinstance(tag, list)
                ],
            )
        )

    def take_events(self) -> list[RelayEvent]:
        events, self._pending = self._pending, []
        return events

    def subscription_due(
        self, sub_id: str, filters: list[dict[str, Any]], lease_s: float
    ) -> str:
        """Why a REQ for `sub_id` is owed on this socket, or "" while it is live (D-25).

        Owed after `open()` (never REQ'd on this socket), after a `CLOSED`, when the filters it
        must carry changed, or when its lease ran out. Nothing the socket RECEIVES extends a
        lease: a relay that dropped the subscription silently still answers presence with `OK`.
        """
        if sub_id not in self.req_at:
            return DUE_OPEN
        if sub_id in self.closed_subscriptions:
            return DUE_CLOSED
        if self.req_filters.get(sub_id) != _filters_key(filters):
            return DUE_FILTERS
        if self._monotonic() - self.req_at[sub_id] >= lease_s:
            return DUE_LEASE
        return ""

    async def subscribe(self, sub_id: str, filters: list[dict[str, Any]]) -> None:
        """Send one REQ, start its lease, and read the relay's answer: stored events, then
        `EOSE` or `CLOSED`, bounded by `read_timeout_s`. Raises :class:`BuzzError` on a dead send.
        """
        # A REQ replaces the subscription of the same id, so a CLOSED for the old one says
        # nothing about the new one: without this, one refusal read as closed forever.
        self.closed_subscriptions = [
            closed for closed in self.closed_subscriptions if closed != sub_id
        ]
        self.closed_reasons.pop(sub_id, None)
        self.closed_reported.discard(sub_id)
        self.eosed.discard(sub_id)
        await self._send(["REQ", sub_id, *filters])
        self.counters.reqs_sent += 1
        # The lease starts once the REQ is SENT, never when it was merely due.
        self.req_at[sub_id] = self._monotonic()
        self.req_filters[sub_id] = _filters_key(filters)
        await self._read_until(
            lambda: sub_id in self.eosed or sub_id in self.closed_subscriptions,
            self.read_timeout_s,
        )

    async def unsubscribe(self, sub_id: str) -> None:
        await self._send(["CLOSE", sub_id])

    async def publish(self, event: Any) -> PublishResult:
        """Send one `EVENT` and WAIT for its `OK`. An absent `OK` is not success.

        The design's DM fallback (D-13) turns on `OK false`, so an optimistic publish that
        never read the result would make that fallback dead code -- and a dropped message look
        like a delivered one.

        The wait is bounded by `ok_timeout_s`, not by the socket going quiet: a relay still
        processing the event is silent, and that silence is not an answer (ISSUE-REF).
        """
        document = json.loads(event.as_json())
        event_id = str(document["id"])
        await self._send(["EVENT", document])
        # D-26 reads: wait on the socket itself up to the deadline (no polling of a
        # non-blocking pump), and stop early when the socket is lost.
        await self._read_until(lambda: event_id in self._ok, self.ok_timeout_s)
        if event_id in self._ok:
            return self._ok.pop(event_id)
        return PublishResult(event_id=event_id, accepted=False, message=NO_OK_REASON)


#: Anything shaped like key material in relay-supplied text: a 64+ hex run (a pubkey, an
#: event id, a raw secret) or a NIP-19 bech32 entity.
_KEY_MATERIAL_RE = re.compile(
    r"[0-9a-fA-F]{64,}|\bn(?:sec|pub|profile|event|addr|ncryptsec)1[02-9ac-hj-np-z]{6,}"
)
RELAY_REASON_MAX_CHARS = 200


def relay_reason(text: Any) -> str:
    """A relay-supplied reason, safe to log: one line, printable, no key material, capped."""
    flat = " ".join(str(text).split())
    flat = "".join(ch for ch in flat if ch.isprintable())
    flat = " ".join(_KEY_MATERIAL_RE.sub("<redacted>", flat).split())
    return flat[:RELAY_REASON_MAX_CHARS]


def next_backoff(current: float) -> float:
    """The capped exponential schedule a reconnect follows. T-2.2."""
    if current <= 0:
        return BACKOFF_INITIAL_S
    return min(current * BACKOFF_FACTOR, BACKOFF_CAP_S)


# ---------------------------------------------------------------------------------------------
# Subscription filters
# ---------------------------------------------------------------------------------------------


def subscription_filters(  # noqa: PLR0913  # one keyword per subscribed event family
    *,
    channels: list[str],
    dm_pubkeys: list[str],
    since: int | None = None,
    metadata: bool = False,
    presence: bool = False,
    launch_pubkeys: list[str] | None = None,
    result_pubkeys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The filters the daemon subscribes with: group chat, gift wraps to our keys, `kind 0`.

    Separate filters rather than one: a relay ANDs the members of a single filter, so folding
    `#h` and `#p` together would ask for events that are both a group message and addressed to
    a key -- which is neither of the two things being subscribed to.

    The `kind 0` filter (D-16) carries NO `since`, deliberately and unlike the other two: a
    profile is a replaceable event published once when a session connects, so a `since` bound
    would hide every identity that announced itself before this daemon started and leave the
    name directory empty for exactly the peers most likely to be addressed. It also needs no
    `#p`/`#h` narrowing, because a pair's relay only carries that pair's identities.

    The `kind 20001` presence filter (Phase 3, ISSUE-REF) also carries no `since`: the relay
    leases presence for 90 s (measured per Buzz #3795), so a `since` bound would hide every
    peer's current presence that was published before the daemon started, and the reader would
    see only this machine's own identities -- which defeats the cross-machine liveness check.

    `launch_pubkeys` (issue-ref D-20) adds a `KIND_LAUNCH_REQUEST` filter addressed to those
    keys -- the daemon's, and only when `[remote_launch].enabled` is true. Absent or empty, the
    filter list is exactly what it was before remote launch existed, so an OFF machine asks the
    relay for nothing new.

    `result_pubkeys` (ISSUE-REF) adds a `KIND_LAUNCH_RESULT` filter addressed to this
    machine's session keys, the keys a request is signed with and so the keys its reply is
    encrypted to; the notifier passes them only once this machine has sent a request.
    """
    filters: list[dict[str, Any]] = []
    if channels:
        chat: dict[str, Any] = {"kinds": [KIND_CHAT], "#h": list(channels)}
        if since is not None:
            chat["since"] = int(since)
        filters.append(chat)
    if dm_pubkeys:
        wraps: dict[str, Any] = {"kinds": [KIND_GIFT_WRAP], "#p": list(dm_pubkeys)}
        if since is not None:
            wraps["since"] = int(since)
        filters.append(wraps)
    if metadata:
        filters.append({"kinds": [KIND_METADATA]})
    if presence:
        filters.append({"kinds": [KIND_PRESENCE]})
    if launch_pubkeys:
        filters.append({"kinds": [KIND_LAUNCH_REQUEST], "#p": list(launch_pubkeys)})
    if result_pubkeys:
        filters.append({"kinds": [KIND_LAUNCH_RESULT], "#p": list(result_pubkeys)})
    return filters


# ---------------------------------------------------------------------------------------------
# T-2.3 Delivery adapter
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchInbound:
    """One launch request as the notifier receives it: who signed it, and what it said.

    `sender_name` is the kind-0 directory's name for the SIGNING key, or "" when the directory
    has none -- which `remote_launch` refuses as `sender-not-allowed`.
    """

    event_id: str
    sender_pubkey: str
    sender_name: str
    content: str


@dataclass
class BuzzDelivery:
    """One poll's worth of Buzz traffic, sorted into what the notifier's renderers consume."""

    #: slug -> the messages that session must see.
    per_slug: dict[str, list[Any]] = field(default_factory=dict)
    #: Messages addressed to the machine rather than to one session.
    machine: list[Any] = field(default_factory=list)
    #: Notices withheld under the bypass-permissions rule, as records for `held/`.
    held: list[dict[str, Any]] = field(default_factory=list)
    #: Held-record keys released into this delivery. The notifier acknowledges them only after
    #: the marker transaction succeeds, so a crash cannot turn a pending notice into data loss.
    released_held: list[str] = field(default_factory=list)
    #: `p`-tagged events naming a key this machine holds none of. COUNTED rather than dropped
    #: silently, and never delivered: an addressed message is for its addressee alone, so the
    #: broadcast path must stay unreachable for it (audit F-26).
    unaddressed: int = 0
    #: Verified, decrypted `KIND_LAUNCH_REQUEST`s addressed to the daemon key (issue-ref D-20).
    #: Never a message: the notifier hands each to `remote_launch.handle_event`, and nothing
    #: here reaches a notice.
    launch_requests: list[LaunchInbound] = field(default_factory=list)
    #: Verified, decrypted `KIND_LAUNCH_RESULT`s addressed to one of this machine's session
    #: keys (ISSUE-REF): replies to requests sent from here. Never a message either.
    launch_results: list[LaunchInbound] = field(default_factory=list)
    #: ISSUE-REF (D-18 (d)): DMs to `attribution_recipient` whose text is an
    #: `aih-attribution/v1` launch record. Never a message: the notifier admits each through
    #: `attribution_buzz.admit`, the peer ledger's own D-7 path, and nothing reaches a notice.
    attribution_records: list[LaunchInbound] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(messages) for messages in self.per_slug.values()) + len(
            self.machine
        )


def _iso_now(when: int) -> str:
    return dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _message(
    event: RelayEvent, sender_label: str, now: int, message_class: str = ""
) -> Any:
    """One `agent_mail_notifier.Message` for a Buzz event.

    The `Message` type is REUSED rather than mirrored so the existing renderers, the escalation
    watchdog and `notice.id` all treat a Buzz message exactly as they treat board mail -- the
    "one daemon, one marker contract" half of D-2 is this import. Imported inside the function
    because the notifier imports THIS module (lazily, from its own `_buzz_channel` seam), and a
    module-level import in both directions is a cycle.

    `message_class` is passed by `buzz_delivery` from the addressing branch it took (D-13/D-18),
    never re-derived here from the event's content: the discrimination already exists as control
    flow in that function, and this argument is what stops it being discarded before the
    notifier -- a dropped class is what makes a DM and a broadcast render identically. "" means
    the caller has no class to state (the `held/` record, which is not a delivery) and falls back
    to the notifier's `CLASS_BROADCAST`, the LESS privileged of the two.
    """
    from ai_chassis.agent_mail_notifier import (
        CLASS_BROADCAST,
        RUNG_BUZZ,
        Message,
        _subject,
    )

    first_line = event.content.splitlines()[0] if event.content else ""
    return Message(
        message_id=event_message_id(event.event_id),
        sender=_subject(sender_label),
        subject=_subject(first_line),
        age_s=max(0, now - event.created_at),
        ack_required=False,
        message_class=message_class or CLASS_BROADCAST,
        rung=RUNG_BUZZ,
        body=event.content,
    )


def held_record_message(
    record: dict[str, Any], pubkey_names: dict[str, str], now: int
) -> Any:
    """The `Message` a `held/` record stands for, for its release (ISSUE-REF).

    Built from the record alone: the event was marked delivered when it was held (ISSUE-REF), so
    the relay will not hand it over again. A record written before the class was kept falls back
    to the less privileged `CLASS_BROADCAST`, as `_message` does.
    """
    from ai_chassis.agent_mail_notifier import (
        CLASS_BROADCAST,
        RUNG_BUZZ,
        Message,
        _subject,
    )

    sender = str(record.get("sender_pubkey") or "")
    label = pubkey_names.get(sender) or (
        f"buzz:{sender[:12]}" if sender else "buzz:unknown"
    )
    try:
        held_at = dt.datetime.strptime(
            str(record.get("held_at") or ""), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
        age = now - int(held_at.timestamp())
    except ValueError:
        age = 0
    return Message(
        message_id=event_message_id(str(record.get("event_id") or "")),
        sender=_subject(label),
        subject=_subject(str(record.get("subject") or "")),
        age_s=max(0, age),
        ack_required=False,
        message_class=str(record.get("message_class") or CLASS_BROADCAST),
        rung=RUNG_BUZZ,
        body=str(record.get("body") or ""),
    )


def _sender_label(event: RelayEvent, pubkey_names: dict[str, str]) -> str:
    """A display name when we know the pubkey, else a short prefix. Never the full key."""
    pubkey = event.author_pubkey
    known = pubkey_names.get(pubkey)
    if known:
        return known
    return f"buzz:{pubkey[:12]}" if pubkey else "buzz:unknown"


#: The flag that puts a Claude Code session in a bypass-permissions class. `claude_sessions`
#: documents it in the argv it reads (`--dangerously-skip-permissions`), and that argv is the
#: ONLY place this machine records it.
BYPASS_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
_BYPASS_PATTERN = re.compile(re.escape(BYPASS_PERMISSIONS_FLAG))


#: T-2.3's permission classification, per slug. TRI-STATE, because "we could not tell" is a
#: third answer and collapsing it into either of the other two is a safety decision in
#: disguise (audit F-31).
PERMISSION_BYPASS = "bypass"
PERMISSION_NORMAL = "normal"
PERMISSION_UNKNOWN = "unknown"


def _read_launch_argv(pid: int, *, proc_root: Path) -> str | None:
    """Read only this session's argv; never log it or fall back after a procfs PID race."""
    if proc_root.exists():
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
        return raw.decode("utf-8", "replace").replace("\0", " ")
    result = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=5,
    )
    # ps can succeed without returning a command. That is no evidence of normal mode.
    return result.stdout.decode("utf-8", "replace").strip() or None


def permission_classes(
    sessions: list[Any] | None,
    *,
    proc_root: Path | None = None,
    argv_reader: Callable[[int], str | None] | None = None,
) -> dict[str, str]:
    """slug -> `bypass` | `normal` | `unknown`. T-2.3's `If` clause, failing CLOSED.

    `normal` is asserted ONLY when the launch argv was actually READ and carries no bypass
    flag. When procfs is absent, read that session's command through ps instead. Anything
    that prevented the read -- an unreadable or absent `/proc/<pid>/cmdline`, a failed ps
    call, a race as the session exits -- is `unknown`, and the caller holds for `unknown`
    exactly as it holds for `bypass`. `argv_reader` injects the same evidence boundary.

    This INVERTS the earlier behaviour, which skipped a pid it could not read and thereby
    reported it as not bypassing. T-2.3 and the tracked `[buzz_notify]` comments require an
    unallowlisted sender's notice to be HELD when the receiving session is in a
    bypass-permissions class, and treating unreadable classification data as "not bypassing"
    weakened that boundary: a transient procfs race, a platform difference or a broken
    liveness adapter would deliver an unvetted message into a session that cannot ask for
    permission (audit F-31).

    The argv is the ONLY evidence this machine has. `claude_sessions.Session` is
    `(pid, name, cwd, started_at_ms, registered, liveness_proof)` -- there is no permission
    field anywhere in the session surface, and `registered` records only that a validated
    registry entry supplied the METADATA, which says nothing about permission mode. So no
    session-registry record can currently prove `normal`; if one is ever added, this is the
    one function that has to learn about it.
    """
    root = Path("/proc") if proc_root is None else proc_root
    out: dict[str, str] = {}
    for session in sessions or []:
        name = getattr(session, "name", None)
        pid = getattr(session, "pid", None)
        if not name:
            # An unnamed session is not addressable by any slug, so there is no recipient to
            # classify. Skipped rather than recorded as `unknown`, which would invent a key.
            continue
        slug = str(name)
        if pid is None:
            out[slug] = PERMISSION_UNKNOWN
            continue
        try:
            argv = (
                argv_reader(pid)
                if argv_reader is not None
                else _read_launch_argv(pid, proc_root=root)
            )
        except Exception:  # No failed argv probe can establish normal permission mode.
            out[slug] = PERMISSION_UNKNOWN
            continue
        if argv is None:
            out[slug] = PERMISSION_UNKNOWN
            continue
        out[slug] = (
            PERMISSION_BYPASS if _BYPASS_PATTERN.search(argv) else PERMISSION_NORMAL
        )
    return out


@functools.cache
def _allow_sender_entry(entry: str) -> tuple[str, str | Address]:
    """Classify one allow-list entry, logging a malformed value only once."""
    if _is_hex(entry, HEX_ID_CHARS):
        return "pubkey", entry
    try:
        address = parse_address(entry) if "@" in entry else None
    except AddressError:
        address = None
    if address is not None:
        return "address", address
    logging.getLogger(__name__).warning(
        "invalid allow_senders entry ignored: %r", entry
    )
    return "invalid", entry


def _sender_is_allowed(sender: str, allow_senders: frozenset[str]) -> bool:
    """Match pubkeys exactly and addresses through git-inbox recipient semantics."""
    try:
        sender_address = parse_address(sender)
    except AddressError:
        sender_address = None

    for entry in allow_senders:
        kind, value = _allow_sender_entry(entry)
        if kind == "pubkey":
            if sender_address is None and sender == value:
                return True
            continue
        if kind != "address" or sender_address is None:
            continue
        # Imported at call time because git_inbox imports fleet_liveness, which imports this
        # module's liveness vocabulary. Delivery runs only after all three modules are loaded.
        from ai_chassis.git_inbox import match_recipients

        matched = match_recipients(
            (value,),
            machine=sender_address.machine,
            live=lambda: (sender_address.slug,),
        )
        if sender_address.slug in matched.classes():
            return True
    return False


def is_held(
    slug: str,
    classes: dict[str, str] | None,
    sender: str,
    allow_senders: frozenset[str],
) -> bool:
    """Whether `slug`'s notice must be withheld. T-2.3's hold rule, in one place.

    Held for `bypass` AND for `unknown`, unless the sender is allow-listed. A slug ABSENT
    from a classification that ran is `unknown` too -- the probe enumerated the live
    sessions, so a slug it did not mention is one it could not classify.

    `classes is None` means NO classification was performed at all (no probe is wired), which
    is a configuration state rather than a probe failure: holding every notice on such a
    machine would turn a missing seam into fleet-wide silent non-delivery. A probe that RAN
    and failed passes `{}` instead, and every slug is then `unknown` and held.
    """
    if classes is None:
        return False
    if _sender_is_allowed(sender, allow_senders):
        return False
    return classes.get(slug, PERMISSION_UNKNOWN) != PERMISSION_NORMAL


def buzz_delivery(  # noqa: C901, PLR0912, PLR0913  # one branch per addressing mode; every input injected
    events: list[RelayEvent],
    *,
    live_slugs: set[str] | None,
    slug_pubkeys: dict[str, str],
    pubkey_names: dict[str, str],
    now: int,
    permission_classes: dict[str, str] | None = None,
    allow_senders: frozenset[str] = frozenset(),
) -> BuzzDelivery:
    """Sort subscribed events into per-session and machine messages, holding what must be held.

    Addressing follows D-3, most specific first:

    * a decrypted gift wrap belongs to the ONE session whose key decrypted it;
    * a `kind 9` carrying `p` tags that name identities of ours belongs to those sessions;
    * any other `kind 9` on a subscribed channel is a broadcast to every live session.

    `live_slugs is None` is UNDETERMINED, never "none are live" (D-8): a broadcast is then
    carried on the MACHINE notice rather than dropped, because silent non-delivery is the one
    failure the whole design exists to eliminate.

    The bypass-permissions rule is applied LAST and per recipient, so one held session never
    suppresses delivery to the others.
    """
    from ai_chassis.agent_mail_notifier import CLASS_BROADCAST, CLASS_DIRECT

    delivery = BuzzDelivery()
    pubkey_slugs = {pubkey: slug for slug, pubkey in slug_pubkeys.items()}

    def deliver(slug: str, event: RelayEvent, message_class: str) -> None:
        sender = event.author_pubkey
        if is_held(slug, permission_classes, sender, allow_senders):
            permission_class = (permission_classes or {}).get(slug, PERMISSION_UNKNOWN)
            delivery.held.append(
                {
                    "event_id": event.event_id,
                    "session": slug,
                    "sender_pubkey": sender,
                    "channel": event.channel or event.h_tag,
                    # The CLASS is recorded, so `held/` distinguishes a session that really
                    # is in a bypass class from one we could not classify.
                    "permission_class": permission_class,
                    "reason": f"{permission_class}-permissions-sender-not-allow-listed",
                    # Kept so a release after the sender is allow-listed (ISSUE-REF) renders
                    # the notice in the class it would have had.
                    "message_class": message_class,
                    "held_at": _iso_now(now),
                    "subject": _message(
                        event, _sender_label(event, pubkey_names), now
                    ).subject,
                    # So a release delivers a readable body, not only its subject (ISSUE-REF).
                    "body": event.content,
                }
            )
            return
        delivery.per_slug.setdefault(slug, []).append(
            _message(event, _sender_label(event, pubkey_names), now, message_class)
        )

    for event in events:
        # N-09: skip events marked as rejected (e.g., oversized after decryption)
        if event.rejected:
            continue
        if event.kind == KIND_GIFT_WRAP:
            # `channel` carries the identity NAME the wrap was decrypted for; the daemon key's
            # own DMs have no session and belong on the machine notice. A gift wrap is a NIP-17
            # DM either way -- it decrypted with a key only this machine holds -- so both arms
            # are DIRECT (D-13's first targeting mode).
            slug = event.channel
            if slug and (live_slugs is None or slug in live_slugs):
                deliver(slug, event, CLASS_DIRECT)
            else:
                delivery.machine.append(
                    _message(
                        event, _sender_label(event, pubkey_names), now, CLASS_DIRECT
                    )
                )
            continue
        if event.kind != KIND_CHAT:
            continue
        # An ADDRESSED `kind 9` never reaches the broadcast path below, whether or not we hold
        # the addressee's key. The old code only took this branch when a `p` tag MATCHED one of
        # our slugs, so a mention naming a key this machine does not hold fell through and was
        # delivered to every live session -- the receiving half of audit F-26.
        if event.p_tags:
            mentioned = {
                pubkey_slugs[pubkey]
                for pubkey in event.p_tags
                if pubkey in pubkey_slugs
            }
            # A `p`-mention of one of OUR keys is D-13's second targeting mode: addressed to
            # that session by name, so DIRECT even though it rode a public channel.
            for slug in sorted(mentioned):
                deliver(slug, event, CLASS_DIRECT)
            if not mentioned:
                delivery.unaddressed += 1
            continue
        if not live_slugs:
            delivery.machine.append(
                _message(
                    event, _sender_label(event, pubkey_names), now, CLASS_BROADCAST
                )
            )
            continue
        for slug in sorted(live_slugs):
            deliver(slug, event, CLASS_BROADCAST)

    for messages in delivery.per_slug.values():
        messages.sort(key=lambda message: message.message_id)
    delivery.machine.sort(key=lambda message: message.message_id)
    return delivery


# ---------------------------------------------------------------------------------------------
# Config: the `[buzz_notify]` table
# ---------------------------------------------------------------------------------------------

#: The `[buzz_notify]` table's defaults, in the same drop-to-default discipline `load_config`
#: applies to `[agent_mail_notify]`: a key of the wrong TYPE falls back rather than raising,
#: because this runs under a supervisor and a bad edit must not crash-loop the one daemon.
DEFAULT_BUZZ_CONFIG: dict[str, Any] = {
    "enabled": False,
    "relay_url": "",
    "fleet_channel": DEFAULT_FLEET_CHANNEL,
    "machine_channel_prefix": DEFAULT_MACHINE_CHANNEL_PREFIX,
    "presence_interval_s": DEFAULT_PRESENCE_INTERVAL_S,
    "max_bytes": DEFAULT_MAX_BYTES,
    "max_wire_bytes": DEFAULT_MAX_WIRE_BYTES,
    "ok_timeout_s": DEFAULT_OK_TIMEOUT_S,
}

#: Config section name, beside `[agent_mail_notify]`.
BUZZ_CONFIG_SECTION = "buzz_notify"


def load_buzz_config(
    path: Path | None = None,
    *,
    pair: str = "",
    env: dict[str, str] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """The `[buzz_notify]` table merged over the defaults, with the per-pair enable applied, with XDG overlay (D-20).

    T-2.R: enabling Buzz is PER PAIR. `[buzz_notify].enabled` stays `false` in every tracked
    file, and `[buzz_notify.pair.<pair>].enabled = true` is what turns the rung on for the pair
    this machine belongs to. A table-level `true` still works, so a local overlay can enable a
    machine outright, but no tracked file may carry either -- pinned by
    `tests/test_buzz_channel.py::TestTrackedDefault`.

    `strict=True` raises the unreadable-file `RuntimeError` instead of returning the defaults:
    the running notifier's per-tick reload (ISSUE-REF) must keep its last good config across a
    half-written edit, and the defaults would turn the rung off until the next good read.
    """
    from ai_chassis.agent_mail_notifier import merge_typed_section, read_config_tables

    config = dict(DEFAULT_BUZZ_CONFIG)
    config["allow_senders"] = []
    try:
        tables = read_config_tables(path, env=env)
    except RuntimeError:
        if strict:
            raise
        return config
    section = tables.get(BUZZ_CONFIG_SECTION)
    if not isinstance(section, dict):
        return config
    config.update(merge_typed_section(section, DEFAULT_BUZZ_CONFIG))
    senders = section.get("allow_senders")
    if isinstance(senders, list):
        config["allow_senders"] = [item for item in senders if isinstance(item, str)]
    if pair:
        pairs = section.get("pair")
        per_pair = pairs.get(pair) if isinstance(pairs, dict) else None
        if isinstance(per_pair, dict) and isinstance(per_pair.get("enabled"), bool):
            config["enabled"] = bool(config["enabled"]) or per_pair["enabled"]
    return config


# ---------------------------------------------------------------------------------------------
# The channel: one connection, the fleet's conventions, presence, groups, status
# ---------------------------------------------------------------------------------------------


@dataclass
class SendOutcome:
    """What one `buzz send` did. `delivered_via` is `dm` or `mention`; empty when refused."""

    sent: bool
    delivered_via: str = ""
    event_id: str = ""
    channel: str = ""
    reason: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "delivered_via": self.delivered_via,
            "event_id": self.event_id,
            "channel": self.channel,
            "reason": self.reason,
        }


@dataclass
class BuzzChannel:
    """The `buzz` rung, as the one notifier daemon sees it.

    Non-raising by contract. Every public method records a reason and returns a value the
    caller can act on, because the AC that matters most here is the one that says a Buzz
    failure must leave agent mail delivering (T-2.2) -- and an exception escaping into
    `Notifier.tick` is exactly how that stops being true.
    """

    config: dict[str, Any]
    machine: str
    key_dir: Path
    pair: dict[str, Any] | None = None
    clock: Any = None
    connect_factory: Any = None
    log: Any = None
    #: Returns `slug -> bypass|normal|unknown`. Injected so a test needs no `/proc` fixture,
    #: and defaulted to :func:`permission_classes` by the notifier's builder.
    permission_probe: Any = None
    #: issue-ref D-20: whether `[remote_launch].enabled` is true on this machine, decided by the
    #: notifier's builder. Only then does the daemon key subscribe to `KIND_LAUNCH_REQUEST`.
    remote_launch_enabled: bool = False
    #: ISSUE-REF: answers, at each (re)subscribe, whether this machine has sent a launch
    #: request whose `KIND_LAUNCH_RESULT` it should now listen for. None = never.
    launch_results_wanted: Any = None
    #: ISSUE-REF: the identity (`<machine>--<slug>`) a peer's launch records are DMed to, from
    #: `[sso_reauth].attribution_buzz_recipient`. "" = this machine receives none, and every DM
    #: stays a message.
    attribution_recipient: str = ""
    #: name -> identity, minted on demand.
    identities: dict[str, BuzzIdentity] = field(default_factory=dict)
    #: identity name -> the socket AUTHENTICATED AS that identity (ISSUE-REF). The pinned relay
    #: binds one pubkey per WebSocket, p-gates gift-wrap reads to it and refuses any other
    #: non-gift-wrap event, so every identity that reads its DMs or publishes needs its own.
    connections: dict[str, RelayConnection] = field(default_factory=dict)
    #: identity name -> why its socket could not be dialled, for identities with none.
    connection_errors: dict[str, str] = field(default_factory=dict)
    reachable: bool = False
    #: Whether the last `_subscribe_all` actually put our filters on the relay (F-28). The
    #: resolver's probe needs this: an authenticated socket with no live subscription
    #: delivers nothing, and looks exactly like a quiet channel.
    subscribed: bool = False
    last_ok_at: str | None = None
    last_error: str | None = None
    subscriptions: list[str] = field(default_factory=list)
    delivered_via: dict[str, int] = field(
        default_factory=lambda: {DELIVERED_VIA_DM: 0, DELIVERED_VIA_MENTION: 0}
    )
    #: OQ-8: an `OK false` on a `kind 9007`/`9000` is surfaced here and in `status()`.
    group_refusals: list[str] = field(default_factory=list)
    #: channel name -> its UUID on the relay, resolved once per name (:meth:`channel_id`).
    channel_ids: dict[str, str] = field(default_factory=dict)
    #: Whether the daemon's pure-`#h` chat REQ is live. Not part of rung health: a relay that
    #: refuses the channel lifecycle must not take DMs down with it (OQ-8).
    chat_subscribed: bool = False
    #: Channel names whose `kind 9007` create the relay has ANSWERED, and `<identity> <channel>`
    #: pairs whose self-add `kind 9000` it has. Tracked separately because they expire differently:
    #: a channel only needs creating once, but a session that goes live AFTER the first connect
    #: still needs admitting. Marked on the relay's answer, never on the send (ISSUE-REF).
    groups_created: set[str] = field(default_factory=set)
    group_members_proposed: set[str] = field(default_factory=set)
    #: Group steps the relay left unanswered on a live socket: key -> (monotonic time the retry
    #: is due, the backoff that set it). A step lost with its socket is retried on the next pass.
    group_retry: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Group step key -> its outstanding refusal or unconfirmed line; `group_refusals` is built
    #: from it, so a step that later succeeds stops being reported.
    group_outcomes: dict[str, str] = field(default_factory=dict)
    #: Identity names whose `kind 0` profile the relay has ACCEPTED (D-16). A `kind 0` is a
    #: replaceable event, so this stops a steady-state tick republishing it every interval.
    metadata_announced: set[str] = field(default_factory=set)
    #: Inbound events refused as inauthentic, cumulative across reconnects (F-30). Kept on the
    #: CHANNEL as well as the connection so a relay that forged events, was dropped and
    #: reconnected does not reset the evidence that it did.
    events_rejected: int = 0
    last_presence_at: int | None = None
    backoff_s: float = 0.0
    #: identity -> family -> `{high_water, floor, seen}` (ISSUE-REF), loaded from
    #: `delivered.json` on first use so a restart keeps what was already delivered.
    delivered: dict[str, dict[str, dict[str, Any]]] | None = None
    #: Re-reads the `[buzz_notify]` table, called at the top of every poll (ISSUE-REF): an
    #: operator's edit to `allow_senders` or any other key applies on the next tick, with no
    #: restart. None keeps `config` as constructed.
    config_loader: Any = None
    #: Why the last reload failed, logged once per distinct reason; None after a good read.
    config_error: str | None = None
    #: Monotonic clock for the D-25 subscription lease; `time.monotonic` when None.
    monotonic: Any = None
    #: REQs and quiet reads of the current pass, shared by every socket (T-6.4).
    counters: RelayCounters = field(default_factory=RelayCounters)

    # -- small helpers -----------------------------------------------------------------------

    def _now(self) -> int:
        if self.clock is not None:
            return int(self.clock())
        return int(dt.datetime.now(dt.timezone.utc).timestamp())

    def _say(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def refresh_config(self) -> None:
        """Re-read `[buzz_notify]` through `config_loader`. Never raises (ISSUE-REF).

        A failed read keeps the last good config: a half-written overlay must not switch the
        rung off, or an operator's edit in progress would read as the relay going away.
        """
        if self.config_loader is None:
            return
        try:
            config = self.config_loader()
        except Exception as exc:  # noqa: BLE001 - a classified word, never the exception text
            reason = type(exc).__name__
            if reason != self.config_error:
                self._say(
                    f"buzz config reload failed ({reason}); keeping the last good config"
                )
            self.config_error = reason
            return
        self.config_error = None
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled") is True

    @property
    def fleet_channel(self) -> str:
        return str(self.config.get("fleet_channel") or DEFAULT_FLEET_CHANNEL)

    @property
    def machine_channel(self) -> str:
        prefix = str(
            self.config.get("machine_channel_prefix") or DEFAULT_MACHINE_CHANNEL_PREFIX
        )
        return f"{prefix}{self.machine}"

    @property
    def channels(self) -> list[str]:
        return [self.fleet_channel, self.machine_channel]

    @property
    def channel_map_path(self) -> Path:
        return channel_map_path(self.key_dir)

    @property
    def delivered_path(self) -> Path:
        return delivered_path(self.key_dir)

    def _delivered_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self.delivered is None:
            self.delivered = load_delivered(self.delivered_path)
        return self.delivered

    def _identity_delivered(self, name: str) -> dict[str, dict[str, Any]]:
        """`name`'s families, adopting the record its legacy name wrote (ISSUE-REF).

        The state is keyed by identity NAME, so without this a `<machine>--<slug>` record is
        orphaned by the rename: the new name's first REQ asks for the whole history and finds an
        empty seen-set, re-delivering everything the old name already delivered. Moved, never
        copied, like the key; persisted by the next write.
        """
        state = self._delivered_state()
        if name not in state:
            legacy = legacy_identity_name(name)
            if legacy in state:
                state[name] = state.pop(legacy)
        return state.setdefault(name, {})

    def delivery_since(self, name: str, family: str) -> int | None:
        """The `since` for `name`'s `family` REQ: None until something was delivered.

        None on first contact asks for the whole stored history, delivered once. After that the
        bound trails the high-water mark by `DELIVERY_SKEW_S`, clamped to this machine's clock
        so one future-dated event cannot push the window past every real one, and never below
        the floor an overflow of the seen-set raised.
        """
        record = self._identity_delivered(name).get(family)
        if record is None:
            return None
        high_water = min(record["high_water"], self._now())
        return max(high_water - DELIVERY_SKEW_S, record["floor"], 0)

    def _delivery_owner(self, event: RelayEvent) -> tuple[str, str] | None:
        """(identity, family) a chat message or gift wrap is tracked under, else None."""
        daemon = daemon_identity_name(self.machine)
        if event.kind == KIND_CHAT:
            return daemon, DELIVERY_FAMILY_CHAT
        if event.kind != KIND_GIFT_WRAP:
            return None
        for name, record in self.identities.items():
            if record.pubkey_hex in event.p_tags:
                return name, DELIVERY_FAMILY_DM
        return daemon, DELIVERY_FAMILY_DM

    def _undelivered(
        self, events: list[RelayEvent], *, durable: bool = False
    ) -> list[RelayEvent]:
        """`events` minus every chat message and gift wrap already handed to delivery.

        Recorded HERE, before delivery, and persisted at once: the window between this write and
        the notifier's marker write is one tick's in-process work, and the other order (record
        after the markers) would re-deliver on every tick whose marker write raised. `kind 0`,
        presence and launch events pass through untouched: they produce no notice, and their
        filters keep the documented no-`since` shape (D-16, ISSUE-REF, D-20).
        """
        state = self._delivered_state()
        fresh: list[RelayEvent] = []
        touched: set[tuple[str, str]] = set()
        for event in events:
            owner = self._delivery_owner(event)
            if owner is None:
                fresh.append(event)
                continue
            name, family = owner
            record = self._identity_delivered(name).setdefault(
                family, {"high_water": 0, "floor": 0, "seen": {}}
            )
            if event.event_id in record["seen"]:
                continue
            record["seen"][event.event_id] = int(event.created_at)
            record["high_water"] = max(record["high_water"], int(event.created_at))
            touched.add(owner)
            fresh.append(event)
        for name, family in touched:
            self._bound_seen(
                state[name][family], self.delivery_since(name, family) or 0
            )
        if touched or durable:
            try:
                _write_private_json(
                    self.delivered_path,
                    {"schema": DELIVERED_SCHEMA, "identities": state},
                )
                if durable:
                    from ai_chassis.wake.events import sync_directory

                    sync_directory(self.delivered_path.parent)
            except OSError:
                if durable:
                    self.delivered = None
                    raise
        return fresh

    def delivery_recorded(self, event: RelayEvent) -> bool:
        """Read the shared replay exclusion without advancing its one writer."""
        owner = self._delivery_owner(event)
        if owner is None:
            return False
        name, family = owner
        record = self._identity_delivered(name).get(family, {})
        since = self.delivery_since(name, family)
        return event.event_id in record.get("seen", {}) or (
            since is not None and event.created_at < since
        )

    def recover_dispatch_delivery(self, root: Path, directory) -> BuzzDelivery:
        """Rollback transfers frozen, unacknowledged payloads, even below since.

        The supervisor serializes whole-path selection. No BuzzSource import or
        new delivery cursor is needed on a legacy-selected host. The old legacy
        pre-marker acknowledgement boundary remains unchanged.
        """
        from ai_chassis.agent_mail_notifier import Message
        from ai_chassis.wake.events import Event, write_record

        refs = {} if directory is None else {ref.slug: ref for ref in directory}
        delivery = BuzzDelivery()
        for path in sorted((root / "bindings").glob("*.json")):
            binding = json.loads(path.read_text())
            facts = [
                fact
                for fact in binding.get("pending", [])
                if not fact["handed_off"]
                and (
                    fact["event"]["target"]["kind"] == "machine"
                    or (
                        (ref := refs.get(fact["event"]["target"]["value"])) is not None
                        and ref.session_id == fact["event"]["target"]["session_id"]
                    )
                )
            ]
            if not facts:
                continue
            # Do not consult since: the private spool can contain older work.
            for fact in facts:
                event = fact["event"]
                target = event["target"]
                message = Message(**fact["message"])
                if target["kind"] == "session":
                    delivery.per_slug.setdefault(target["value"], []).append(message)
                else:
                    delivery.machine.append(message)
                fact["handed_off"] = True
            write_record(path, binding)
            for fact in facts:
                pending = (
                    root
                    / "pending"
                    / f"{Event.from_record(fact['event']).identity}.json"
                )
                if pending.exists():
                    row = json.loads(pending.read_text())
                    write_record(pending, {**row, "handed_off": True})
            if binding.get("wire"):
                self._undelivered([RelayEvent(**binding["wire"])])
        return delivery

    @staticmethod
    def _bound_seen(record: dict[str, Any], since: int) -> None:
        """Bound the seen-set: drop ids the REQ's `since` already excludes, then cap the size.

        An id older than `since` can never be replayed, so keeping it buys nothing. A cap overflow
        evicts the oldest ids and lifts `floor` past them, trading a lost out-of-order straggler
        in that pathological case for never re-delivering an evicted id.
        """
        seen: dict[str, int] = record["seen"]
        for event_id in [key for key, created in seen.items() if created < since]:
            del seen[event_id]
        if len(seen) <= DELIVERED_SEEN_CAP:
            return
        ordered = sorted(seen.items(), key=lambda item: (item[1], item[0]))
        evicted = ordered[: len(seen) - DELIVERED_SEEN_CAP]
        for event_id, _created in evicted:
            del seen[event_id]
        record["floor"] = max(record["floor"], evicted[-1][1] + 1)

    def channel_id(self, name: str, *, persist: bool = True) -> str:
        """The relay's UUID for channel `name`: persisted, else derived (and then persisted).

        The relay has no channel names on the wire: an `h` that is not a UUID is no channel, so
        a create by name is refused and a REQ by name is CLOSED (OQ-8, ISSUE-REF).
        """
        known = self.channel_ids.get(name)
        if known:
            return known
        persisted = load_channel_map(self.channel_map_path)
        channel = persisted.get(name) or derive_channel_id(
            str((self.pair or {}).get("name") or ""), name
        )
        if persist:
            self.channel_ids[name] = channel
            if name not in persisted:
                with contextlib.suppress(OSError):
                    _write_private_json(
                        self.channel_map_path, {**persisted, name: channel}
                    )
        return channel

    def channel_name(self, channel_id: str) -> str:
        """The name for a channel UUID this channel resolved, else the value as received."""
        for name, known in self.channel_ids.items():
            if known == channel_id:
                return name
        return channel_id

    @property
    def max_bytes(self) -> int:
        value = self.config.get("max_bytes", DEFAULT_MAX_BYTES)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else DEFAULT_MAX_BYTES
        )

    @property
    def max_wire_bytes(self) -> int:
        value = self.config.get("max_wire_bytes", DEFAULT_MAX_WIRE_BYTES)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else DEFAULT_MAX_WIRE_BYTES
        )

    @property
    def ok_timeout_s(self) -> float:
        value = self.config.get("ok_timeout_s", DEFAULT_OK_TIMEOUT_S)
        return (
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
            else DEFAULT_OK_TIMEOUT_S
        )

    @property
    def presence_interval_s(self) -> int:
        value = self.config.get("presence_interval_s", DEFAULT_PRESENCE_INTERVAL_S)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else DEFAULT_PRESENCE_INTERVAL_S
        )

    @property
    def lease_s(self) -> int:
        """D-25: how long a live subscription lasts before one replacement REQ renews it."""
        return LEASE_PRESENCE_INTERVALS * self.presence_interval_s

    @property
    def allow_senders(self) -> frozenset[str]:
        senders = self.config.get("allow_senders") or []
        return frozenset(item for item in senders if isinstance(item, str))

    def relay_endpoint(self) -> RelayEndpoint | None:
        """The derived endpoint, or None when it cannot be derived. Never raises."""
        try:
            return derive_relay_endpoint(
                self.pair, explicit=str(self.config.get("relay_url") or "")
            )
        except BuzzError as exc:
            self.last_error = str(exc)
            return None

    def relay_url(self) -> str:
        """The derived identity URL, or "" when it cannot be derived. Never raises."""
        endpoint = self.relay_endpoint()
        return endpoint.identity_url if endpoint is not None else ""

    def identity(self, name: str) -> BuzzIdentity | None:
        """The keypair for `name`, minting it on first use. None (with a reason) on refusal."""
        existing = self.identities.get(name)
        if existing is not None:
            return existing
        try:
            minted = ensure_identity(name, directory=self.key_dir)
        except BuzzError as exc:
            self.last_error = str(exc)
            self._say(f"buzz identity refused for {name}: {exc}")
            return None
        self.identities[name] = minted
        # D-16: a key is in the directory from the moment it exists, not only once a relay
        # accepted its `kind 0` -- a same-machine `--to` must work with no relay at all.
        self.record_identity_pubkey(minted)
        return minted

    @property
    def daemon_identity(self) -> BuzzIdentity | None:
        return self.identity(daemon_identity_name(self.machine))

    @property
    def connection(self) -> RelayConnection | None:
        """The machine daemon's socket: the one the protocol-health record describes."""
        return self.connections.get(daemon_identity_name(self.machine))

    def slug_pubkeys(self) -> dict[str, str]:
        """slug -> pubkey, for the sessions this channel currently holds a key for."""
        out: dict[str, str] = {}
        for name, record in self.identities.items():
            _, machine, slug = parse_identity_parts(name)
            if machine != self.machine or slug is None:
                continue
            out[slug] = record.pubkey_hex
        return out

    def pubkey_names(self) -> dict[str, str]:
        """pubkey -> name, from our own identities AND the kind-0 directory (D-16).

        The directory half is what lets an inbound message name its sender: without it every
        peer renders as `buzz:<12 hex chars>`, which is unaddressable by a human reading it.
        """
        names = {record.pubkey_hex: name for name, record in self.identities.items()}
        own = set(names)
        newest: dict[str, int] = {}
        for name, entry in load_pubkey_directory(self.pubkey_directory).items():
            pubkey = entry.get("pubkey")
            if not isinstance(pubkey, str) or not pubkey or pubkey in own:
                continue
            created = entry.get("created_at")
            stamp = created if isinstance(created, int) else -1
            # A key that renamed itself (ISSUE-REF) is still bound under its old name too:
            # first binding wins (D-21), so the label follows its newest announcement.
            if pubkey not in names or stamp > newest[pubkey]:
                names[pubkey] = name
                newest[pubkey] = stamp
        return names

    # -- D-16 the kind-0 name directory ------------------------------------------------------

    @property
    def pubkey_directory(self) -> Path:
        return pubkey_directory_path(self.key_dir)

    # -- F-28 the protocol-health record the resolver's probe reads ---------------------------

    @property
    def channel_state_path(self) -> Path:
        return channel_state_path(self.key_dir)

    @property
    def presence_file(self) -> Path:
        return presence_file_path(self.key_dir)

    @property
    def auth_ok(self) -> bool:
        """Whether the relay has not REFUSED our AUTH. True before a connection exists.

        True rather than False for "no connection yet", because an absent connection is
        already reported by `reachable`, and reporting it twice would make
        `buzz-wss-failed` and `buzz-auth-refused` fire together for one fault -- the probe
        would then name the wrong one, since it checks reachability first.
        """
        return self.connection is None or self.connection.authenticated

    def write_channel_state(self) -> bool:
        """Persist protocol health for the resolver. Called on EVERY tick. F-28.

        Every tick, not only on change: the probe ages `last_ok_at` to decide staleness, so
        a daemon that stopped writing has to become stale rather than keep a healthy record
        alive. Never raises -- a state file we cannot write must not stop a delivery.
        """
        connection = self.connection
        document = {
            "schema": CHANNEL_STATE_SCHEMA,
            "updated_at": _iso_now(self._now()),
            "reachable": bool(self.reachable),
            "auth_ok": bool(self.auth_ok),
            "subscribed": bool(self.subscribed),
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error
            or (connection.auth_error if connection is not None else "")
            or None,
            "connections": self.connection_states(),
            "reqs_sent": self.counters.reqs_sent,
            "quiet_reads": self.counters.quiet_reads,
        }
        try:
            return _write_private_json(self.channel_state_path, document)
        except OSError as exc:
            self._say(f"buzz-state-write-failed ({exc.strerror})")
            return False

    def connection_states(self) -> dict[str, dict[str, Any]]:
        """identity name -> its socket's state. Names and booleans only, never a key."""
        states: dict[str, dict[str, Any]] = {}
        for name, reason in self.connection_errors.items():
            states[name] = {
                "connected": False,
                "authenticated": False,
                "subscribed": False,
                "last_error": reason,
            }
        for name, connection in self.connections.items():
            states[name] = {
                "connected": connection.connected,
                "authenticated": connection.authenticated,
                "subscribed": connection.subscribed,
                "last_error": connection.last_error or connection.auth_error or None,
            }
        return dict(sorted(states.items()))

    def record_identity_pubkey(self, record: BuzzIdentity) -> None:
        """Put one of OUR identities into the directory, so a local `--to` resolves at once.

        Stamped with this machine's clock as the `created_at`, which is what makes a local
        mint the FIRST binding for a name this machine has just started using -- and under
        D-21 that first binding is the one that stands.

        The D-21 consequence, accepted by that decision rather than worked around here: a key
        this machine LOST (its file deleted) cannot rebind its own name either, because the
        attestation needs the key that is gone. Rotating a session key is therefore
        `buzz identity forget <name>` and then a restart, documented in the ops doc's Step 2.
        No local-is-trusted exemption: an attacker able to plant a key file under the Buzz
        config directory can already write `pubkeys.json` itself, so the exemption would buy
        nothing and would put a second, weaker rule in front of the one D-21 chose.

        Restricted to THIS machine's own identity names. Asserting a locally minted key for a
        name belonging to another machine would poison the directory with a key no peer holds,
        and the refusal `--to` depends on would become a confident wrong answer.
        """
        if parse_identity_parts(record.name)[1] != self.machine:
            return
        now = self._now()
        with contextlib.suppress(OSError):
            record_pubkey(
                self.pubkey_directory,
                record.name,
                record.pubkey_hex,
                created_at=now,
                now=now,
            )

    def resolve_recipient_pubkey(self, name: str) -> str:
        """The recipient's public key, or "" -- which every caller must treat as a REFUSAL. D-16.

        The directory FIRST, because that is the only source that can answer for a session on
        another machine. A local key FILE is consulted only as a fallback for a name this
        machine already holds a key for, and is never used to invent a key for a name it does
        not: reading a private key file was the sole lookup before, which is why a remote
        recipient always resolved to "" and the message became a broadcast (audit F-26).
        """
        from_directory = resolve_pubkey(self.pubkey_directory, name)
        if from_directory:
            return from_directory
        if not _key_path(self.key_dir, name).is_file():
            return ""
        record = self.identity(name)
        return record.pubkey_hex if record is not None else ""

    async def announce_identities(self) -> None:
        """Publish a `kind 0` for every identity we hold that has not announced yet. D-16.

        Called after AUTH on each connect and after new session keys are minted, so a session
        that went live mid-run becomes addressable without waiting for a reconnect. Tracked in
        `metadata_announced` so a steady-state tick republishes nothing: a `kind 0` is a
        replaceable event, and re-sending it every 10 s would be pure relay traffic.

        A refusal is SURFACED, never swallowed: an identity whose profile the relay rejected is
        an identity no peer can address, and that must not read as a healthy connection.

        Each profile goes out over its OWN identity's socket (ISSUE-REF): the pinned relay
        refuses a `kind 0` not signed by the socket's AUTH key. An identity with no open socket
        (not live) is left for the tick on which it has one.
        """
        now = self._now()
        for name in sorted(self.identities):
            if name in self.metadata_announced:
                continue
            connection = self.connections.get(name)
            if connection is None or not connection.connected:
                continue
            record = self.identities[name]
            event = build_metadata_event(record.keys(), name, now=now)
            try:
                result = await connection.publish(event)
            except BuzzError as exc:
                self._say(f"buzz-metadata-refused ({name}: {exc})")
                continue
            if not result.accepted:
                self._say(
                    f"buzz-metadata-refused "
                    f"({name}: {result.message or 'no reason given'})"
                )
                continue
            self.metadata_announced.add(name)
            self.record_identity_pubkey(record)

    def absorb_metadata(self, events: list[RelayEvent]) -> int:
        """Fold inbound `kind 0` events into the directory. Returns how many entries changed.

        Metadata is NOT a message and is never delivered to a session: it is addressing data,
        and rendering it as mail would put every peer's profile into every session's notice.

        A refused rebind is LOGGED rather than counted (D-21): the return value answers "how
        many entries moved", and a takeover attempt that moved nothing has to be visible
        somewhere other than a number that stayed the same.
        """
        changed = 0
        for event in events:
            if event.kind != KIND_METADATA:
                continue
            name = metadata_name(event)
            if not name or not event.pubkey:
                continue
            try:
                update = record_pubkey(
                    self.pubkey_directory,
                    name,
                    event.pubkey,
                    created_at=event.created_at,
                    now=self._now(),
                    rotation=rotation_attestation(event),
                )
                if update.changed:
                    changed += 1
                elif update.reason:
                    self._report_binding_refusal(name, event.pubkey, update)
            except OSError as exc:
                self._say(f"buzz-directory-write-failed ({exc.strerror})")
                return changed
        return changed

    def _report_binding_refusal(
        self, name: str, claimed_by: str, update: DirectoryUpdate
    ) -> None:
        """Log a refused `kind 0` rebind, naming BOTH keys. D-21.

        Both pubkeys, unlike `_report_rejections`, and the difference is deliberate. That one
        refuses events whose signature did NOT check out, where the pubkey is an unverified
        attacker-supplied string that must not be rendered (audit F-30). Every event reaching
        here has already passed `validate_event_node`, so both keys are proved to have signed
        their own claims -- and the operator cannot otherwise tell a session that lost its key
        (forget the entry) from a session trying to take a name (do not).

        Truncated, because the question the line answers is "which two keys", and the operator
        compares the prefixes against `buzz status`'s `directory_names`.
        """
        self._say(
            f"buzz-directory-refused ({update.reason}: {name} is bound to "
            f"{update.bound_pubkey[:PUBKEY_LOG_CHARS]}, refused a kind 0 signed by "
            f"{claimed_by[:PUBKEY_LOG_CHARS]}; clear it with "
            f"`buzz identity forget {name}` only if that key is genuinely lost)"
        )

    def absorb_presence(self, events: list[RelayEvent]) -> int:
        """Fold inbound `kind 20001` presence events into the presence file. ISSUE-REF.

        Returns how many entries changed. Presence is NOT a message and is never delivered to a
        session: it is liveness data, and the resolver ages it to decide cross-machine staleness.

        A presence event from a sender outside the allow-list is recorded with its pubkey and
        never filed as a message: the sender control applies to DELIVERY, not to liveness
        tracking (which must see every peer's presence to answer the cross-machine question).
        """
        changed = 0
        # Resolve the directory ONCE before the loop: repeated loads would make O(n²) reads.
        pubkey_to_name = self.pubkey_names()
        for event in events:
            if event.kind != KIND_PRESENCE:
                continue
            # F-30: verify before recording. A forged presence is a forged liveness claim.
            event_json = json.dumps(
                {
                    "id": event.event_id,
                    "pubkey": event.pubkey,
                    "created_at": event.created_at,
                    "kind": event.kind,
                    "tags": event.tags,
                    "content": event.content,
                    "sig": event.signature,
                }
            )
            if not verify_event_json(event_json):
                self._say(
                    "buzz-presence-rejected (id-or-signature-invalid); event not recorded"
                )
                continue
            if not event.pubkey:
                continue
            liveness = parse_presence_liveness(event)
            # Resolve the identity name from the pubkey directory. If it is not in the directory,
            # skip recording: we cannot attribute the presence to an identity, and recording a
            # pubkey-only entry would be unactionable by the liveness resolver.
            identity = pubkey_to_name.get(event.pubkey, "")
            if not identity:
                continue
            _, machine, slug = parse_identity_parts(identity)
            if not machine:
                continue
            try:
                if record_presence(
                    self.presence_file,
                    identity,
                    machine,
                    slug,
                    liveness,
                    event.pubkey,
                    generated_at=event.created_at,
                    now=self._now(),
                    agent_mail_name=parse_presence_board_name(event, identity),
                    presence_event=parse_presence_change(event, identity),
                ):
                    changed += 1
            except OSError as exc:
                self._say(f"buzz-presence-write-failed ({exc.strerror})")
                return changed
        return changed

    # -- T-2.2 connection --------------------------------------------------------------------

    async def ensure_connection(self, slugs: set[str] | None) -> bool:
        """Open the daemon's connection and one per live session, settle `AUTH`, (re)subscribe.

        False with a reason when the DAEMON's socket fails; a session socket that fails is
        recorded against that identity and leaves the rest of the rung up (ISSUE-REF).

        Resubscription happens AFTER the handshake every time, including after a reconnect: a
        relay that authenticated a new connection has no memory of the old one's `REQ`s, and a
        client that assumed otherwise would sit on a live socket receiving nothing -- a silent
        non-delivery that reads as "no mail".
        """
        if not self.enabled:
            self.last_error = "buzz-not-configured"
            return False
        endpoint = self.relay_endpoint()
        if endpoint is None:
            return False
        daemon = self.daemon_identity
        if daemon is None:
            return False

        if self.connection is not None and self.connection.connected:
            return await self._subscribe_all(slugs)

        try:
            await self._open_connection(daemon, endpoint)
        except BuzzError as exc:
            self.connections.pop(daemon.name, None)
            self.reachable = False
            self.last_error = str(exc)
            self.backoff_s = next_backoff(self.backoff_s)
            self._say(
                f"buzz relay unreachable ({exc}); retry after {self.backoff_s:.0f}s"
            )
            return False
        self.backoff_s = 0.0
        self.reachable = True
        self.last_ok_at = _iso_now(self._now())
        self.last_error = None
        return await self._subscribe_all(slugs)

    async def _open_connection(
        self, record: BuzzIdentity, endpoint: RelayEndpoint
    ) -> RelayConnection:
        """Dial `endpoint` and authenticate as `record`. Raises :class:`BuzzError` on a failed dial."""
        connection = RelayConnection(
            relay_url=endpoint.identity_url,
            dial=endpoint.dial,
            keys=record.keys(),
            clock=self.clock,
            connect_factory=self.connect_factory,
            max_bytes=self.max_bytes,
            max_wire_bytes=self.max_wire_bytes,
            counters=self.counters,
            monotonic=self.monotonic,
            ok_timeout_s=self.ok_timeout_s,
        )
        # `open()` settles the AUTH handshake before anything is subscribed or published.
        await connection.open()
        stale = self.connections.get(record.name)
        if stale is not None:
            await stale.close()
        self.connections[record.name] = connection
        self.connection_errors.pop(record.name, None)
        await connection.pump()
        return connection

    async def connection_for(self, name: str) -> RelayConnection | None:
        """The socket authenticated as `name`, dialled on first use. None with a recorded reason.

        Every publish goes through here with the SIGNER's name: the pinned relay refuses any
        non-gift-wrap event whose pubkey is not the socket's AUTH key (`handlers/event.rs:652-661`).
        """
        existing = self.connections.get(name)
        if existing is not None and existing.connected:
            return existing
        endpoint = self.relay_endpoint()
        record = self.identity(name)
        if endpoint is None or record is None:
            return None
        try:
            return await self._open_connection(record, endpoint)
        except BuzzError as exc:
            self.connections.pop(name, None)
            self.connection_errors[name] = str(exc)
            self._say(f"buzz connection refused for {name} ({exc})")
            return None

    async def _close_identity(self, name: str) -> None:
        connection = self.connections.pop(name, None)
        self.connection_errors.pop(name, None)
        if connection is not None:
            await connection.close()

    async def _sync_session_connections(self, slugs: set[str] | None) -> None:
        """One subscribed socket per live session; close the sockets of those no longer live.

        `None` is the probe being BLIND (D-8), never "nobody is live": nothing is closed and
        nothing new is opened, and the sockets already held keep delivering.
        """
        daemon_name = daemon_identity_name(self.machine)
        if slugs is not None:
            wanted = {identity_name(self.machine, slug) for slug in slugs}
            stale = set(self.connections) | set(self.connection_errors)
            for name in sorted(stale - wanted - {daemon_name}):
                await self._close_identity(name)
            for name in sorted(wanted):
                await self.connection_for(name)
        for name in sorted(self.connections):
            if name != daemon_name:
                await self._subscribe_session(name)

    async def _resubscribe(
        self, connection: RelayConnection, sub_id: str, filters: list[dict[str, Any]]
    ) -> None:
        """REQ `sub_id` only when D-25 says one is owed; otherwise the live one stands.

        NIP-01: a relay keeps a subscription "active for newly received matching events" until
        the connection closes, and a REQ reusing the id REPLACES it -- so a REQ every tick bought
        a replay of stored history and nothing else. Raises :class:`BuzzError` on a dead send.
        """
        due = connection.subscription_due(sub_id, filters, self.lease_s)
        if not due:
            return
        if due == DUE_CLOSED and sub_id not in connection.closed_reported:
            # A CLOSED that arrived between passes, not as the answer to a REQ of ours.
            reason = connection.closed_reasons.get(sub_id, "")
            self._say(
                f"buzz subscription-closed ({sub_id})"
                + (f": {reason}" if reason else "")
                + "; resubscribing"
            )
        await connection.subscribe(sub_id, filters)
        if due == DUE_LEASE:
            self._say(
                f"buzz subscription lease renewed ({sub_id}) after {self.lease_s}s"
            )

    async def _subscribe_session(self, name: str) -> None:
        """REQ the gift wraps addressed to this session's key, on this session's own socket."""
        connection = self.connections.get(name)
        record = self.identities.get(name)
        if connection is None or record is None or not connection.connected:
            return
        sub_id = f"aih-{name}"
        try:
            await self._resubscribe(
                connection,
                sub_id,
                subscription_filters(
                    channels=[],
                    dm_pubkeys=[record.pubkey_hex],
                    since=self.delivery_since(name, DELIVERY_FAMILY_DM),
                ),
            )
        except BuzzError as exc:
            connection.subscribed = False
            connection.last_error = str(exc)
            await self._close_identity(name)
            self.connection_errors[name] = str(exc)
            return
        if sub_id in connection.closed_subscriptions:
            connection.subscribed = False
            reason = connection.closed_reasons.get(sub_id, "")
            connection.last_error = f"subscription-closed ({sub_id})" + (
                f": {reason}" if reason else ""
            )
            self._say(f"buzz {connection.last_error}")
            connection.closed_reported.add(sub_id)
            return
        connection.subscribed = True
        connection.last_error = ""

    async def _subscribe_all(self, slugs: set[str] | None) -> bool:
        connection = self.connection
        if connection is None:
            return False
        for slug in sorted(slugs or set()):
            self.identity(identity_name(self.machine, slug))
        daemon = self.daemon_identity
        # The pinned relay p-gates gift wraps: a `#p` naming any key but the one this socket
        # authenticated as CLOSES the whole REQ ("restricted: p-gated events require #p
        # matching your pubkey", block/buzz `70d2ca7` req.rs:1513-1547), and a connection
        # authenticates exactly one key (auth.rs:308-318). This socket is the daemon's.
        dm_pubkeys = [daemon.pubkey_hex] if daemon is not None else []
        launch_pubkeys = (
            [daemon.pubkey_hex]
            if self.remote_launch_enabled and daemon is not None
            else None
        )
        session_pubkeys = [record.pubkey_hex for record in self.identities.values()]
        result_pubkeys = (
            sorted(session_pubkeys)
            if self.launch_results_wanted is not None and self.launch_results_wanted()
            else None
        )
        # No `#h` filter here: a REQ mixing `#h` with an unscoped filter is registered GLOBAL
        # and never receives a channel event live (`subscription.rs:487-492`). Chat is its own
        # REQ, `_subscribe_chat`, once the channels exist.
        filters = subscription_filters(
            channels=[],
            dm_pubkeys=sorted(dm_pubkeys),
            since=self.delivery_since(
                daemon_identity_name(self.machine), DELIVERY_FAMILY_DM
            ),
            metadata=True,
            presence=True,
            launch_pubkeys=launch_pubkeys,
            result_pubkeys=result_pubkeys,
        )
        sub_id = f"aih-{self.machine}"
        try:
            # A REQ reads the relay's replay up to EOSE, so the SAME pass delivers what was
            # waiting rather than the next one.
            await self._resubscribe(connection, sub_id, filters)
        except BuzzError as exc:
            self.last_error = str(exc)
            self.reachable = False
            self.subscribed = False
            return False
        if sub_id in connection.closed_subscriptions:
            # The relay accepted the REQ frame and then CLOSED it. The socket is fine and
            # nothing will ever arrive on it, which is the failure F-28 exists to surface.
            self.subscribed = False
            connection.subscribed = False
            reason = connection.closed_reasons.get(sub_id, "")
            self.last_error = f"subscription-closed ({sub_id})" + (
                f": {reason}" if reason else ""
            )
            connection.last_error = self.last_error
            self._say(f"buzz {self.last_error}")
            connection.closed_reported.add(sub_id)
            return False
        self.subscribed = True
        connection.subscribed = True
        connection.last_error = ""
        self.subscriptions = []
        if dm_pubkeys:
            self.subscriptions.append(f"{sub_id}:p={len(dm_pubkeys)} key(s)")
        self.subscriptions.append(f"{sub_id}:kind={KIND_METADATA}")
        self.subscriptions.append(f"{sub_id}:kind={KIND_PRESENCE}")
        if launch_pubkeys:
            self.subscriptions.append(f"{sub_id}:kind={KIND_LAUNCH_REQUEST}")
        if result_pubkeys:
            self.subscriptions.append(f"{sub_id}:kind={KIND_LAUNCH_RESULT}")
        await self._sync_session_connections(slugs)
        self.subscriptions.extend(
            f"aih-{name}:p=1 key(s)"
            for name, session in sorted(self.connections.items())
            if session is not connection and session.subscribed
        )
        # D-16: announce BEFORE the group lifecycle. A peer cannot address this machine's
        # sessions until their `kind 0` is on the relay, and `ensure_groups` can spend a
        # round trip per channel on a relay that refuses NIP-29.
        await self.announce_identities()
        await self.ensure_groups(slugs)
        if not connection.connected:
            # ISSUE-REF: a port-forward member's transport supervisor restarting closes every
            # socket mid start-up. The chat REQ and any group step left run on the next pass,
            # on a fresh socket, rather than into this dead one.
            self.reachable = False
            self.subscribed = False
            self.chat_subscribed = False
            self.last_error = connection.last_error or "relay-socket-lost"
            self._say(
                f"buzz relay socket lost during start-up ({self.last_error}); "
                "reconnecting on the next pass"
            )
            return False
        await self._subscribe_chat()
        return True

    async def _subscribe_chat(self) -> None:
        """REQ `kind 9` on both channels BY UUID, alone, on the daemon's socket.

        A CLOSED is logged and kept off rung health: DMs and presence do not depend on it.
        """
        connection = self.connection
        if connection is None:
            return
        sub_id = f"aih-{self.machine}-chat"
        filters = subscription_filters(
            channels=[self.channel_id(name) for name in self.channels],
            dm_pubkeys=[],
            since=self.delivery_since(
                daemon_identity_name(self.machine), DELIVERY_FAMILY_CHAT
            ),
        )
        try:
            await self._resubscribe(connection, sub_id, filters)
        except BuzzError as exc:
            self.chat_subscribed = False
            self._say(f"buzz chat subscribe failed ({exc})")
            return
        if sub_id in connection.closed_subscriptions:
            self.chat_subscribed = False
            reason = connection.closed_reasons.get(sub_id, "")
            self._say(
                f"buzz subscription-closed ({sub_id})"
                + (f": {reason}" if reason else "")
            )
            connection.closed_reported.add(sub_id)
            return
        self.chat_subscribed = True
        self.subscriptions.insert(0, f"{sub_id}:h={','.join(self.channels)}")

    # -- T-2.G group lifecycle (OQ-8) --------------------------------------------------------

    async def ensure_groups(self, slugs: set[str] | None) -> list[str]:
        """Create both channels by UUID, then self-add each identity. Refusals are SURFACED.

        OQ-8, as measured on the pinned relay (ISSUE-REF): any authenticated key may create an
        open channel; a create carries the UUID as `h` and the name as `name`; membership is a
        SELF-targeted `kind 9000`, always allowed, published over that identity's own socket and
        admitting its single `p`. A refusal is recorded as `buzz-group-refused (<reason>)`,
        because a session never admitted publishes into a void that reads as success.

        A step is done once the relay ANSWERS it (ISSUE-REF). One lost with its socket records
        nothing and runs again on the next pass; one left unanswered on a live socket runs again
        on `next_backoff`'s schedule. A repeat is harmless: a second create answers `duplicate:`
        and a second self-add is accepted.
        """
        daemon = self.daemon_identity
        connection = self.connection
        if connection is None or not connection.connected or daemon is None:
            return self.group_refusals
        for channel in self.channels:
            if not await self._ensure_channel(channel):
                return self.group_refusals
        now = self._now()
        members = [daemon.name] + [
            identity_name(self.machine, slug) for slug in sorted(slugs or set())
        ]
        for name in members:
            record = self.identities.get(name)
            connection = self.connections.get(name)
            if record is None or connection is None or not connection.connected:
                continue
            for channel in self.channels:
                key = f"{name} {channel}"
                if key in self.group_members_proposed or not self._group_step_due(key):
                    continue
                add = build_event(
                    record.keys(),
                    KIND_GROUP_ADD_USER,
                    "",
                    [["h", self.channel_id(channel)], ["p", record.pubkey_hex]],
                    created_at=now,
                )
                if await self._record_group_result(
                    connection, add, f"add-member {channel}", key
                ):
                    self.group_members_proposed.add(key)
                elif not connection.connected:
                    break
        return self.group_refusals

    async def _ensure_channel(self, name: str) -> bool:
        """Create channel `name` once per process, by UUID, with the daemon's key.

        False when the daemon's socket was lost doing it, so the caller stops publishing into it.
        """
        connection = self.connection
        daemon = self.daemon_identity
        if connection is None or daemon is None:
            return False
        key = f"create {name}"
        if name in self.groups_created or not self._group_step_due(key):
            return True
        create = build_event(
            daemon.keys(),
            KIND_GROUP_CREATE,
            "",
            [["h", self.channel_id(name)], ["name", name]],
            created_at=self._now(),
        )
        if await self._record_group_result(connection, create, f"create {name}", key):
            self.groups_created.add(name)
        return connection.connected

    def _group_step_due(self, key: str) -> bool:
        retry = self.group_retry.get(key)
        return retry is None or (self.monotonic or time.monotonic)() >= retry[0]

    async def _record_group_result(
        self, connection: RelayConnection, event: Any, what: str, key: str
    ) -> bool:
        """Publish one group step and record the outcome under `key`. True once it is answered."""
        try:
            result = await connection.publish(event)
        except BuzzError:
            return False
        if result.message == NO_OK_REASON:
            if not connection.connected:
                return False
            # No answer is not a refusal: the relay may have stored it (ISSUE-REF, AC-2).
            backoff = next_backoff(self.group_retry.get(key, (0.0, 0.0))[1])
            self.group_retry[key] = (
                (self.monotonic or time.monotonic)() + backoff,
                backoff,
            )
            self._set_group_outcome(
                key,
                f"buzz-group-unconfirmed ({what}: no OK within "
                f"{connection.ok_timeout_s:g} s)",
            )
            return False
        self.group_retry.pop(key, None)
        if result.accepted or result.message.startswith(GROUP_EXISTS_PREFIX):
            self._set_group_outcome(key, "")
        else:
            self._set_group_outcome(
                key,
                f"buzz-group-refused ({what}: {result.message or 'no reason given'})",
            )
        return True

    def _set_group_outcome(self, key: str, line: str) -> None:
        """Record `line` as step `key`'s outstanding failure, or clear it when ""; log a change."""
        if not line:
            self.group_outcomes.pop(key, None)
        elif self.group_outcomes.get(key) != line:
            self.group_outcomes[key] = line
            self._say(line)
        self.group_refusals = list(dict.fromkeys(self.group_outcomes.values()))

    # -- T-2.3 poll --------------------------------------------------------------------------

    async def poll(self, live_slugs: set[str] | None) -> BuzzDelivery:
        """One pass: connect if needed, read what arrived, sort it. Never raises.

        An empty delivery is returned for every refusal, which is what lets the notifier's tick
        call this unconditionally and still write the agent-mail markers it would have written
        anyway (T-2.2's failure path).

        The protocol-health record is written on EVERY exit path (F-28), including each
        refusal: a resolver that only ever saw records from healthy ticks would keep choosing
        this rung for the whole time it was broken.

        A pass starts the T-6.4 counters afresh, so `status()` reports the REQs and quiet reads
        of the LAST pass (presence published after it included), not a running total.
        """
        self.refresh_config()
        self.counters.reset()
        try:
            return await self._poll(live_slugs)
        finally:
            self.write_channel_state()

    async def _poll(self, live_slugs: set[str] | None) -> BuzzDelivery:
        if not await self.ensure_connection(live_slugs):
            return BuzzDelivery()
        connection = self.connection
        if connection is None:
            return BuzzDelivery()
        # Two loop turns before the non-blocking drains, because this loop may not have run since
        # the last pass. The first turn's select moves what the kernel holds for EVERY socket
        # into websockets' buffers, but its read callbacks run after this task resumes in that
        # turn; the second turn resumes after them. Measured: with one turn, a frame pushed
        # before the pass was left for the next one.
        for _ in range(2):
            await asyncio.sleep(0)
        try:
            await connection.pump()
        except BuzzError as exc:
            self.last_error = str(exc)
            self.reachable = False
            return BuzzDelivery()
        # ISSUE-REF: the relay replays its stored history on every REQ (a reconnect, a CLOSED, a
        # lease renewal), so what was delivered on an earlier pass (or before a restart) is
        # dropped here, before anything reads it.
        events = self._undelivered(await self._read_all(connection))
        # D-16: the name directory is updated BEFORE delivery, so a message arriving in the
        # same pump as its sender's profile is already attributable to a name.
        self.absorb_metadata(events)
        # Phase 3 (ISSUE-REF): peer presence is recorded AFTER metadata, so a presence event
        # arriving in the same pump as its sender's profile can be attributed to an identity.
        self.absorb_presence(events)
        for event in events:
            if event.kind == KIND_CHAT:
                event.channel = self.channel_name(event.h_tag)
                if event.p_tags:
                    self.delivered_via[DELIVERED_VIA_MENTION] += 1
            elif event.kind == KIND_GIFT_WRAP:
                await self._decrypt_wrap(event)
        self.last_ok_at = _iso_now(self._now())
        records, events = self._attribution_records(events)
        # The permission probe enumerates every live session (and reads each one's argv through
        # ps where there is no procfs), so it runs only on a pass that has a notice to place:
        # `buzz_delivery` consults the classes per recipient, never otherwise (ISSUE-REF).
        deliverable = any(
            not event.rejected and event.kind in (KIND_CHAT, KIND_GIFT_WRAP)
            for event in events
        )
        delivery = buzz_delivery(
            events,
            live_slugs=live_slugs,
            slug_pubkeys=self.slug_pubkeys(),
            pubkey_names=self.pubkey_names(),
            now=self._now(),
            permission_classes=self._permission_classes() if deliverable else None,
            allow_senders=self.allow_senders,
        )
        delivery.launch_requests = self._launch_requests(events)
        delivery.launch_results = self._launch_results(events)
        delivery.attribution_records = records
        if delivery.unaddressed:
            # Not silent: an addressed message we cannot place is a delivery that did not
            # happen, and the operator has to be able to see that it did not.
            self._say(
                f"buzz-unaddressed-mention ({delivery.unaddressed} event(s) named a key "
                "this machine holds none of; not delivered)"
            )
        return delivery

    async def _read_all(self, daemon: RelayConnection) -> list[RelayEvent]:
        """Every socket's events for this pass, the daemon's first, each event ONCE.

        Each session's DMs arrive on that session's own socket (ISSUE-REF). De-duplicated by
        event id: a live push and the replay of a REQ in the same pass carry the same event.
        """
        events = daemon.take_events()
        self._report_rejections(daemon)
        for _name, session in sorted(self.connections.items()):
            if session is daemon:
                continue
            try:
                await session.pump()
            except BuzzError as exc:
                session.last_error = str(exc)
                continue
            events.extend(session.take_events())
            self._report_rejections(session)
        unique: dict[str, RelayEvent] = {}
        for event in events:
            unique.setdefault(event.event_id, event)
        return list(unique.values())

    async def readable(
        self, timeout: float | None = None, *, interrupt: asyncio.Event | None = None
    ) -> int:
        """Wait until any open socket has a frame or fails, and file what that wait read. D-26.

        The daemon's one wait between passes (issue-ref D-14). Returns how many sockets yielded
        a frame or an error: 0 means `timeout` elapsed or `interrupt` was set first.

        Exactly once, by construction. One `recv` task per socket is the whole wait; when the
        first completes, every task still pending is cancelled AND awaited, and then every task
        that ended with a frame -- including one that completed during its own cancellation --
        hands that frame, in socket-name order, to the same `RelayConnection.dispatch` that
        `pump` uses. A raw frame therefore never reaches `_pending`, which holds validated
        `RelayEvent`s only, and a cancelled `recv` loses nothing (websockets 15 keeps a frame
        it did not return queued for the next read). The hand-over runs in `finally`, so even a
        cancellation of this coroutine cannot drop a frame a task already consumed.

        A socket that raised is recorded once against its identity and dropped from
        `connections`, so the next pass redials it; the other sockets keep delivering.
        """
        waits: dict[asyncio.Task[Any], tuple[str, RelayConnection]] = {
            asyncio.ensure_future(connection.receive()): (name, connection)
            for name, connection in sorted(self.connections.items())
            if connection.connected
        }
        stop = (
            asyncio.ensure_future(interrupt.wait()) if interrupt is not None else None
        )
        watched = [*waits, *([stop] if stop is not None else [])]
        try:
            if watched:
                await asyncio.wait(
                    watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
            elif timeout:
                await asyncio.sleep(timeout)
        finally:
            for task in watched:
                task.cancel()
            await asyncio.gather(*watched, return_exceptions=True)
            handled = await self._hand_over(waits)
        return handled

    async def _hand_over(
        self, waits: dict[asyncio.Task[Any], tuple[str, RelayConnection]]
    ) -> int:
        """Dispatch each finished `recv` task's frame once, in socket order; record errors."""
        handled = 0
        for task, (name, connection) in waits.items():
            if task.cancelled():
                continue
            failure = task.exception()
            if failure is None:
                try:
                    # An AUTH answer is a send, and a send on a dying socket raises: that
                    # must not cost the sockets after this one their frames.
                    await connection.dispatch(task.result())
                except BuzzError as exc:
                    failure = exc
            if failure is not None:
                await self._drop_failed(name, connection, failure)
            handled += 1
        return handled

    async def _drop_failed(
        self, name: str, connection: RelayConnection, failure: BaseException
    ) -> None:
        """Record a socket that failed while waited on, ONCE, and drop it for a redial."""
        connection._lose(failure)
        reason = connection.last_error
        if self.connections.get(name) is connection:
            self.connections.pop(name)
        self.connection_errors[name] = reason
        if name == daemon_identity_name(self.machine):
            self.reachable = False
            self.last_error = reason
        await connection.close()
        self._say(f"buzz connection lost for {name} ({reason}); redialled next pass")

    def _report_rejections(self, connection: RelayConnection) -> None:
        """Log newly refused inbound events, by classified reason only. F-30.

        A refused event is a possible ATTACK and at minimum a broken relay, so it must be
        visible. The log carries the reason and the count and nothing else: the pubkey and
        the content of an unverified event are exactly what must not be rendered anywhere.
        """
        # A DELTA against this connection's own reported count, accumulated onto the
        # channel's running total. Comparing the connection's counter against the channel's
        # total would suppress every rejection on a fresh connection until it exceeded the
        # old one, because a reconnect resets the connection's counter and not the total.
        new = connection.rejected - connection.rejections_reported
        if new <= 0:
            return
        connection.rejections_reported = connection.rejected
        self.events_rejected += new
        reasons = ", ".join(connection.rejections)
        self._say(f"buzz-event-rejected ({reasons}); {new} event(s) not delivered")

    def _permission_classes(self) -> dict[str, str] | None:
        """The probe's classification, `{}` when it RAISED, or None when there is no probe.

        Three outcomes, because they license three different actions (audit F-31):

        * None -- no probe is wired. No classification was attempted, which is a
          configuration state, not a probe failure; holding every notice on such a machine
          would turn a missing seam into fleet-wide silent non-delivery.
        * `{}` -- the probe RAN and failed. Every slug is then `unknown`, and `unknown` is
          held. This is the inversion F-31 asked for: a probe exception used to return "no
          session bypasses", which delivered unvetted messages into sessions that cannot ask
          for permission.
        * populated -- per-slug `bypass` / `normal` / `unknown`.
        """
        if self.permission_probe is None:
            return None
        try:
            classes = self.permission_probe()
        except Exception as exc:  # noqa: BLE001 - a broken probe never fails a delivery
            self._say(
                f"buzz permission probe raised ({type(exc).__name__}); "
                "every session is unknown and holds"
            )
            return {}
        return dict(classes) if isinstance(classes, dict) else {}

    async def _decrypt_wrap(self, event: RelayEvent) -> None:
        """Try every identity's key against a wrap, recording the one that opened it.

        `channel` becomes the SLUG the wrap was addressed to and `sender_pubkey` the SEALED
        sender -- the wrap's own `pubkey` is a single-use ephemeral key and naming a peer by it
        would make every DM look like it came from a stranger.

        N-09: checks the DECRYPTED plaintext size against max_bytes and marks the event as
        rejected-oversized if it exceeds the cap.
        """
        document = {
            "id": event.event_id,
            "pubkey": event.pubkey,
            "kind": event.kind,
            "content": event.content,
            "created_at": event.created_at,
            "tags": event.tags,
            "sig": event.signature,
        }
        for name, record in self.identities.items():
            opened = await unwrap_gift(record.keys(), json.dumps(document))
            if opened is None:
                continue
            sender, text = opened
            # N-09: check decrypted plaintext size
            plaintext_size = len(text.encode("utf-8"))
            if plaintext_size > self.max_bytes:
                # Mark as rejected and record the reason
                event.rejected = True
                self.events_rejected += 1
                if self.connection is not None:
                    if "oversized" not in self.connection.rejections:
                        self.connection.rejections.append("oversized")
                return
            event.sender_pubkey = sender
            event.content = text
            _, machine, slug = parse_identity_parts(name)
            event.channel = (slug or DAEMON_SLUG) if machine == self.machine else name
            self.delivered_via[DELIVERED_VIA_DM] += 1
            return

    def _attribution_records(
        self, events: list[RelayEvent]
    ) -> tuple[list[LaunchInbound], list[RelayEvent]]:
        """Split off the DMs to `attribution_recipient` that carry a launch record. ISSUE-REF.

        Taken out BEFORE `buzz_delivery`, so a record is never rendered as a notice. Only a wrap
        that decrypted with the recipient's own key qualifies; the sender is the SEALED one, which
        `attribution_buzz.admit` judges against `allow_senders` before parsing anything.

        ISSUE-REF: an `approval-outcome` record DMed to THIS machine's daemon also qualifies. That
        is the return leg: the approving host addresses the launching host's daemon, which is not
        `attribution_recipient` on the launching host (that names the approver it sends TO).
        """
        if not self.attribution_recipient:
            return [], events
        from ai_chassis.attribution_buzz import (
            is_approval_outcome_payload,
            is_record_payload,
        )

        names = self.pubkey_names()
        daemon = daemon_identity_name(self.machine)
        records: list[LaunchInbound] = []
        rest: list[RelayEvent] = []
        for event in events:
            to = (
                identity_name(self.machine, event.channel)
                if event.kind == KIND_GIFT_WRAP and not event.rejected and event.channel
                else ""
            )
            if (
                to == self.attribution_recipient and is_record_payload(event.content)
            ) or (to == daemon and is_approval_outcome_payload(event.content)):
                records.append(
                    LaunchInbound(
                        event_id=event.event_id,
                        sender_pubkey=event.author_pubkey,
                        sender_name=names.get(event.author_pubkey, ""),
                        content=event.content,
                    )
                )
                continue
            rest.append(event)
        return records, rest

    def _launch_requests(self, events: list[RelayEvent]) -> list[LaunchInbound]:
        """Launch requests addressed to the daemon key, decrypted. issue-ref D-20.

        Every event here already passed `validate_event_node` in `_file_event` (F-30), so the
        signer IS the outer `pubkey`; that key's directory name is the sender `remote_launch`
        judges. Collected whatever `remote_launch_enabled` says: an OFF machine does not
        subscribe, and one that receives a request anyway still hands it on, so the executor's
        own `disabled` refusal is audited rather than the request vanishing.
        """
        daemon = self.daemon_identity
        if daemon is None:
            return []
        names = self.pubkey_names()
        inbound: list[LaunchInbound] = []
        for event in events:
            if event.kind != KIND_LAUNCH_REQUEST or event.rejected:
                continue
            if daemon.pubkey_hex not in event.p_tags:
                continue
            text = open_addressed_event(daemon.keys(), event)
            if text is None or len(text.encode("utf-8")) > self.max_bytes:
                self.events_rejected += 1
                continue
            sender_name = names.get(event.pubkey, "")
            # Our OWN identities are in `pubkey_names` too; none of them may be a requester.
            if parse_identity_parts(sender_name)[1] == self.machine:
                sender_name = ""
            inbound.append(
                LaunchInbound(
                    event_id=event.event_id,
                    sender_pubkey=event.pubkey,
                    sender_name=sender_name,
                    content=text,
                )
            )
        return inbound

    def _launch_results(self, events: list[RelayEvent]) -> list[LaunchInbound]:
        """Launch replies addressed to one of this machine's session keys, decrypted.

        Same discipline as `_launch_requests`: every event already passed signature validation,
        so `sender_name` is the directory's name for the signing key, and
        `remote_launch.record_result` trusts a reply only when that is the asked daemon's name.
        """
        held = {record.pubkey_hex: record for record in self.identities.values()}
        names = self.pubkey_names()
        inbound: list[LaunchInbound] = []
        for event in events:
            if event.kind != KIND_LAUNCH_RESULT or event.rejected:
                continue
            recipient = next((held[key] for key in event.p_tags if key in held), None)
            if recipient is None:
                continue
            text = open_addressed_event(recipient.keys(), event)
            if text is None or len(text.encode("utf-8")) > self.max_bytes:
                self.events_rejected += 1
                continue
            inbound.append(
                LaunchInbound(
                    event_id=event.event_id,
                    sender_pubkey=event.pubkey,
                    sender_name=names.get(event.pubkey, ""),
                    content=text,
                )
            )
        return inbound

    # -- T-2.4 send --------------------------------------------------------------------------

    def refuse_cross_pair(self, target_machine: str) -> str:
        """`target-not-in-pair (<machine>)` when this relay cannot reach that machine. F-27.

        "" when it can, or when there is nothing to judge (no target machine named). A relay
        serves ONE pair (D-9), so publishing a message for a machine outside it puts the text
        on a relay no intended reader subscribes to -- and the relay's own `OK true` makes
        that look like a delivery. Cross-pair reach is `git-inbox`, which the ladder selects
        once this rung refuses.
        """
        if not target_machine or self.pair is None:
            return ""
        if target_machine == self.machine:
            return ""
        members = self.pair.get("members") or []
        if target_machine in members:
            return ""
        return f"target-not-in-pair ({target_machine})"

    def refuse_oversize(self, text: str) -> str:
        """The size refusal, or "". Checked BEFORE anything is published, naming both sizes."""
        size = len(text.encode("utf-8"))
        if size > self.max_bytes:
            return f"message-too-large ({size} bytes, cap {self.max_bytes} bytes)"
        return ""

    async def send_channel(
        self,
        channel: str,
        text: str,
        *,
        sender: str = "",
        mentions: list[str] | None = None,
    ) -> SendOutcome:
        """Publish one `kind 9` to `channel`. T-2.4."""
        refusal = self.refuse_oversize(text)
        if refusal:
            return SendOutcome(sent=False, reason=refusal)
        if not await self.ensure_connection(None):
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        identity = self.identity(sender) if sender else self.daemon_identity
        connection = await self.connection_for(identity.name) if identity else None
        if identity is None or connection is None:
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        await self._ensure_channel(channel)
        event = build_chat_event(
            identity.keys(),
            self.channel_id(channel),
            text,
            now=self._now(),
            mention_pubkeys=mentions or [],
        )
        try:
            result = await connection.publish(event)
        except BuzzError as exc:
            return SendOutcome(sent=False, reason=str(exc))
        if not result.accepted:
            return SendOutcome(
                sent=False,
                channel=channel,
                event_id=result.event_id,
                reason=result.message or "relay-refused",
            )
        via = DELIVERED_VIA_MENTION if mentions else ""
        if via:
            self.delivered_via[DELIVERED_VIA_MENTION] += 1
        return SendOutcome(
            sent=True, delivered_via=via, event_id=result.event_id, channel=channel
        )

    async def send_direct(  # noqa: PLR0911, PLR0913  # one return per outcome; a keyword per mode
        self,
        recipient: str,
        recipient_pubkey: str,
        text: str,
        *,
        sender: str = "",
        dm_only: bool = False,
        one_shot: bool = False,
    ) -> SendOutcome:
        """D-13: NIP-17 gift wrap first, a `p`-mention in the recipient's machine channel after.

        The fallback is AUTOMATIC and is the resolution of D-13 -- Buzz issue #4677 reports the
        desktop client not subscribing to `kind 1059` at all, so a DM the relay accepted can
        still be a message nobody reads. `--dm-only` refuses the fallback naming the relay's own
        reason rather than silently downgrading a message the caller wanted private.

        `one_shot` is for a caller that built this channel for the send and closes it after
        (D-27): only the sender's socket is dialled and AUTH-ed, with no `REQ`, no `kind 0`
        announce and no group lifecycle, none of which a gift wrap needs. A fallback, when one
        runs, still goes through `send_channel` and its full start-up.
        """
        refusal = self.refuse_oversize(text)
        if refusal:
            return SendOutcome(sent=False, reason=refusal)
        # D-16, BEFORE any connection or publish: an unknown recipient key is a REFUSAL, and
        # this return is what makes the publish below unreachable for it. The code this
        # replaces carried on with an empty key, published an UNTAGGED `kind 9` into the
        # recipient's machine channel, and reported `delivered_via=mention` while every live
        # session on that machine received the message (audit F-26).
        if not recipient_pubkey:
            return SendOutcome(sent=False, reason=RECIPIENT_KEY_UNKNOWN_REASON)
        if one_shot and not self.enabled:
            return SendOutcome(sent=False, reason="buzz-not-configured")
        if not one_shot and not await self.ensure_connection(None):
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        identity = self.identity(sender) if sender else self.daemon_identity
        connection = await self.connection_for(identity.name) if identity else None
        if identity is None or connection is None:
            refused = (
                self.connection_errors.get(identity.name, "")
                if identity and one_shot
                else ""
            )
            return SendOutcome(
                sent=False, reason=self.last_error or refused or "buzz-unavailable"
            )

        try:
            wrap = await build_gift_wrap(
                identity.keys(), recipient_pubkey, text, now=self._now()
            )
            result = await connection.publish(wrap)
        except BuzzError as exc:
            result = PublishResult(event_id="", accepted=False, message=str(exc))
        if result.accepted:
            self.delivered_via[DELIVERED_VIA_DM] += 1
            return SendOutcome(
                sent=True,
                delivered_via=DELIVERED_VIA_DM,
                event_id=result.event_id,
            )
        reason = result.message or "relay-refused"

        if dm_only:
            return SendOutcome(
                sent=False, reason=f"dm-only-refused-fallback ({reason})"
            )
        machine = parse_identity_parts(recipient)[1] or self.machine
        prefix = str(
            self.config.get("machine_channel_prefix") or DEFAULT_MACHINE_CHANNEL_PREFIX
        )
        # The D-13 fallback is ALWAYS `p`-tagged to the resolved key: an untagged publish
        # here is the broadcast F-26 named, and the guard above is what rules it out.
        outcome = await self.send_channel(
            f"{prefix}{machine}",
            text,
            sender=sender,
            mentions=[recipient_pubkey],
        )
        if outcome.sent:
            return SendOutcome(
                sent=True,
                delivered_via=DELIVERED_VIA_MENTION,
                event_id=outcome.event_id,
                channel=outcome.channel,
                reason=f"dm-fallback ({reason})",
            )
        return outcome

    # -- issue-ref D-20 remote launch ---------------------------------------------------------

    async def send_launch_request(  # noqa: PLR0911  # one return per named delivery outcome
        self, target_machine: str, content: str, *, sender: str
    ) -> SendOutcome:
        """Publish one `KIND_LAUNCH_REQUEST` to `daemon@<target_machine>`. No fallback, ever.

        Unlike `send_direct` there is no mention fallback: a launch request on a public channel
        would be readable by every member and is exactly the weaker carrier D-20 refuses.
        """
        refusal = self.refuse_oversize(content) or self.refuse_cross_pair(
            target_machine
        )
        if refusal:
            return SendOutcome(sent=False, reason=refusal)
        daemon_name = daemon_identity_name(target_machine)
        recipient = self.resolve_recipient_pubkey(daemon_name)
        if not recipient:
            return SendOutcome(sent=False, reason=RECIPIENT_KEY_UNKNOWN_REASON)
        if not await self.ensure_connection(None):
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        identity = self.identity(sender)
        connection = await self.connection_for(identity.name) if identity else None
        if identity is None or connection is None:
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        event = build_launch_request_event(
            identity.keys(), recipient, content, now=self._now()
        )
        try:
            result = await connection.publish(event)
        except BuzzError as exc:
            return SendOutcome(sent=False, reason=str(exc))
        if not result.accepted:
            return SendOutcome(
                sent=False,
                event_id=result.event_id,
                reason=result.message or "relay-refused",
            )
        return SendOutcome(
            sent=True, delivered_via=DELIVERED_VIA_DM, event_id=result.event_id
        )

    async def send_launch_result(
        self, recipient_pubkey: str, reply: dict[str, Any]
    ) -> SendOutcome:
        """Publish the `KIND_LAUNCH_RESULT` reply from the daemon key to the request's signer."""
        if not await self.ensure_connection(None):
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        daemon = self.daemon_identity
        connection = self.connection
        if daemon is None or connection is None:
            return SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
        base = {"request_id", "decision", "reason", "launched_identity"}
        event = build_launch_result_event(
            daemon.keys(),
            reply.get("request_id"),
            str(reply.get("decision") or ""),
            str(reply.get("reason") or ""),
            reply.get("launched_identity"),
            now=self._now(),
            recipient_pubkey_hex=recipient_pubkey,
            extra={key: value for key, value in reply.items() if key not in base},
        )
        try:
            result = await connection.publish(event)
        except BuzzError as exc:
            return SendOutcome(sent=False, reason=str(exc))
        return SendOutcome(
            sent=result.accepted,
            event_id=result.event_id,
            reason="" if result.accepted else (result.message or "relay-refused"),
        )

    # -- T-2.P presence ----------------------------------------------------------------------

    async def publish_presence(
        self, records: list[dict[str, Any]]
    ) -> list[SendOutcome]:
        """One `kind 20001` per record. The Buzz leg of Phase 3's heartbeat (T-2.P).

        The seam Phase 3 calls: it passes `fleet_liveness`'s own records and this publishes
        them, so the tri-state has ONE source (`_live_slugs`, via the notifier) rather than a
        second liveness notion living here. A record whose identity has no key is published
        under the machine daemon's key, which is D-3's stated purpose for that key.
        """
        outcomes: list[SendOutcome] = []
        if not await self.ensure_connection(None):
            return [
                SendOutcome(sent=False, reason=self.last_error or "buzz-unavailable")
            ]
        now = self._now()
        for record in records:
            liveness = str(record.get("liveness") or LIVENESS_UNDETERMINED)
            name = str(record.get("identity") or daemon_identity_name(self.machine))
            identity = (
                self.identity(name)
                if record.get("source") == "presence-event"
                else self.identities.get(name) or self.daemon_identity
            )
            if identity is None:
                outcomes.append(
                    SendOutcome(sent=False, reason=self.last_error or "no-identity")
                )
                continue
            # Signed by `identity`, so published over `identity`'s own socket (ISSUE-REF).
            connection = await self.connection_for(identity.name)
            if connection is None:
                outcomes.append(
                    SendOutcome(
                        sent=False,
                        reason=self.connection_errors.get(identity.name)
                        or "buzz-unavailable",
                    )
                )
                continue
            event = build_presence_event(
                identity.keys(),
                liveness,
                json.dumps(record, sort_keys=True),
                now=now,
            )
            try:
                result = await connection.publish(event)
            except BuzzError as exc:
                outcomes.append(SendOutcome(sent=False, reason=str(exc)))
                continue
            outcomes.append(
                SendOutcome(
                    sent=result.accepted,
                    event_id=result.event_id,
                    reason=""
                    if result.accepted
                    else (result.message or "relay-refused"),
                )
            )
        self.last_presence_at = now
        return outcomes

    def presence_due(self, now: int | None = None) -> bool:
        """Whether `presence_interval_s` has elapsed. The daemon's tick is faster than this."""
        moment = self._now() if now is None else int(now)
        if self.last_presence_at is None:
            return True
        return moment - self.last_presence_at >= self.presence_interval_s

    # -- T-2.5 status ------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The `buzz` object the notifier's `status` JSON gains. T-2.5."""
        from ai_chassis.agent_mail_notifier import read_config_source

        return {
            "enabled": self.enabled,
            "reachable": self.reachable,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
            "relay_url": str(self.config.get("relay_url") or "")
            or (self.relay_url() if self.pair is not None else ""),
            "pair": (self.pair or {}).get("name"),
            "subscriptions": list(self.subscriptions),
            # ISSUE-REF: one authenticated socket per live identity, each with its own state.
            "connections": self.connection_states(),
            "delivered_via": dict(self.delivered_via),
            "group_refusals": list(self.group_refusals),
            # OQ-8: name -> the relay's channel UUID, read without writing the map.
            "channels": {
                name: self.channel_id(name, persist=False) for name in self.channels
            },
            "chat_subscribed": self.chat_subscribed,
            "identities": sorted(self.identities),
            # D-16: how many names a `--to` on this machine could resolve, and which of ours
            # the relay has accepted a profile for. Names only -- never a public key.
            "directory_names": sorted(load_pubkey_directory(self.pubkey_directory)),
            "metadata_announced": sorted(self.metadata_announced),
            # F-30: inbound events refused as inauthentic. A non-zero count is a broken or
            # hostile relay and has to be visible in `status`, not only in the log.
            "events_rejected": self.events_rejected,
            "presence_interval_s": self.presence_interval_s,
            "last_presence_at": self.last_presence_at,
            "config_source": read_config_source(),
            # T-6.4 (issue-ref D-25/D-26), for the LAST pass: REQs sent (0 between a socket's
            # open, a CLOSED and a lease renewal) and non-blocking reads that found nothing.
            "reqs_sent": self.counters.reqs_sent,
            "quiet_reads": self.counters.quiet_reads,
        }

    async def aclose(self) -> None:
        for name in sorted(self.connections):
            await self._close_identity(name)
