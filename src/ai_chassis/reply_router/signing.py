"""Ed25519 over canonical JSON without the signature field, using OpenSSL 3."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ai_chassis.reply_router.records import (
    MAX_RECORD_BYTES,
    REPLY,
    RecordError,
    timestamp,
    validate_record,
)

_ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")
_SIGNATURE_BYTES = 64


class SignatureError(ValueError):
    """No key material or subprocess diagnostics enter this error."""


def _openssl(args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["openssl", *args], capture_output=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        raise SignatureError("signing-unavailable") from None
    if result.returncode:
        raise SignatureError("signature-invalid")
    return result.stdout


def _canonical(record: dict) -> bytes:
    return json.dumps(
        {key: value for key, value in record.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _check_size(canonical: bytes, signature: str) -> None:
    # Add the omitted field to recover the full wire size without encoding it twice.
    size = (
        len(canonical)
        + len(b',"signature":')
        + len(json.dumps(signature, ensure_ascii=False).encode("utf-8"))
    )
    if size > MAX_RECORD_BYTES:
        raise RecordError(f"max_bytes limit {MAX_RECORD_BYTES}")


def _key(path: Path, *, public: bool) -> None:
    args = [
        "pkey",
        *(["-pubin"] if public else []),
        "-in",
        str(path),
        "-pubout",
        "-outform",
        "DER",
    ]
    der = _openssl(args)
    if not der.startswith(_ED25519_SPKI) or len(der) != len(_ED25519_SPKI) + 32:
        raise SignatureError("key-not-ed25519")


def sign(record: dict, private_key: Path) -> dict:
    validate_record(record)
    canonical = _canonical(record)
    _check_size(canonical, record["signature"])
    _key(private_key, public=False)
    with tempfile.TemporaryDirectory(prefix="aih-reply-sign-") as scratch:
        data = Path(scratch) / "record.json"
        data.write_bytes(canonical)
        signature = _openssl(
            ["pkeyutl", "-sign", "-rawin", "-inkey", str(private_key), "-in", str(data)]
        )
    signed = {**record, "signature": signature.hex()}
    _check_size(canonical, signed["signature"])
    return signed


def verify(record: dict, public_key: Path) -> None:
    validate_record(record)
    canonical = _canonical(record)
    _check_size(canonical, record["signature"])
    try:
        signature = bytes.fromhex(record["signature"])
    except ValueError:
        raise SignatureError("signature-invalid") from None
    if len(signature) != _SIGNATURE_BYTES:
        raise SignatureError("signature-invalid")
    _key(public_key, public=True)
    with tempfile.TemporaryDirectory(prefix="aih-reply-verify-") as scratch:
        data, sig = Path(scratch) / "record.json", Path(scratch) / "signature"
        data.write_bytes(canonical)
        sig.write_bytes(signature)
        _openssl(
            [
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key),
                "-in",
                str(data),
                "-sigfile",
                str(sig),
            ]
        )


def verify_reply(record: dict, public_key: Path) -> None:
    from datetime import UTC, datetime

    verify(record, public_key)
    if record["schema"] != REPLY:
        raise RecordError("expected reply schema")
    if timestamp(record["not_after"], "not_after") <= datetime.now(UTC):
        raise RecordError("expired")
