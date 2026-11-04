#!/usr/bin/env python3
# ruff: noqa: E402
"""Fleet receipt telemetry; intentionally does not inspect audit Markdown."""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.audit_receipt import ReceiptError, load, validate_receipt_authority


def summarize(paths: list[Path]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    by_stage: dict[str, Counter[str]] = defaultdict(Counter)
    by_stage_tier: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    invalid: dict[str, str] = {}

    def increment(metric: str, stage: str, tier: str) -> None:
        totals[metric] += 1
        by_tier[tier][metric] += 1
        by_stage[stage][metric] += 1
        by_stage_tier[stage][tier][metric] += 1

    for path in paths:
        try:
            events = load(path)
            validate_receipt_authority(events)
        except ReceiptError as exc:
            invalid[str(path)] = str(exc)
            continue
        tier = events[0]["budget"]["tier"] if events else "unknown"
        for event in events:
            kind = event["kind"]
            stage = event["stage"]
            increment(kind, stage, tier)
            if kind == "audit":
                increment("round_" + event["scope"]["kind"], stage, tier)
                for severity, count in event["new_by_severity"].items():
                    for _ in range(count):
                        increment("new_" + severity, stage, tier)
            elif kind == "halt":
                increment("halt_" + event["halt"], stage, tier)
            elif kind == "terminal":
                increment("terminal_" + event["terminal"], stage, tier)
    return {
        "counts": dict(totals),
        "by_risk_tier": {tier: dict(c) for tier, c in by_tier.items()},
        "by_stage": {stage: dict(c) for stage, c in by_stage.items()},
        "by_stage_risk_tier": {
            stage: {tier: dict(c) for tier, c in tiers.items()}
            for stage, tiers in by_stage_tier.items()
        },
        "invalid": invalid,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    print(summarize(args.paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
