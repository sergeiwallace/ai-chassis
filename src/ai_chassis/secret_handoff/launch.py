"""Machine gating before the optional skill environment is resolved."""

import os
from pathlib import Path
import sys
import tomllib

from ai_chassis.machine_registry import load_registry
from .models import HandoffError
from .planning import profile_enabled

ROOT = Path(__file__).resolve().parents[3]
DISABLED = "INFO secret-handoff: disabled for machine profile"


def enabled(root: Path | None = None) -> bool:
    try:
        root = ROOT if root is None else root
        document = tomllib.loads(
            (root / "projects-wide-session-config/ai-harness.toml").read_text("utf-8")
        )
        return profile_enabled(
            load_registry(document), document, os.environ.get("AI_HOST")
        )
    except HandoffError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise HandoffError(3, "secret-handoff: machine profile unavailable.") from None


def main(argv: list[str]) -> int:
    try:
        if not enabled():
            print(DISABLED)
            return 0
        script = ROOT / "skills/secret-handoff/scripts/secret_handoff.py"
        # Keep the command PID and the owner's explicitly inherited private descriptors.
        os.chdir(ROOT)
        os.execvp("uv", ["uv", "run", "--script", str(script), *argv])
    except HandoffError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except OSError:
        print("secret-handoff: launcher unavailable.", file=sys.stderr)
        return 3
