#!/usr/bin/env python3
# ruff: noqa: E402
"""Create and validate the commit-bound coverage producer envelope."""

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
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_ARTIFACT_AGE = timedelta(hours=24)
SHA256_HEX_DIGEST_LENGTH = 64


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _suite_digest(paths: list[Path]) -> str:
    digest_value = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file():
            raise ValueError(f"coverage suite input is absent: {path}")
        digest_value.update(path.as_posix().encode("utf-8"))
        digest_value.update(path.read_bytes())
    return digest_value.hexdigest()


def validate(
    envelope: dict[str, object],
    *,
    commit: str,
    ref: str,
    stack: str,
    root: Path = Path("."),
    source_paths: list[str] | None = None,
) -> None:
    required = {
        "schema_version",
        "producer_argv",
        "stack",
        "commit_sha",
        "ref",
        "source_paths",
        "artifact_path",
        "artifact_sha256",
        "suite_digest",
        "suite_paths",
        "generated_at",
    }
    missing = required - envelope.keys()
    if missing:
        raise ValueError("coverage envelope missing " + ", ".join(sorted(missing)))
    if (
        envelope["schema_version"] != 1
        or envelope["commit_sha"] != commit
        or envelope["ref"] != ref
        or envelope["stack"] != stack
    ):
        raise ValueError(
            "coverage envelope does not match pushed ref, commit, or stack"
        )
    if not isinstance(envelope["producer_argv"], list) or not envelope["producer_argv"]:
        raise ValueError("coverage envelope has no full-test producer command")
    if not isinstance(envelope["source_paths"], list) or not envelope["source_paths"]:
        raise ValueError("coverage envelope has no source paths")
    if source_paths is not None and envelope["source_paths"] != source_paths:
        raise ValueError("coverage envelope source paths do not match the ratchet")
    if (
        not isinstance(envelope["suite_digest"], str)
        or len(envelope["suite_digest"]) != SHA256_HEX_DIGEST_LENGTH
    ):
        raise ValueError("coverage envelope has an invalid suite digest")
    if not isinstance(envelope["suite_paths"], list) or not all(
        isinstance(path, str) for path in envelope["suite_paths"]
    ):
        raise ValueError("coverage envelope has invalid suite paths")
    suite_paths = [root / path for path in envelope["suite_paths"]]
    if _suite_digest(suite_paths) != envelope["suite_digest"]:
        raise ValueError("coverage envelope suite digest is invalid")
    try:
        generated_at = datetime.fromisoformat(str(envelope["generated_at"]))
    except ValueError as error:
        raise ValueError(
            "coverage envelope has an invalid generated_at timestamp"
        ) from error
    if (
        generated_at.tzinfo is None
        or datetime.now(UTC) - generated_at > MAX_ARTIFACT_AGE
    ):
        raise ValueError("coverage envelope is stale")
    artifact = root / str(envelope["artifact_path"])
    if not artifact.is_file() or digest(artifact) != envelope["artifact_sha256"]:
        raise ValueError("coverage artifact is absent or digest-invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--source-path", action="append", default=[])
    parser.add_argument("--producer-arg", action="append", default=[])
    parser.add_argument("--suite-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = {
        "schema_version": 1,
        "producer_argv": args.producer_arg,
        "stack": args.stack,
        "commit_sha": args.commit,
        "ref": args.ref,
        "source_paths": args.source_path,
        "artifact_path": str(args.artifact),
        "artifact_sha256": digest(args.artifact),
        "suite_digest": _suite_digest(args.suite_file),
        "suite_paths": [str(path) for path in args.suite_file],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    args.output.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
