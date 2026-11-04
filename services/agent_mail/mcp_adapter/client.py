from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import ssl
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError


class BoardAccessDenied(PermissionError):
    pass


class MissingCredential(BoardAccessDenied):
    """This machine holds no token for the agent: not registered yet, or a name it does not own."""


FORBIDDEN_IDENTITY_FIELDS = frozenset(
    {"origin", "origin_machine", "machine_id", "ai_host", "ai_machine_id"}
)
Invoker = Callable[[str, dict[str, Any], "AdapterSettings"], Awaitable[Any]]
RetryObserver = Callable[[str, str], None]
#: Told why a `BoardSession` connection was discarded while its owner task still held it.
LossObserver = Callable[[str], None]
_TOKEN_STORE_LOCK = threading.Lock()
MAX_POLL_TIMEOUT_SECONDS = 60
#: The MCP method a resource read travels under through the invoker seam. No tool name carries a
#: `/`, so it cannot collide with one.
READ_RESOURCE = "resources/read"
#: Tools that only read, so a repeat after a lost response cannot deliver or mint anything twice.
RETRY_SAFE_TOOLS = frozenset({"fetch_inbox", READ_RESOURCE})
TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504})
#: `BoardSession`'s wait after consecutive failed connects: 1 s, doubling, never above the cap.
RECONNECT_BACKOFF_BASE_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0
#: How long a `BoardSession` call waits for its reply. A reply whose body dies after its headers is
#: logged by mcp's transport and never answered, so without a bound the call, and the notifier tick
#: awaiting it, waited forever. Twice the HTTP read timeout, which already bounds a slow upstream.
BOARD_CALL_TIMEOUT_S = 60.0
#: The JSON-RPC error code `_CallIsolatingTransport` answers a request's own transient failure
#: with, in the implementation-defined range. Never sent on the wire: it stays in this process.
ISOLATED_FAILURE_CODE = -32050
#: The transport errors that belong to ONE request. A failed connect is not among them: it means
#: the board is unreachable, so it still ends the session and the reconnect backoff applies.
ISOLATED_TRANSPORT_ERRORS = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)
#: What a call raises once the MCP session's in-memory streams are closed under it: a send on the
#: closed write stream, a send its reader closed, a read past the end. None of them can heal, so
#: the connection is discarded (ISSUE-REF).
CLOSED_STREAM_ERRORS = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(bearer|[a-z_-]*(?:token|secret|password|passwd|api[_-]?key|authorization))"
    r"(\s*[=:]\s*|\s+)(?:bearer\s+)?\S+",
    re.IGNORECASE,
)
MAX_DETAIL_LEAVES = 5
MAX_LEAF_MESSAGE_CHARS = 160
#: Long enough for upstream's error type and its first sentence, which names the remedy.
MAX_UPSTREAM_REASON_CHARS = 240


def client_env_path() -> Path:
    """Where install.sh's `_install_agent_mail_client` writes this machine's client config."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "ai-harness" / "agent-mail" / "client.env"


def _read_client_env(path: Path, names: set[str]) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in names:
            values[name] = value.strip()
    return values


@dataclass(frozen=True)
class AdapterSettings:
    url: str
    cert_file: str
    key_file: str
    ca_file: str
    agent_tokens_file: str

    @classmethod
    def from_env(cls) -> "AdapterSettings":
        """The five from the environment, else from the installer-written client.env.

        Only the supervised unit and an interactive shell carry the five, so a Claude Code
        session (which starts this adapter) and a tool-call shell had none (ISSUE-REF).
        client.env is what the unit itself reads, so it is the same truth, not a second one.
        An environment value always wins. The file is parsed, never executed.
        """
        required = {
            "url": "AGENT_MAIL_URL",
            "cert_file": "AGENT_MAIL_CLIENT_CERT",
            "key_file": "AGENT_MAIL_CLIENT_KEY",
            "ca_file": "AGENT_MAIL_CA_CERT",
            "agent_tokens_file": "AGENT_MAIL_AGENT_TOKENS",
        }
        client_env = client_env_path()
        from_file = _read_client_env(client_env, set(required.values()))
        values = {
            field: os.environ.get(env) or from_file.get(env, "")
            for field, env in required.items()
        }
        missing = [env for field, env in required.items() if not values[field]]
        if missing:
            raise BoardAccessDenied(
                "missing local adapter configuration: "
                + ", ".join(missing)
                + f" (not in the environment or {client_env})"
            )
        parsed = urlparse(values["url"])
        if parsed.scheme != "https" or not parsed.hostname:
            raise BoardAccessDenied(
                "AGENT_MAIL_URL must be an absolute HTTPS URL with a host"
            )
        return cls(**values)


class AgentTokenStore:
    """Machine-local Agent Mail credentials that never enter the MCP schema."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def save(self, project_key: str, agent_name: str, token: str) -> None:
        with _TOKEN_STORE_LOCK:
            records = self._read()
            records[self._key(project_key, agent_name)] = token
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(
                prefix=".agent-tokens-", dir=self.path.parent, text=True
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(records, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def get(self, project_key: str, agent_name: str) -> str:
        token = self._read().get(self._key(project_key, agent_name))
        if not token:
            raise MissingCredential(
                f"no local Agent Mail credential for {agent_name!r} in {project_key!r}; this "
                "machine's notifier daemon registers every live session as "
                "<AI_HOST>--<session title> (agent-mail-notifier status)"
            )
        return token

    def names(self, project_key: str) -> list[str]:
        """The agent names this store holds a credential for in `project_key`."""
        keys = (json.loads(key) for key in self._read())
        return [name for project, name in keys if project == project_key]

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        if self.path.stat().st_mode & 0o077:
            raise BoardAccessDenied(
                "local Agent Mail credential file must have mode 0600"
            )
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data.items()
        ):
            raise BoardAccessDenied("invalid local Agent Mail credential file")
        return data

    @staticmethod
    def _key(project_key: str, agent_name: str) -> str:
        return json.dumps([project_key, agent_name], separators=(",", ":"))


class BoardClient:
    def __init__(
        self,
        settings: AdapterSettings,
        *,
        invoker: Invoker | None = None,
        on_retry: RetryObserver | None = None,
        credential_wait_s: float = 0,
    ) -> None:
        self.settings = settings
        self._invoker = invoker or _invoke_remote_tool
        self._on_retry = on_retry
        self._tokens = AgentTokenStore(settings.agent_tokens_file)
        #: How long `call_as_agent` waits for a credential that is not on disk yet. The notifier
        #: daemon is the one registrar of session identities, so a session that calls before the
        #: daemon's next poll has registered it waits for that poll instead of racing it with a
        #: second registration of the same name (ISSUE-REF).
        self._credential_wait_s = credential_wait_s

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """One board call. A read-only tool that fails transiently is retried ONCE (ISSUE-REF).

        Measured on example-project-ec2: about one call in 36,000 stalls upstream past the gateway's 30 s
        read timeout and comes back as a 500 or a client ReadTimeout, and the very next call
        succeeds. One retry absorbs that; a board that is really down fails the retry as well,
        so it is still reported in the same poll.
        """
        forbidden = _find_forbidden_identity_field(arguments)
        if forbidden:
            raise BoardAccessDenied(
                f"caller-supplied identity field is forbidden: {forbidden}"
            )
        try:
            return await self._call_once(name, arguments)
        except BoardAccessDenied:
            raise
        except BaseException as exc:
            if name not in RETRY_SAFE_TOOLS or not is_transient_failure(exc):
                raise
            if self._on_retry is not None:
                self._on_retry(name, describe_failure(exc))
        return await self._call_once(name, arguments)

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self._invoker(name, arguments, self.settings)
        except BoardAccessDenied:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise BoardAccessDenied("board authorization denied") from exc
            raise
        except BaseExceptionGroup as exc:
            # A gateway admission refusal happens during `session.initialize()`, inside the
            # anyio task group, so it reaches us wrapped and the branch above cannot see it.
            if _is_authorization_refusal(exc):
                raise BoardAccessDenied("board authorization denied") from exc
            raise

    async def read_resource(self, uri: str) -> Any:
        """Read one upstream MCP resource and return its JSON payload.

        Resources need no agent token, which is what makes ``resource://agents/{project_key}`` the
        directory a machine can resolve a PEER's identity through: ``whois`` authenticates as its
        target, so it refuses every caller that does not hold the target's token (ISSUE-REF).
        """
        result = await self.call_tool(READ_RESOURCE, {"uri": uri})
        contents = result.get("contents") if isinstance(result, dict) else None
        for item in contents if isinstance(contents, list) else []:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                return json.loads(text)
        raise RuntimeError("Agent Mail resource read returned no text content")

    async def read_resource_as_agent(
        self, uri: str, *, project_key: str, agent_name: str
    ) -> Any:
        """Read a resource upstream authenticates as ``agent_name``, such as its outbox.

        Upstream takes a resource's credential as the ``agent_token`` query parameter. The URI
        carrying it is built here and goes only to the invoker; callers never see or log it.
        """
        separator = "&" if "?" in uri else "?"
        token = urlencode({"agent_token": self._tokens.get(project_key, agent_name)})
        return await self.read_resource(f"{uri}{separator}{token}")

    async def register_agent(
        self,
        *,
        project_key: str,
        program: str,
        model: str,
        name_hint: str | None = None,
        task_description: str = "",
    ) -> Any:
        result = await self.call_tool(
            "create_agent_identity",
            {
                "project_key": project_key,
                "program": program,
                "model": model,
                "name_hint": name_hint,
                "task_description": task_description,
                "return_registration_token": True,
            },
        )
        agent_name = _find_named_value(result, "name")
        token = _find_named_value(result, "registration_token")
        if not agent_name or not token:
            raise RuntimeError("Agent Mail did not return a new identity credential")
        self._tokens.save(project_key, agent_name, token)
        return _redact_credentials(result)

    def has_credential(self, project_key: str, agent_name: str) -> bool:
        """Whether this machine can authenticate as ``agent_name``. Reads no token out."""
        try:
            self._tokens.get(project_key, agent_name)
        except BoardAccessDenied:
            return False
        return True

    def credential_names(self, project_key: str) -> list[str]:
        """The agent names this machine can authenticate as in ``project_key``. Reads no token."""
        return self._tokens.names(project_key)

    async def call_as_agent(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        project_key: str,
        agent_name: str,
        token_argument: str,
    ) -> Any:
        authenticated = dict(arguments)
        authenticated[token_argument] = await self._credential(project_key, agent_name)
        return await self.call_tool(name, authenticated)

    async def _credential(self, project_key: str, agent_name: str) -> str:
        deadline = time.monotonic() + self._credential_wait_s
        while True:
            try:
                return self._tokens.get(project_key, agent_name)
            except MissingCredential:
                if time.monotonic() >= deadline:
                    if not self._credential_wait_s:
                        raise
                    raise MissingCredential(
                        f"no local Agent Mail credential for {agent_name!r} in "
                        f"{project_key!r} after waiting {self._credential_wait_s:g}s; this "
                        "machine's notifier daemon registers every live session as "
                        "<AI_HOST>--<session title> on its next poll, so either the name is "
                        "not this session's or the daemon is not running "
                        "(agent-mail-notifier status)"
                    ) from None
            await asyncio.sleep(1)

    async def poll_inbox(
        self,
        *,
        project_key: str,
        agent_name: str,
        after_message_id: int | None,
        timeout_seconds: float,
        poll_interval_seconds: float = 1,
    ) -> Any:
        """Bounded, cancellation-safe polling over the authorized inbox surface."""
        if (
            not 0 <= timeout_seconds <= MAX_POLL_TIMEOUT_SECONDS
            or poll_interval_seconds <= 0
        ):
            raise BoardAccessDenied(
                "poll timeout must be 0..60 seconds and interval positive"
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = await self.call_as_agent(
                "fetch_inbox",
                {"project_key": project_key, "agent_name": agent_name, "limit": 20},
                project_key=project_key,
                agent_name=agent_name,
                token_argument="registration_token",
            )
            if _contains_message_after(result, after_message_id):
                return _filter_messages_after(result, after_message_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"messages": [], "timed_out": True}
            await asyncio.sleep(min(poll_interval_seconds, remaining))


def _leaves(exc: BaseException) -> list[BaseException]:
    """The non-group exceptions inside `exc`, depth first and in order; `[exc]` for a plain one."""
    if not isinstance(exc, BaseExceptionGroup):
        return [exc]
    found: list[BaseException] = []
    for member in exc.exceptions:
        found.extend(_leaves(member))
    return found


def _is_authorization_refusal(group: BaseExceptionGroup) -> bool:
    """True when every leaf of `group` is a 401/403 from the board."""
    leaves = _leaves(group)
    return bool(leaves) and all(
        isinstance(leaf, httpx.HTTPStatusError)
        and leaf.response.status_code in {401, 403}
        for leaf in leaves
    )


def is_transient_failure(exc: BaseException) -> bool:
    """True when every leaf is a transport error or a 5xx: worth one retry, never a 4xx."""
    leaves = _leaves(exc)
    return bool(leaves) and all(
        isinstance(leaf, httpx.TransportError)
        or (
            isinstance(leaf, httpx.HTTPStatusError)
            and leaf.response.status_code in TRANSIENT_STATUS_CODES
        )
        for leaf in leaves
    )


def _describe_leaf(leaf: BaseException) -> str:
    if isinstance(leaf, httpx.HTTPStatusError):
        # Its message is mostly the request URL, which must never reach a log or a marker.
        return f"HTTPStatusError status={leaf.response.status_code}"
    text = type(leaf).__name__
    message = _CREDENTIAL.sub(r"\1\2<redacted>", _URL.sub("<url>", str(leaf)))
    message = " ".join(message.split())
    if message:
        if len(message) > MAX_LEAF_MESSAGE_CHARS:
            message = message[: MAX_LEAF_MESSAGE_CHARS - 1] + "…"
        text += f": {message}"
    cause = leaf.__cause__ or leaf.__context__
    if cause is not None and type(cause) is not type(leaf):
        text += f" (from {type(cause).__module__}.{type(cause).__name__})"
    return text


def describe_failure(exc: BaseException) -> str:
    """Name what a board call died of, leaf by leaf, safe for a journal line or `inbox.json`.

    ISSUE-REF: the notifier used to record only `type(exc).__name__`, so every drop read
    "ExceptionGroup" and its cause was in nobody's log. Each leaf gives its type, its message
    with any URL replaced by `<url>` and any `token=`/`Authorization:`/`Bearer`-shaped value
    replaced by `<redacted>`, and the type of its cause (the TLS or socket error under an httpx
    wrapper); an HTTP status error gives its status only.
    """
    leaves = _leaves(exc)
    parts = [_describe_leaf(leaf) for leaf in leaves[:MAX_DETAIL_LEAVES]]
    if len(leaves) > MAX_DETAIL_LEAVES:
        parts.append(f"+{len(leaves) - MAX_DETAIL_LEAVES} more")
    if not isinstance(exc, BaseExceptionGroup):
        return parts[0]
    return f"{type(exc).__name__}[{'; '.join(parts)}]"


def _find_forbidden_identity_field(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if normalized in FORBIDDEN_IDENTITY_FIELDS:
                return str(key)
            found = _find_forbidden_identity_field(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_forbidden_identity_field(nested)
            if found:
                return found
    return None


def _find_named_value(value: Any, target: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(target)
        if isinstance(candidate, str) and candidate:
            return candidate
        for nested in value.values():
            found = _find_named_value(nested, target)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_named_value(nested, target)
            if found:
                return found
    return None


def inbox_message_id(node: dict[str, Any]) -> int | None:
    """The id of a message node in a tool result, or None when ``node`` is not one.

    Upstream's ``fetch_inbox`` rows (``_message_to_dict``) carry the id as ``id``; other tools
    and older fakes say ``message_id``. A bare ``id`` counts only beside a ``subject``, because
    project and agent nodes carry an ``id`` too.
    """
    for key in ("message_id", "id"):
        value = node.get(key)
        if key == "id" and "subject" not in node:
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _contains_message_after(value: Any, after_message_id: int | None) -> bool:
    if isinstance(value, dict):
        message_id = inbox_message_id(value)
        if message_id is not None and (
            after_message_id is None or message_id > after_message_id
        ):
            return True
        return any(
            _contains_message_after(item, after_message_id) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_message_after(item, after_message_id) for item in value)
    return False


def _filter_messages_after(value: Any, after_message_id: int | None) -> Any:
    """Drop any message node at or before the cursor, mirroring `_contains_message_after`'s walk."""
    if isinstance(value, dict):
        message_id = inbox_message_id(value)
        if message_id is not None:
            if after_message_id is not None and message_id <= after_message_id:
                return None
            return value
        return {
            key: _filter_messages_after(nested, after_message_id)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [
            filtered
            for filtered in (
                _filter_messages_after(item, after_message_id) for item in value
            )
            if filtered is not None
        ]
    return value


def _redact_credentials(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_credentials(nested)
            for key, nested in value.items()
            if str(key) not in {"registration_token", "sender_token"}
        }
    if isinstance(value, list):
        return [_redact_credentials(nested) for nested in value]
    return value


def http2_available() -> bool:
    """Whether httpx can speak HTTP/2 here: it needs the `h2` package (`httpx[http2]`)."""
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


class _CallIsolatingTransport(httpx.AsyncBaseTransport):
    """Turn ONE request's transient failure into that request's own JSON-RPC error.

    MCP's streamable HTTP client runs every POST as a task in one task group, so a transport
    error or a 5xx on any single request ends the group, the session and every call in flight
    with it. With calls concurrent (ISSUE-REF D-15) that turned one blip into N failed calls and
    N retries. Here the failing request is answered, locally, with a JSON-RPC error carrying a
    key, and the original exception is parked under that key for `_run_call` to re-raise, so
    only that call fails (and is retried once by `BoardClient`). `initialize`, a failed connect
    and anything that is not a transient failure pass through unchanged and still end the
    session.

    A message that is not a request (a notification such as `notifications/initialized`, or a
    reply to a server request) is POSTed INLINE by the transport's writer task, which logs a
    failure of that POST and returns, closing both MCP streams without failing any task group
    (ISSUE-REF). Every later call on the session then dies with a bare `ClosedResourceError`
    while its owner task lives on. Here the transport sees that POST fail first, so
    ``on_inline_failure`` is told which message failed and how, and the session is discarded.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        failures: dict[str, BaseException],
        on_inline_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        self._inner = inner
        self._failures = failures
        self._on_inline_failure = on_inline_failure

    async def __aenter__(self) -> "_CallIsolatingTransport":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._inner.__aexit__(*exc)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self._inner.handle_async_request(request)
        except ISOLATED_TRANSPORT_ERRORS as exc:
            isolated = self._isolate(request, exc)
            if isolated is None:
                self._inline_failed(request, exc)
                raise
            return isolated
        except Exception as exc:
            self._inline_failed(request, exc)
            raise
        if response.status_code not in TRANSIENT_STATUS_CODES:
            # The writer returns quietly on a 2xx and on a 404 (its "session terminated" path),
            # and raises on anything else.
            if not response.is_success and response.status_code != 404:  # noqa: PLR2004
                self._inline_failed(
                    request,
                    httpx.HTTPStatusError(
                        f"status {response.status_code}",
                        request=request,
                        response=response,
                    ),
                )
            return response
        await response.aread()
        failure = httpx.HTTPStatusError(
            f"Server error {response.status_code}", request=request, response=response
        )
        isolated = self._isolate(request, failure)
        if isolated is None:
            self._inline_failed(request, failure)
        return isolated or response

    def _inline_failed(self, request: httpx.Request, failure: BaseException) -> None:
        """Report a failed POST of a non-request message: the failure that ends the session."""
        if self._on_inline_failure is None:
            return
        message = _request_message(request)
        if message is None or ("method" in message and "id" in message):
            return
        method = message.get("method")
        label = method if isinstance(method, str) else "a reply to a server request"
        self._on_inline_failure(label, describe_failure(failure))

    def _isolate(
        self, request: httpx.Request, failure: BaseException
    ) -> httpx.Response | None:
        message = _request_message(request)
        if (
            message is None
            or "id" not in message
            or message.get("method") == "initialize"
        ):
            return None
        key = str(message["id"])
        self._failures[key] = failure
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {
                    "code": ISOLATED_FAILURE_CODE,
                    "message": "isolated transient failure",
                    "data": key,
                },
            },
            request=request,
        )


def _request_message(request: httpx.Request) -> dict[str, Any] | None:
    """The JSON-RPC message a POST carries, or None when it carries no JSON object."""
    try:
        message = json.loads(request.content)
    except (ValueError, httpx.RequestNotRead):
        return None
    return message if isinstance(message, dict) else None


@dataclass
class _Connection:
    """One connect of a `BoardSession`: its owner task, its MCP session and the calls on it."""

    stop: asyncio.Event
    owner: asyncio.Task[None] | None = None
    calls: Any | None = None
    session: ClientSession | None = None
    failures: dict[str, BaseException] = field(default_factory=dict)
    pending: set[asyncio.Future[Any]] = field(default_factory=set)
    #: Set by the idle close, or by a discard, before its teardown yields; never cleared.
    closing: bool = False
    #: Why a discard ended this connection (ISSUE-REF), None when nothing did.
    lost: str | None = None
    #: The httpx transport under the isolation wrapper, whose pool `open_connections` reads.
    transport: httpx.AsyncBaseTransport | None = None

    def usable(self) -> bool:
        return (
            not self.closing
            and self.calls is not None
            and self.owner is not None
            and not self.owner.done()
        )


class BoardSession:
    """ONE mTLS HTTPS client and ONE initialized MCP session, reused by every call until closed.

    ISSUE-REF: opening both per call cost a TLS handshake plus four POSTs (`initialize`,
    `notifications/initialized`, the call, and the `tools/list` a fresh session's empty schema
    cache sends after it), so the notifier's 78 serial `fetch_inbox` calls held a tick at 32 s.
    One session serves every identity: the gateway admits the MACHINE by the client certificate
    on the TLS connection, and an identity's token is an argument of each call. Upstream runs
    stateless (`stateless_http=True`), so no server-side session can expire under it.

    The client, transport and session live inside one owner task, because their anyio task
    groups must be entered and exited by the same task; each call runs as a task in a group the
    owner holds. A transient transport error or 5xx on ONE call fails that call alone
    (`_CallIsolatingTransport`), so concurrent calls on the session do not share one blip
    (ISSUE-REF D-15). Any other failure inside the session ends it, and every call still
    waiting gets that same exception, classified exactly as the per-call path classified it.
    The next call reconnects. A CONNECT that fails backs off (1 s, doubling, capped at
    `RECONNECT_BACKOFF_MAX_S`) and a call inside the backoff re-raises that failure without
    touching the network. It is bound to the event loop of its first call; callable as an
    `Invoker`.

    ``http2`` asks for HTTP/2 when `h2` is importable; the edge's ALPN decides, and httpx's
    HTTP/1.1 pool carries the calls otherwise. ``on_connect`` is told the negotiated protocol
    once per connect (the first response's ``http_version``).

    ``idle_s`` (ISSUE-REF T-4.6): a connection that has carried no call for that long closes
    its client and MCP session while the loop waits, instead of holding a TLS connection open
    for hours (the `CLOSE-WAIT` socket measured on example-project-ec2); the next call reconnects. The
    close marks its connection unusable before it yields, so a call arriving mid-close opens a
    new one rather than landing on the one being torn down. None never closes.

    A connection whose MCP streams closed while its owner task lives on is discarded the same
    way (ISSUE-REF): mcp's transport writer and receive loop each end on a failure they only log,
    and the session they leave answers every call with `ClosedResourceError` for as long as it is
    held (85 minutes on example-client-host-mac, with no reconnect). The transport reports the failed inline POST
    that causes it, and a call that meets a closed stream (`CLOSED_STREAM_ERRORS`) reports any
    closer it cannot see. Either one discards the connection: the calls already on it fail, the
    next call reconnects, and ``on_lost`` is told why, once per discarded connection.

    ``call_timeout_s`` bounds how long each call waits for its reply (`BOARD_CALL_TIMEOUT_S`); a
    call past it fails with mcp's request-timeout `McpError` and the session stays up.
    """

    def __init__(  # noqa: PLR0913  # one keyword per transport knob; each defaults to off
        self,
        settings: AdapterSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        http2: bool = False,
        on_connect: Callable[[str], None] | None = None,
        idle_s: float | None = None,
        on_lost: LossObserver | None = None,
        call_timeout_s: float = BOARD_CALL_TIMEOUT_S,
    ) -> None:
        self.settings = settings
        self.on_lost = on_lost
        self._transport = transport
        self._clock = clock
        self._http2 = http2 and http2_available()
        self._on_connect = on_connect
        self._idle_s = idle_s
        self._call_timeout = timedelta(seconds=call_timeout_s)
        #: The protocol the current (or last) connection negotiated, or None before one has.
        self.protocol: str | None = None
        self._conn: _Connection | None = None
        #: Owners of idle connections still tearing down, awaited by ``aclose``.
        self._closing: set[asyncio.Task[None]] = set()
        self._lock: asyncio.Lock | None = None
        self._last_call = 0.0
        self._connect_failures = 0
        self._retry_at = 0.0
        self._connect_error: BaseException | None = None

    async def __aenter__(self) -> "BoardSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def __call__(
        self, name: str, arguments: dict[str, Any], _settings: AdapterSettings
    ) -> Any:
        return await self.invoke(name, arguments)

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        conn = await self._connected()
        calls = conn.calls
        if calls is None:
            # Discarded between its connect and this call joining it.
            raise ConnectionError(f"Agent Mail board session lost: {conn.lost}")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        # Joined before any await, so the idle check (which only closes a connection with
        # nothing pending) cannot close this one under the call.
        conn.pending.add(future)
        self._last_call = self._clock()
        try:
            calls.start_soon(
                self._run_call, future, conn, conn.session, name, arguments
            )
            return await future
        finally:
            conn.pending.discard(future)
            self._last_call = self._clock()

    async def aclose(self) -> None:
        """End the session and close its client. Never raises; a second call is a no-op."""
        conn, self._conn = self._conn, None
        owners = list(self._closing)
        if conn is not None and conn.owner is not None:
            conn.stop.set()
            owners.append(conn.owner)
        for owner in owners:
            with contextlib.suppress(Exception):
                await owner

    async def _connected(self) -> _Connection:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._conn is not None and self._conn.usable():
                return self._conn
            if self._connect_error is not None and self._clock() < self._retry_at:
                raise self._connect_error.with_traceback(None)
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[None] = loop.create_future()
            conn = _Connection(stop=asyncio.Event())
            conn.owner = loop.create_task(self._hold(conn, ready))
            self._conn = conn
            try:
                await ready
            except BaseException as exc:
                self._connect_failures += 1
                delay = min(
                    RECONNECT_BACKOFF_BASE_S * 2 ** (self._connect_failures - 1),
                    RECONNECT_BACKOFF_MAX_S,
                )
                self._retry_at = self._clock() + delay
                self._connect_error = exc
                raise
            self._connect_failures = 0
            self._connect_error = None
            return conn

    def open_connections(self) -> int | None:
        """TCP connections the current client's pool holds open (D-16 `sockets_board`).

        0 with no live connection; None when the transport is not httpx's own (a test's fake),
        whose pool cannot be read. HTTP/2 multiplexes onto one; the HTTP/1.1 pool opens one per
        request in flight.
        """
        conn = self._conn
        if conn is None or not conn.usable():
            return 0
        pool = getattr(conn.transport, "_pool", None)
        connections = getattr(pool, "connections", None)
        if not isinstance(connections, list):
            return None
        return sum(1 for connection in connections if not connection.is_closed())

    def _http_client(self, conn: _Connection) -> httpx.AsyncClient:
        transport = self._transport
        if transport is None:
            context = ssl.create_default_context(cafile=self.settings.ca_file)
            context.load_cert_chain(self.settings.cert_file, self.settings.key_file)
            transport = httpx.AsyncHTTPTransport(
                verify=context, http2=self._http2, trust_env=False
            )
        conn.transport = transport
        reported: list[str] = []

        async def note_protocol(response: httpx.Response) -> None:
            if reported:
                return
            reported.append(response.http_version)
            self.protocol = response.http_version
            if self._on_connect is not None:
                self._on_connect(response.http_version)

        def inline_failed(message: str, detail: str) -> None:
            self._discard(
                conn,
                f"{message} failed ({detail}); the MCP transport closed its streams",
            )

        return httpx.AsyncClient(
            transport=_CallIsolatingTransport(transport, conn.failures, inline_failed),
            follow_redirects=False,
            trust_env=False,
            timeout=30,
            event_hooks={"response": [note_protocol]},
        )

    async def _hold(self, conn: _Connection, ready: asyncio.Future[None]) -> None:
        failure: BaseException | None = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                http_client = await stack.enter_async_context(self._http_client(conn))
                (
                    read_stream,
                    write_stream,
                    _session_id,
                ) = await stack.enter_async_context(
                    streamable_http_client(self.settings.url, http_client=http_client)
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=self._call_timeout,
                    )
                )
                await session.initialize()
                calls = await stack.enter_async_context(anyio.create_task_group())
                conn.session, conn.calls = session, calls
                # A connect is activity: the call that asked for it has not joined yet.
                self._last_call = self._clock()
                ready.set_result(None)
                await self._until_stopped_or_idle(conn)
                calls.cancel_scope.cancel()
        except BaseException as exc:
            failure = exc
        finally:
            conn.session = conn.calls = None
            lost = failure or ConnectionError(
                f"Agent Mail board session lost: {conn.lost}"
                if conn.lost
                else "Agent Mail board session closed"
            )
            for waiting in [ready, *conn.pending]:
                if waiting.done():
                    continue
                if isinstance(lost, asyncio.CancelledError):
                    waiting.cancel()
                else:
                    waiting.set_exception(lost)
        if isinstance(failure, asyncio.CancelledError):
            raise failure

    async def _until_stopped_or_idle(self, conn: _Connection) -> None:
        """Return on ``aclose``, or once ``idle_s`` has passed with no call on ``conn``."""
        while not conn.stop.is_set():
            if self._idle_s is None:
                await conn.stop.wait()
                return
            remaining = self._last_call + self._idle_s - self._clock()
            if remaining <= 0 and not conn.pending:
                self._retire(conn)
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    conn.stop.wait(), remaining if remaining > 0 else self._idle_s
                )

    def _retire(self, conn: _Connection) -> None:
        """Mark ``conn`` unusable BEFORE the first await of its teardown, so a call arriving from
        here on opens a new connection instead of joining this one; ``aclose`` awaits its owner."""
        conn.closing = True
        owner = conn.owner
        if owner is not None and not owner.done():
            self._closing.add(owner)
            owner.add_done_callback(self._closing.discard)

    def _discard(self, conn: _Connection, reason: str) -> None:
        """End ``conn``, whose MCP streams are closed under its live owner, and say why once."""
        if conn.closing:
            return
        conn.lost = reason
        self._retire(conn)
        conn.stop.set()
        if self.on_lost is not None:
            self.on_lost(reason)

    async def _run_call(
        self,
        future: asyncio.Future[Any],
        conn: _Connection,
        session: ClientSession,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        # `Exception`, never `BaseException`: a call cancelled because the session ended must
        # leave its future to `_hold`, which sets the transport's own exception on it.
        try:
            result = await _call_on_session(session, name, arguments)
        except McpError as exc:
            isolated = (
                conn.failures.pop(str(exc.error.data), None)
                if exc.error.code == ISOLATED_FAILURE_CODE
                else None
            )
            if not future.done():
                future.set_exception(isolated or exc)
        except Exception as exc:  # noqa: BLE001 - handed to the caller unchanged
            if isinstance(exc, CLOSED_STREAM_ERRORS):
                self._discard(
                    conn,
                    f"a call found the MCP streams closed ({describe_failure(exc)})",
                )
            if not future.done():
                future.set_exception(exc)
        else:
            if not future.done():
                future.set_result(result)


async def _call_on_session(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> Any:
    if name == READ_RESOURCE:
        read = await session.read_resource(arguments["uri"])
        return read.model_dump(mode="json")
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise RuntimeError(
            f"upstream Agent Mail {name} refused: {_upstream_reason(result)}"
        )
    return result.model_dump(mode="json")


async def _invoke_remote_tool(
    name: str,
    arguments: dict[str, Any],
    settings: AdapterSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    """One call on its own session: the session-side adapter's default, one process per session."""
    async with BoardSession(settings, transport=transport) as session:
        return await session.invoke(name, arguments)


def _upstream_reason(result: Any) -> str:
    """Upstream's own reason for an `isError` tool result, safe for a terminal or a log line.

    Without it a contact-policy refusal, a retired sender and a dead board all read as the same
    "tool call failed" (ISSUE-REF). A structured `ToolExecutionError` payload gives its type and
    message only, never its free-form `data`; any URL or token-shaped value is redacted.
    """
    texts = [
        item.text
        for item in getattr(result, "content", None) or []
        if isinstance(getattr(item, "text", None), str)
    ]
    reason = " ".join(texts)
    try:
        payload = json.loads(reason)
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        reason = f"{error.get('type', 'ERROR')}: {error.get('message', '')}"
    reason = _CREDENTIAL.sub(r"\1\2<redacted>", _URL.sub("<url>", reason))
    reason = " ".join(reason.split())
    if len(reason) > MAX_UPSTREAM_REASON_CHARS:
        reason = reason[: MAX_UPSTREAM_REASON_CHARS - 1] + "…"
    return reason or "no reason given"
