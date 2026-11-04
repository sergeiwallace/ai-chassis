#!/usr/bin/env python3
"""Fail-open UserPromptSubmit pickup for terminal local supervisor jobs."""

from __future__ import annotations

import sys
from pathlib import Path

EVENT_NAME = "UserPromptSubmit"


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    from ai_chassis.job_supervisor_pickup import run

    return run(EVENT_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
