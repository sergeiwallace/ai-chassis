from __future__ import annotations

import asyncio
import os
import re
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from .client import (
    AdapterSettings,
    BoardClient,
    BoardSession,
    Invoker,
    MissingCredential,
    describe_failure,
)
from .inject_tools import register_inject_tools


#: Two of the notifier's default 10 s polls plus one tick's board work: how long a session that
#: started moments ago waits for the daemon to register its identity (ISSUE-REF).
SESSION_CREDENTIAL_WAIT_S = 30
CREDENTIAL_WAIT_ENV = "AGENT_MAIL_CREDENTIAL_WAIT_S"

#: Upstream's refusal for a send recipient its directory does not hold (ISSUE-REF).
UNREGISTERED_RECIPIENT = "are not registered in project"
REFUSED_RECIPIENTS = re.compile(r"local recipients (.+?) are not registered in project")


def _credential_wait_s() -> float:
    try:
        return max(0.0, float(os.environ[CREDENTIAL_WAIT_ENV]))
    except (KeyError, ValueError):
        return SESSION_CREDENTIAL_WAIT_S


#: This adapter process's one board session (ISSUE-REF). The adapter is a stdio child of one CC
#: session, so every tool call it serves shares one mTLS client and one MCP initialize instead of
#: paying both per call, the same reuse ISSUE-REF gave the notifier.
_board_session: BoardSession | None = None


def _board_invoker(settings: AdapterSettings) -> Invoker:
    """The process's shared `BoardSession`, replaced when the adapter's settings change."""
    global _board_session  # noqa: PLW0603 - one session per adapter process, by design
    if _board_session is not None and _board_session.settings != settings:
        # A rewritten client.env names another board or credential: never reuse a client
        # authenticated with the old one. Close it on the running loop and start fresh.
        asyncio.get_running_loop().create_task(_board_session.aclose())
        _board_session = None
    if _board_session is None:
        from ai_chassis.agent_mail_notifier import board_session_idle_s, load_config

        # ISSUE-REF T-4.6: an adapter sits idle between a session's tool calls, so its one
        # connection closes after `board_session_idle_s` and the next call reconnects.
        _board_session = BoardSession(
            settings, idle_s=board_session_idle_s(load_config())
        )
    return _board_session


def _client() -> BoardClient:
    settings = AdapterSettings.from_env()
    return BoardClient(
        settings,
        credential_wait_s=_credential_wait_s(),
        invoker=_board_invoker(settings),
    )


def _configured_project_key() -> str:
    from ai_chassis.agent_mail_notifier import load_config

    return str(load_config()["project_key"])


def _own_identity() -> str | None:
    """This session's board name, resolved as ``agent-mail-notifier read`` resolves it.

    The adapter is a stdio child of the session's ``claude`` process, so the notifier's ancestry
    probe names the same session here. A session the daemon has not registered yet gets the name
    the daemon registers it under, the one name the credential wait applies to.
    """
    from ai_chassis import agent_mail_notifier as notifier
    from ai_chassis.session_address import Address, board_name

    identities = notifier._read_json(
        notifier.StateDir(notifier.state_root()).identities, {}
    )
    slug = notifier.Notifier._own_session_slug()
    if slug is None:
        machine = identities.get(notifier.MACHINE_KEY)
        return machine if isinstance(machine, str) else None
    registered = identities.get(slug)
    if isinstance(registered, str):
        return registered
    return board_name(Address(slug=slug, machine=notifier.machine_label()))


def _resolve(
    client: BoardClient, project_key: str | None, agent_name: str | None
) -> tuple[str, str]:
    """The (project_key, agent_name) a tool acts as; a guess is refused at once (ISSUE-REF).

    Only this session's own identity is waited for: any other name this machine holds no
    credential for will not appear, so waiting for it only delays the refusal.
    """
    configured = _configured_project_key()
    key = project_key or configured
    held = client.credential_names(key)
    if not held and key != configured:
        raise MissingCredential(
            f"no local Agent Mail credential under project_key {key!r}: the configured key is "
            f"{configured!r} ([agent_mail_notify].project_key); omit project_key to use it"
        )
    if agent_name is not None and agent_name in held:
        return key, agent_name
    own = _own_identity()
    if own is None:
        raise MissingCredential(
            "no local Agent Mail credential: this process is in no titled Claude Code session "
            "and the machine has no board identity yet, so no <AI_HOST>--<session title> name "
            "resolves; check that the notifier daemon is running (agent-mail-notifier status)"
        )
    if agent_name is None or agent_name == own:
        return key, own
    raise MissingCredential(
        f"no local Agent Mail credential for {agent_name!r} in {key!r}; this session is "
        f"{own!r}, a name of the form <AI_HOST>--<session title> that this machine's notifier "
        "daemon registers; omit agent_name to act as it"
    )


def _record_receipt(result: Any, agent_name: str, message_id: int, flag: str) -> Any:
    """Record a receipt the board accepted where the notifier daemon reads it (ISSUE-REF).

    The daemon drops ids in this ledger from every pass, so a pass whose fetch predates this
    receipt cannot re-list the message (ISSUE-REF). Accepted means the board's reply carries
    ``flag`` true for this id, the CLI's own test. A ledger that cannot be written never fails
    the tool: the board already holds the receipt.
    """
    from ai_chassis import agent_mail_notifier as notifier

    if any(
        node.get("message_id") == message_id and node.get(flag) is True
        for node in notifier._walk(result)
    ):
        try:
            notifier.record_receipt(agent_name, message_id, ack=flag == "acknowledged")
        except OSError:
            pass
    return result


def _described(summary: str) -> str:
    return (
        f"{summary}\n\n`project_key` defaults to this machine's configured key, "
        f"[agent_mail_notify].project_key ({_configured_project_key()!r} here). The agent name "
        "defaults to this session's own board identity, <AI_HOST>--<session title> (e.g. "
        "example-project-ec2--ISSUE-REF), which the notifier daemon registers. Omit both: a value this "
        "machine holds no credential for fails at once."
    )


async def register_board_agent(
    program: str,
    model: str,
    project_key: str | None = None,
    name_hint: str | None = None,
    task_description: str = "",
) -> Any:
    return await _client().register_agent(
        project_key=project_key or _configured_project_key(),
        program=program,
        model=model,
        name_hint=name_hint,
        task_description=task_description,
    )


async def send_board_message(  # noqa: PLR0913, PLR0917 - public MCP schema is intentionally flat
    to: list[str],
    subject: str,
    body_md: str,
    project_key: str | None = None,
    sender_name: str | None = None,
    thread_id: str | None = None,
    importance: str = "normal",
    ack_required: bool = False,
) -> Any:
    client = _client()
    project_key, sender_name = _resolve(client, project_key, sender_name)
    try:
        return await client.call_as_agent(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": sender_name,
                "to": to,
                "subject": subject,
                "body_md": body_md,
                "thread_id": thread_id,
                "importance": importance,
                "ack_required": ack_required,
            },
            project_key=project_key,
            agent_name=sender_name,
            token_argument="sender_token",
        )
    except RuntimeError as exc:
        if UNREGISTERED_RECIPIENT not in str(exc):
            raise
        reason = await _unregistered_recipient_reason(client, project_key, to, str(exc))
        raise RuntimeError(f"{exc} {reason}") from exc


async def _unregistered_recipient_reason(
    client: BoardClient, project_key: str, to: list[str], refusal: str
) -> str:
    """Why upstream does not know a recipient, for a refusal that otherwise reads as a key bug.

    Boards are per machine pair (ISSUE-REF): a session registered only on another pair's board
    is absent from this one, and the refusal named neither board. This names the board asked
    (host and port only), the recipient host's sessions it does list, and the git-inbox route.
    Only the recipients upstream's own text names are explained, so a recipient the board holds
    is never routed elsewhere, even when the directory cannot be read.
    """
    from ai_chassis.session_address import address_from_board_name, display_name

    parsed = urlparse(client.settings.url)
    board = (
        parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    )
    match = REFUSED_RECIPIENTS.search(refusal)
    named = (
        {part.strip().casefold() for part in match[1].split(",")} if match else set()
    )
    refused = [recipient for recipient in to if recipient.casefold() in named] or to
    try:
        payload = await client.read_resource(
            f"resource://agents/{project_key}?format=json"
        )
        agents = payload.get("agents") if isinstance(payload, dict) else None
        if not isinstance(agents, list):
            raise ValueError("the directory carries no agents list")
        names = [
            row["name"]
            for row in agents
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        ]
    except Exception as exc:  # noqa: BLE001 - the refusal must still name the route
        names = None
        unread = describe_failure(exc)
    listed = {name.casefold() for name in names or []}
    parts = [
        f"This session asked the Agent Mail board at {board}; boards are per machine pair, "
        "so a session registered on another pair's board is not on this one."
    ]
    if names is None:
        parts.append(f"Its directory could not be read ({unread}).")
    for recipient in refused:
        if recipient.casefold() in listed:
            continue
        address = address_from_board_name(recipient)
        if address is None:
            parts.append(
                f"{recipient!r} is not a <host>--<session> name, so no host or git-inbox "
                "route follows from it."
            )
            continue
        if names is not None:
            peers = sorted(
                display_name(name)
                for name in names
                if (peer := address_from_board_name(name)) is not None
                and peer.machine.casefold() == address.machine.casefold()
            )
            parts.append(
                f"Registered here from {address.machine}: {', '.join(peers)}."
                if peers
                else f"No {address.machine} session is registered on this board."
            )
        parts.append(f"Cross-pair route: git-inbox send --to {address}.")
    return " ".join(parts)


async def reply_board_message(
    message_id: int,
    body_md: str,
    project_key: str | None = None,
    sender_name: str | None = None,
) -> Any:
    client = _client()
    project_key, sender_name = _resolve(client, project_key, sender_name)
    return await client.call_as_agent(
        "reply_message",
        {
            "project_key": project_key,
            "message_id": message_id,
            "sender_name": sender_name,
            "body_md": body_md,
        },
        project_key=project_key,
        agent_name=sender_name,
        token_argument="sender_token",
    )


async def fetch_board_inbox(
    project_key: str | None = None,
    agent_name: str | None = None,
    limit: int = 20,
    urgent_only: bool = False,
    include_bodies: bool = True,
) -> Any:
    client = _client()
    project_key, agent_name = _resolve(client, project_key, agent_name)
    return await client.call_as_agent(
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": agent_name,
            "limit": limit,
            "urgent_only": urgent_only,
            "include_bodies": include_bodies,
        },
        project_key=project_key,
        agent_name=agent_name,
        token_argument="registration_token",
    )


async def mark_board_message_read(
    message_id: int,
    project_key: str | None = None,
    agent_name: str | None = None,
) -> Any:
    # Send the receipt through the common gateway mutation path. The local ledger
    # below is a receipt cache; it must not publish a second board stream frame.
    client = _client()
    project_key, agent_name = _resolve(client, project_key, agent_name)
    result = await client.call_as_agent(
        "mark_message_read",
        {
            "project_key": project_key,
            "agent_name": agent_name,
            "message_id": message_id,
        },
        project_key=project_key,
        agent_name=agent_name,
        token_argument="registration_token",
    )
    return _record_receipt(result, agent_name, message_id, "read")


async def acknowledge_board_message(
    message_id: int,
    project_key: str | None = None,
    agent_name: str | None = None,
) -> Any:
    # The gateway journals this call for CLI, board and adapter clients alike.
    client = _client()
    project_key, agent_name = _resolve(client, project_key, agent_name)
    result = await client.call_as_agent(
        "acknowledge_message",
        {
            "project_key": project_key,
            "agent_name": agent_name,
            "message_id": message_id,
        },
        project_key=project_key,
        agent_name=agent_name,
        token_argument="registration_token",
    )
    return _record_receipt(result, agent_name, message_id, "acknowledged")


async def poll_board_inbox(
    project_key: str | None = None,
    agent_name: str | None = None,
    after_message_id: int | None = None,
    timeout_seconds: float = 30,
) -> Any:
    client = _client()
    project_key, agent_name = _resolve(client, project_key, agent_name)
    return await client.poll_inbox(
        project_key=project_key,
        agent_name=agent_name,
        after_message_id=after_message_id,
        timeout_seconds=timeout_seconds,
    )


async def search_board_messages(
    query: str,
    project_key: str | None = None,
    agent_name: str | None = None,
    limit: int = 20,
) -> Any:
    client = _client()
    project_key, agent_name = _resolve(client, project_key, agent_name)
    return await client.call_as_agent(
        "search_messages",
        {
            "project_key": project_key,
            "agent_name": agent_name,
            "query": query,
            "limit": limit,
        },
        project_key=project_key,
        agent_name=agent_name,
        token_argument="registration_token",
    )


def create_server() -> FastMCP:
    """Build the legacy catalog without opening a board session or token store."""
    server = FastMCP("secure-agent-mail")
    register_inject_tools(server)
    server.tool(
        description=(
            "Create a NEW, non-session board identity and retain its token only in the local "
            "adapter. The notifier daemon registers every live session as "
            "<AI_HOST>--<session title> by itself, so this tool is never for a session's own "
            "identity: registering one mints a duplicate persistent identity. `project_key` "
            "defaults to [agent_mail_notify].project_key."
        )
    )(register_board_agent)
    server.tool(
        description=_described(
            "Send one coordination hand-off through the board fallback channel."
        )
    )(send_board_message)
    server.tool(
        description=_described(
            "Reply to a board message without accepting any machine-origin argument."
        )
    )(reply_board_message)
    server.tool(
        description=_described(
            "Fetch this session's inbox, bodies included. Same as ``agent-mail-notifier read <id>``. "
            "A call made before the daemon's first poll for this session waits for that "
            "registration."
        )
    )(fetch_board_inbox)
    server.tool(
        description=_described(
            "Mark one received message read for this agent, so it stops notifying and escalating. "
            "Never mark a message read whose BODY you have not read (fetch_board_inbox or "
            "``agent-mail-notifier read <id>``): the subject alone is not the message."
        )
    )(mark_board_message_read)
    server.tool(
        description=_described(
            "Acknowledge one received message (it is marked read too), e.g. one sent ack_required. "
            "Never acknowledge a message whose BODY you have not read."
        )
    )(acknowledge_board_message)
    server.tool(
        description=_described(
            "Wait up to 60 seconds for an authorized inbox message newer than the cursor."
        )
    )(poll_board_inbox)
    server.tool(
        description=_described(
            "Search only the transport-authorized storage namespace."
        )
    )(search_board_messages)
    return server


mcp = create_server()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
