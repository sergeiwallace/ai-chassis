"""Install reviewed public bindings named by cross-pair launch policies; never open key files."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from ai_chassis import buzz_channel as buzz
from ai_chassis import remote_launch


def install_pubkeys(repo_root: Path, key_dir: Path) -> bool:
    from ai_chassis.agent_mail_notifier import read_config_tables

    try:
        tables = read_config_tables(
            repo_root / "projects-wide-session-config/ai-harness.toml"
        )
        config = remote_launch.load_remote_launch_config(tables)
        if config.disabled_reason not in {"", "enabled is not true"}:
            raise ValueError("invalid policy")
        destination = buzz.pubkey_directory_path(key_dir)
        # The normal directory reader tolerates corruption. Installation must preserve it.
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValueError("invalid directory")
    except (OSError, ValueError, RuntimeError):
        print(
            "  [warn] cross-pair public bindings: unreadable policy or directory; merge refused",
            file=sys.stderr,
        )
        return False

    identities = {
        identity
        for policy in config.cross_pair
        if policy.config.enabled
        for identity in (
            *policy.config.senders,
            *(f"daemon@{host}" for host in policy.to_machines),
        )
    }
    ok = True
    for identity in sorted(identities):
        path = repo_root / "config/buzz-pubkeys" / f"{identity}.pub"
        try:
            public = path.read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-f]{64}", public):
                raise ValueError("invalid public binding")
            if not _merge_binding(destination, identity, public):
                ok = False
        except (OSError, ValueError, AttributeError):
            print(
                f"  [warn] cross-pair public binding unavailable or refused: {identity}",
                file=sys.stderr,
            )
            ok = False
    return ok


def _merge_binding(destination: Path, identity: str, public: str) -> bool:
    # Re-read immediately before each update; preserve all unrelated bindings.
    existing = json.loads(destination.read_text()) if destination.exists() else {}
    if identity in existing:
        if existing[identity].get("pubkey") != public:
            print(
                f"  [warn] cross-pair public binding conflict: {identity}; refused",
                file=sys.stderr,
            )
            return False
        return True
    now = int(time.time())
    result = buzz.record_pubkey(destination, identity, public, created_at=now, now=now)
    if not result.changed:
        raise ValueError("binding refused")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--key-dir", required=True, type=Path)
    args = parser.parse_args()
    return 0 if install_pubkeys(args.repo_root, args.key_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
