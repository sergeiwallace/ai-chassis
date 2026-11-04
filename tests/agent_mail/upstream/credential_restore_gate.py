from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from typer.testing import CliRunner

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.cli import app, resolve_sqlite_database_path
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import reset_database_state
from mcp_agent_mail.storage import clear_repo_cache


async def exercise(root: Path) -> None:
    database = root / "test.sqlite3"
    storage = root / "storage"
    archive_dir = root / "archives"
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "STORAGE_ROOT": str(storage),
            "APP_ENVIRONMENT": "test",
            "GIT_AUTHOR_NAME": "restore-gate",
            "GIT_AUTHOR_EMAIL": "restore-gate@example.com",
        }
    )
    clear_settings_cache()
    reset_database_state()
    clear_repo_cache()

    from mcp_agent_mail import cli

    def archive_states_dir(create: bool = True) -> Path:
        del create
        return archive_dir

    cli._archive_states_dir = archive_states_dir
    project_key = "/credential-aware-disaster-recovery"
    async with Client(build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        identity = await client.call_tool(
            "create_agent_identity",
            {"project_key": project_key, "program": "gate", "model": "test"},
        )
        agent_name = identity.data["name"]
        registration_token = identity.data["registration_token"]
        await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": agent_name,
                "to": [agent_name],
                "subject": "restore token",
                "body_md": "preserved",
            },
        )

    runner = CliRunner()
    saved = runner.invoke(app, ["archive", "save", "--label", "token-aware"])
    assert saved.exit_code == 0, saved.stdout
    archive = next(archive_dir.glob("*.zip"))
    settings = get_settings()
    clear_repo_cache()
    reset_database_state()
    database_path = resolve_sqlite_database_path(settings.database.url)
    if database_path.exists():
        database_path.unlink()
    if storage.exists():
        shutil.rmtree(storage)
    restored = runner.invoke(app, ["archive", "restore", str(archive), "--force"])
    assert restored.exit_code == 0, restored.stdout

    async with Client(build_mcp_server()) as client:
        inbox = await client.call_tool(
            "fetch_inbox",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "registration_token": registration_token,
                "include_bodies": True,
            },
        )
    assert any(
        "restore token" in getattr(content, "text", "") for content in inbox.content
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agent-mail-restore-gate-") as root:
        asyncio.run(exercise(Path(root)))
    print("credential-aware archive/restore: passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"credential-aware archive/restore: failed: {exc}", file=sys.stderr)
        raise
