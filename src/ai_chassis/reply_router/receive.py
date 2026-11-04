"""Verify and claim a reply, commit held answers, then call the prompt library."""

from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from ai_chassis.prompt_injection import config as pi_config
from ai_chassis.prompt_injection.injector import SlackReplyInjector
from ai_chassis.reply_router.grammar import payload_for
from ai_chassis.reply_router.records import (
    REPLY,
    RecordError,
    read_record,
    write_record,
)
from ai_chassis.reply_router.signing import SignatureError, sign, verify_reply

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RETRY_LEASE_S = 300


def load_config(host: str, path: Path | None = None) -> dict:
    tables = tomllib.loads(
        (path or REPO_ROOT / "projects-wide-session-config/ai-harness.toml").read_text(
            encoding="utf-8"
        )
    )
    return tables.get("machine", {}).get(host, {}).get("slack_hub", {})


def slack_hub_declares_a_capability(table: dict) -> bool:
    return table.get("enabled", True) is True


class Receiver:
    def __init__(  # noqa: PLR0913 - explicit process boundaries and offline seams
        self,
        *,
        host: str,
        public_key: Path | None = None,
        private_key: Path | None = None,
        state_dir: Path | None = None,
        inject=None,
        answer=None,
        library_config=pi_config.load_config,
        config=None,
    ):
        self.host = host
        self.root = (
            state_dir
            or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            / "ai-harness/hub"
        )
        self.public_key = public_key or self.root / "hub.pub"
        self.private_key = private_key or self.root / "host.key"
        self.inject = inject or self._inject
        self.answer = answer or self._answer
        self.library_config = library_config
        self.config = config or (lambda: load_config(host))

    @staticmethod
    def _prepare_ledger(ledger: sqlite3.Connection) -> None:
        ledger.execute(
            "CREATE TABLE IF NOT EXISTS delivered (delivery_id TEXT PRIMARY KEY)"
        )
        ledger.execute(
            "CREATE TABLE IF NOT EXISTS pending (delivery_id TEXT PRIMARY KEY, "
            "record TEXT, layers TEXT, injections TEXT, next_attempt_at REAL, "
            "lease_until REAL NOT NULL DEFAULT 0)"
        )
        # Serialize this migration with claims, including on pre-lease ledgers.
        if "lease_until" not in {
            column[1] for column in ledger.execute("PRAGMA table_info(pending)")
        }:
            ledger.execute(
                "ALTER TABLE pending ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"
            )

    def _claim(self, record: dict, policy: dict) -> float | None:
        now = time.time()
        lease_until = now + float(policy.get("retry_lease_s", DEFAULT_RETRY_LEASE_S))
        with (
            closing(sqlite3.connect(self.root / "delivered.sqlite3")) as ledger,
            ledger,
        ):
            ledger.execute("BEGIN IMMEDIATE")
            self._prepare_ledger(ledger)
            if ledger.execute(
                "SELECT 1 FROM delivered WHERE delivery_id = ?",
                (record["delivery_id"],),
            ).fetchone():
                return None
            try:
                ledger.execute(
                    "INSERT INTO pending "
                    "(delivery_id, record, layers, injections, next_attempt_at, lease_until) "
                    "VALUES (?, ?, '[]', '[]', ?, ?)",
                    (
                        record["delivery_id"],
                        write_record(record).decode("utf-8"),
                        now,
                        lease_until,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
        return lease_until

    def _checkpoint(
        self, record, layers, injections, lease_until, *, retry_at=None
    ) -> bool:
        with (
            closing(sqlite3.connect(self.root / "delivered.sqlite3")) as ledger,
            ledger,
        ):
            return bool(
                ledger.execute(
                    "UPDATE pending SET layers = ?, injections = ?, "
                    "next_attempt_at = COALESCE(?, next_attempt_at), lease_until = ? "
                    "WHERE delivery_id = ? AND lease_until = ?",
                    (
                        json.dumps(layers),
                        json.dumps(injections),
                        retry_at,
                        0 if retry_at is not None else lease_until,
                        record["delivery_id"],
                        lease_until,
                    ),
                ).rowcount
            )

    def _finish(self, record, layers, injections, lease_until) -> dict | None:
        # Persist terminal work before emitting its receipt, so recovery never
        # repeats an injection whose terminal outcome was checkpointed.
        if not self._checkpoint(record, layers, injections, lease_until):
            return None
        with (
            closing(sqlite3.connect(self.root / "delivered.sqlite3")) as ledger,
            ledger,
        ):
            ledger.execute("BEGIN IMMEDIATE")
            if not ledger.execute(
                "SELECT 1 FROM pending WHERE delivery_id = ? AND lease_until = ?",
                (record["delivery_id"], lease_until),
            ).fetchone():
                return None
            result = self._result(record, layers, injections)
            ledger.execute(
                "INSERT OR IGNORE INTO delivered VALUES (?)", (record["delivery_id"],)
            )
            ledger.execute(
                "DELETE FROM pending WHERE delivery_id = ?", (record["delivery_id"],)
            )
        return result

    def _result(self, record: dict, layers: list, injections: list) -> dict:
        session = record["target"].split("@", 1)[0]
        skill = record["command"]
        changed_form = any(layer["detail"] == "form-changed" for layer in layers)
        skill_result = (
            {
                "name": skill["skill"],
                "state": "not-run" if changed_form else injections[-1]["state"],
                "landing": None if changed_form else injections[-1]["landing"],
            }
            if skill and injections
            else None
        )
        result = sign(
            {
                **self._empty_result(record),
                "session": session,
                "layers": layers,
                "injection": injections,
                "skill": skill_result,
            },
            self.private_key,
        )
        with (self.root / "receipts.jsonl").open("ab") as receipt:
            receipt.write(write_record(result) + b"\n")
            receipt.flush()
            os.fsync(receipt.fileno())
        return result

    def receive(self, raw: dict | str | bytes) -> dict | None:
        """Return a terminal receipt, or None while a retry is pending."""
        record = read_record(write_record(raw) if isinstance(raw, dict) else raw)
        if record["schema"] != REPLY:
            raise RecordError("expected reply schema")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            verify_reply(record, self.public_key)
            policy = self.config()
            self._policy(record, policy)
            payload_for(record)
            # Confirm that an echo can be signed before claiming or delivering.
            sign(self._empty_result(record), self.private_key)
            lease_until = self._claim(record, policy)
            if lease_until is None:
                raise ValueError("duplicate-delivery")
        except (SignatureError, ValueError) as error:
            return self._result(
                record,
                [{"layer": "receive", "outcome": "refused", "detail": str(error)}],
                [],
            )
        return self._attempt(record, [], [], lease_until)

    def retry_pending(self) -> list[dict]:
        """Resume each due reply once; no new transport delivery is required."""
        database = self.root / "delivered.sqlite3"
        if not database.exists():
            return []
        with closing(sqlite3.connect(database)) as ledger, ledger:
            # Also supports ledgers created before retries were persisted.
            ledger.execute("BEGIN IMMEDIATE")
            self._prepare_ledger(ledger)
            now = time.time()
            rows = ledger.execute(
                "SELECT delivery_id, record, layers, injections, next_attempt_at, "
                "lease_until FROM pending WHERE next_attempt_at <= ? AND lease_until <= ? "
                "ORDER BY next_attempt_at, delivery_id",
                (now, now),
            ).fetchall()
        results = []
        for delivery_id, raw, layers, injections, next_attempt_at, old_lease in rows:
            now = time.time()
            lease_until = now + float(
                self.config().get("retry_lease_s", DEFAULT_RETRY_LEASE_S)
            )
            # Compare the snapshot and lease together: another tick may have
            # already run and rescheduled this row since the SELECT above.
            with closing(sqlite3.connect(database)) as ledger, ledger:
                claimed = ledger.execute(
                    "UPDATE pending SET lease_until = ? WHERE delivery_id = ? "
                    "AND next_attempt_at = ? AND layers = ? AND injections = ? "
                    "AND lease_until = ? "
                    "AND lease_until <= ?",
                    (
                        lease_until,
                        delivery_id,
                        next_attempt_at,
                        layers,
                        injections,
                        old_lease,
                        now,
                    ),
                ).rowcount
            if not claimed:
                continue
            result = self._attempt(
                read_record(raw),
                json.loads(layers),
                json.loads(injections),
                lease_until,
            )
            if result is not None:
                results.append(result)
        return results

    def _attempt(
        self, record: dict, layers: list, injections: list, lease_until: float
    ) -> dict | None:
        if layers and layers[-1]["outcome"] not in ("answered", "written"):
            return self._finish(record, layers, injections, lease_until)
        try:
            verify_reply(record, self.public_key)
            policy = self.config()
            self._policy(record, policy)
        except ValueError as error:
            layers.append(
                {"layer": "receive", "outcome": "refused", "detail": str(error)}
            )
            return self._finish(record, layers, injections, lease_until)
        if record["kind"] == "held-answer" and not any(
            layer["outcome"] == "answered" for layer in layers
        ):
            try:
                self.answer(record)
            except (OSError, subprocess.SubprocessError):
                layers.append(
                    {
                        "layer": "file",
                        "outcome": "refused",
                        "detail": "held-answer-failed",
                    }
                )
                return self._finish(record, layers, injections, lease_until)
            layers.append(
                {"layer": "file", "outcome": "answered", "detail": record["item_path"]}
            )
            if not self._checkpoint(record, layers, injections, lease_until):
                return None
            # Held-answer work may outlast the reply or change live policy.
            try:
                verify_reply(record, self.public_key)
                policy = self.config()
                self._policy(record, policy)
            except ValueError as error:
                layers.append(
                    {"layer": "receive", "outcome": "refused", "detail": str(error)}
                )
                return self._finish(record, layers, injections, lease_until)
        return self._inject_attempt(
            record, layers, injections, lease_until, policy=policy
        )

    def _inject_attempt(
        self, record, layers, injections, lease_until, *, policy
    ) -> dict | None:
        payload = self._fallback(
            record, payload_for(record), layers if not injections else []
        )
        result = self.inject(record, payload)
        injections.append(
            {
                name: getattr(result, name)
                for name in ("attempt_id", "state", "landing", "reason")
            }
        )
        retry = result.state == "REFUSED" and result.reason in (
            "composer-nonempty",
            "not-ready",
        )
        if retry and len(injections) <= int(policy.get("retry_max", 10)):
            self._checkpoint(
                record,
                layers,
                injections,
                lease_until,
                retry_at=time.time() + policy.get("retry_interval_s", 30),
            )
            return None
        layers.append(
            {
                "layer": "prompt",
                "outcome": result.state.lower(),
                "detail": result.reason or "",
            }
        )
        return self._finish(record, layers, injections, lease_until)

    def _empty_result(self, record):
        return {
            "schema": "aih-reply-result/v1",
            "delivery_id": record["delivery_id"],
            "host": self.host,
            "session": record["target"].split("@", 1)[0],
            "layers": [],
            "injection": [],
            "skill": None,
            "finished_at": datetime.now(UTC).isoformat(),
            "signature": "",
        }

    def _policy(self, record: dict, policy: dict) -> None:
        if not slack_hub_declares_a_capability(policy):
            raise ValueError("disabled")
        if record["target"].split("@", 1)[1] != self.host:
            raise ValueError("wrong-host")
        if record["channel"] != "slack":
            raise ValueError("channel-policy-unresolved")
        if record["kind"] == "note":
            raise ValueError("note-requires-inbox-socket")
        if record["command"] and record["command"]["skill"] in policy.get(
            "skill_denylist", []
        ):
            raise ValueError("command-denied")

    def _text_file(self, record: dict) -> Path:
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = inbox / f"{record['delivery_id']}.md"
        if path.exists():
            if path.read_text(encoding="utf-8") != record["text"]:
                raise ValueError("inbox-content-mismatch")
            return path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(record["text"])
        return path

    def _fallback(self, record, payload, layers):
        settings = self.library_config()
        limit = (
            settings["multiline_mode"]["max_chars"]
            if payload.mode == "multiline"
            else settings["max_typed_chars"]
        )
        if len(payload.text) <= limit:
            return payload
        from ai_chassis.prompt_injection import PromptPayload

        path = self._text_file(record)
        layers.append({"layer": "file", "outcome": "written", "detail": "form-changed"})
        return PromptPayload(f"Read the reply in {path}")

    def _inject(self, record, payload):
        return SlackReplyInjector(
            record=record, public_key=self.public_key, inbox=self.root / "inbox"
        ).run("session:" + record["target"].split("@", 1)[0], payload)

    def _answer(self, record):
        path = self._text_file(record)
        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/held_cli.py"),
                "answer",
                record["item_path"],
                "--text-file",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
