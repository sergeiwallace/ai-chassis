"""Fail-closed cleanup for leaked upstream aiosqlite workers.

MCP Agent Mail v0.3.4's concurrency tests can leave a connection worker alive after
pytest has reported its result. Python then waits forever during interpreter shutdown.
This gate closes any such connection and turns an uncloseable worker into a test failure.
"""

from __future__ import annotations

import gc

from aiosqlite import Connection

from mcp_agent_mail.db import reset_database_state

THREAD_JOIN_SECONDS = 5


def pytest_sessionfinish(session) -> None:
    reset_database_state()
    gc.collect()
    leaked = [
        connection
        for connection in gc.get_objects()
        if isinstance(connection, Connection) and connection._thread.is_alive()
    ]
    for connection in leaked:
        connection.stop()
        connection._thread.join(THREAD_JOIN_SECONDS)
    if any(connection._thread.is_alive() for connection in leaked):
        session.exitstatus = 1
