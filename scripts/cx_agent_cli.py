#!/usr/bin/env python3
"""Thin ``cx agent`` MCP client; it owns neither policy nor controller state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "cx_agent_server.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _validate_start_policy(arguments: dict[str, Any]) -> None:
    """Reject invalid backend/profile choices before launching the MCP child."""
    from scripts.cx_policy_plan import (
        PolicyPlanOverrides,
        installed_profile_path,
        resolve_attached_policy_plan,
    )

    resolve_attached_policy_plan(
        arguments["role"],
        installed_profile_path(arguments["role"]),
        config_path=ROOT / "config.toml",
        backend_id=arguments.get("backendId"),
        profile_id=arguments.get("profileId"),
        overrides=PolicyPlanOverrides(cwd=Path.cwd()),
    )


def _call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    ]
    completed = subprocess.run(
        [sys.executable, str(SERVER)],
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "cx-agents MCP server failed")
    return json.loads(completed.stdout.splitlines()[-1])["result"]["structuredContent"]


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - closed MCP command table
    parser = argparse.ArgumentParser(prog="cx agent")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("role")
    start.add_argument("--task", dest="taskKey", required=True)
    start.add_argument("--request-id", dest="clientRequestId", required=True)
    start.add_argument("--topology", choices=("per-lease", "shared"))
    start.add_argument("--persistence", choices=("ephemeral", "recovery"))
    start.add_argument("--backend", dest="backendId")
    start.add_argument("--profile", dest="profileId")
    start.add_argument("prompt", nargs="+")
    for name, tool in (
        ("status", "cx_agent_get_status"),
        ("watch", "cx_agent_get_events"),
    ):
        command = commands.add_parser(name)
        command.add_argument("handle")
        if name == "watch":
            command.add_argument("--after-cursor", dest="afterCursor")
            command.add_argument("--limit", type=int)
            command.add_argument("--wait-ms", dest="waitMs", type=int)
        command.set_defaults(tool=tool)
    steer = commands.add_parser("steer")
    steer.add_argument("handle")
    steer.add_argument("--turn", dest="expectedTurnId", required=True)
    steer.add_argument(
        "--generation", dest="expectedControllerGeneration", type=int, required=True
    )
    steer.add_argument("--request-id", dest="clientRequestId", required=True)
    steer.add_argument("message", nargs="+")
    steer.set_defaults(tool="cx_agent_steer")
    send = commands.add_parser("send-message")
    send.add_argument("handle")
    send.add_argument(
        "--generation", dest="expectedControllerGeneration", type=int, required=True
    )
    send.add_argument("--request-id", dest="clientRequestId", required=True)
    send.add_argument("prompt", nargs="+")
    send.set_defaults(tool="cx_agent_send_message")
    interrupt = commands.add_parser("interrupt")
    interrupt.add_argument("handle")
    interrupt.add_argument("--turn", dest="expectedTurnId", required=True)
    interrupt.add_argument(
        "--generation", dest="expectedControllerGeneration", type=int, required=True
    )
    interrupt.add_argument("--request-id", dest="clientRequestId", required=True)
    interrupt.set_defaults(tool="cx_agent_interrupt")
    stop = commands.add_parser("stop")
    stop.add_argument("handle")
    stop.add_argument(
        "--generation", dest="expectedControllerGeneration", type=int, default=1
    )
    stop.add_argument("--request-id", dest="clientRequestId")
    stop.add_argument("--reason")
    stop.set_defaults(tool="cx_agent_stop")
    listing = commands.add_parser("list")
    listing.set_defaults(tool="cx_agent_list")
    sweep = commands.add_parser("sweep")
    sweep.add_argument("--mode", choices=("expired", "all"), default="expired")
    sweep.add_argument("--dry-run", action="store_true", dest="dryRun")
    sweep.add_argument("--confirm-all", action="store_true", dest="confirmAll")
    sweep.add_argument("--request-id", dest="clientRequestId")
    sweep.set_defaults(tool="cx_agent_sweep")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "start":
        prompt = " ".join(args.prompt).strip()
        selected = {
            key: value
            for key, value in vars(args).items()
            if key
            in {
                "role",
                "taskKey",
                "clientRequestId",
                "topology",
                "persistence",
                "backendId",
                "profileId",
            }
            and value is not None
        }
        try:
            _validate_start_policy(selected)
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "requestId": selected["clientRequestId"],
                        "ok": False,
                        "data": None,
                        "error": {
                            "code": "invalid_policy",
                            "message": str(exc),
                            "retryable": False,
                        },
                    },
                    sort_keys=True,
                )
            )
            return 2
        envelope = _call("cx_agent_start", selected | {"prompt": prompt})
    else:
        arguments = {
            key: value
            for key, value in vars(args).items()
            if key
            in {
                "handle",
                "afterCursor",
                "limit",
                "waitMs",
                "expectedTurnId",
                "expectedControllerGeneration",
                "reason",
                "mode",
                "dryRun",
                "confirmAll",
                "clientRequestId",
            }
            and value is not None
        }
        if args.command in {"steer", "send-message"}:
            arguments["message" if args.command == "steer" else "prompt"] = " ".join(
                getattr(args, "message", getattr(args, "prompt", []))
            ).strip()
        if args.command == "stop":
            arguments.update(
                {
                    "clientRequestId": arguments.get("clientRequestId")
                    or str(uuid.uuid4()),
                }
            )
        if args.command == "sweep":
            if args.mode == "all" and not args.dryRun and not args.confirmAll:
                _parser().error(
                    "sweep --mode all requires --confirm-all unless --dry-run"
                )
            if not args.confirmAll:
                arguments.pop("confirmAll", None)
            arguments["clientRequestId"] = arguments.get("clientRequestId") or str(
                uuid.uuid4()
            )
        envelope = _call(args.tool, arguments)
    print(json.dumps(envelope, sort_keys=True))
    return 0 if envelope["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
