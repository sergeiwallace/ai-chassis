from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .authorization import AccessDenied, BMS_NAMESPACE, TransportIdentity
from .operability import OperabilityEvidence


@dataclass(frozen=True)
class MachineRecord:
    machine_id: str
    label: str
    namespace: str
    transports: tuple[TransportIdentity, ...]
    active: bool = True


class MachineRegistry:
    """Server-owned, independently revocable mapping from transport proof to machine."""

    def __init__(
        self, path: str | Path, audit_path: str | Path, manifest_path: str | Path
    ) -> None:
        self.path = Path(path)
        self.audit_path = Path(audit_path)
        self.manifest_path = Path(manifest_path)

    def machines(self) -> list[MachineRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [self._record_from_mapping(item) for item in raw.get("machines", [])]

    @staticmethod
    def _record_from_mapping(item: dict[str, Any]) -> MachineRecord:
        transports = item.get("transports")
        if transports is None:
            transports = [
                {"kind": item["transport_kind"], "value": item["transport_value"]}
            ]
        return MachineRecord(
            machine_id=item["machine_id"],
            label=item["label"],
            namespace=item["namespace"],
            transports=tuple(
                TransportIdentity(**transport) for transport in transports
            ),
            active=item.get("active", True),
        )

    def _manifest_machine(self, machine_id: str) -> MachineRecord:
        if not self.manifest_path.exists():
            raise AccessDenied("machine manifest is required for enrollment")
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        matches = [
            item
            for item in raw.get("machines", [])
            if item.get("machine_id") == machine_id
        ]
        if len(matches) != 1:
            raise AccessDenied("machine id is not present in the server-owned manifest")
        item = matches[0]
        try:
            return self._record_from_mapping(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise AccessDenied("machine manifest entry is invalid") from exc

    def get(self, machine_id: str) -> MachineRecord:
        for machine in self.machines():
            if machine.machine_id == machine_id:
                return machine
        raise KeyError(machine_id)

    def enroll(
        self,
        *,
        machine_id: str,
        label: str,
        namespace: str,
        transport: TransportIdentity,
        evidence: OperabilityEvidence,
    ) -> MachineRecord:
        failed = evidence.failed()
        if failed:
            self._audit(
                "enroll", "denied", "operability_gate:" + ",".join(failed), machine_id
            )
            raise AccessDenied("credential enrollment blocked by: " + ", ".join(failed))
        manifest = self._manifest_machine(machine_id)
        if label != manifest.label or namespace != manifest.namespace:
            raise AccessDenied(
                "label and namespace must match the server-owned manifest"
            )
        if transport not in manifest.transports:
            raise AccessDenied(
                "transport identity is not authorized by the server-owned manifest"
            )
        records = self.machines()
        existing = next(
            (item for item in records if item.machine_id == machine_id), None
        )
        if any(transport in item.transports for item in records):
            raise AccessDenied("transport identity already maps to a machine")
        if existing is None:
            record = MachineRecord(machine_id, label, namespace, (transport,))
            updated = [*records, record]
        else:
            record = MachineRecord(
                existing.machine_id,
                existing.label,
                existing.namespace,
                (*existing.transports, transport),
                active=existing.active,
            )
            updated = [
                record if item.machine_id == machine_id else item for item in records
            ]
        self._write(updated)
        self._audit("enroll", "allowed", "all_gates_passed", machine_id)
        return record

    def authenticate(
        self,
        identity: TransportIdentity | None,
        *,
        edge_verified: bool = True,
    ) -> MachineRecord:
        if not edge_verified:
            self._audit("authenticate", "denied", "unverified_edge")
            raise AccessDenied("direct backend access is denied")
        if identity is None:
            self._audit("authenticate", "denied", "missing_identity")
            raise AccessDenied("verified transport identity is required")
        matches = [item for item in self.machines() if identity in item.transports]
        if len(matches) != 1:
            self._audit(
                "authenticate",
                "denied",
                "unknown_identity",
                identity=self._identity_hash(identity),
            )
            raise AccessDenied("unknown transport identity")
        machine = matches[0]
        if not machine.active:
            self._audit(
                "authenticate", "denied", "revoked_identity", machine.machine_id
            )
            raise AccessDenied("machine credential is revoked")
        self._audit("authenticate", "allowed", "active_identity", machine.machine_id)
        return machine

    def revoke(self, machine_id: str, *, reason: str) -> None:
        records = [
            MachineRecord(**{**asdict(item), "active": False})
            if item.machine_id == machine_id
            else item
            for item in self.machines()
        ]
        if not any(item.machine_id == machine_id for item in records):
            raise KeyError(machine_id)
        self._write(records)
        self._audit("revoke", "allowed", reason, machine_id)

    def rotate_transport(
        self,
        machine_id: str,
        *,
        retired: TransportIdentity,
        replacement: TransportIdentity,
        evidence: OperabilityEvidence,
        reason: str,
    ) -> MachineRecord:
        """Retire one compromised identity and explicitly reactivate its machine."""
        failed = evidence.failed()
        if failed:
            raise AccessDenied("credential rotation blocked by: " + ", ".join(failed))
        if replacement == retired:
            self._audit(
                "rotate_transport",
                "denied",
                "replacement_transport_must_differ_from_retired",
                machine_id,
            )
            raise AccessDenied(
                "replacement transport must differ from retired transport"
            )
        existing = self.get(machine_id)
        manifest = self._manifest_machine(machine_id)
        if retired not in existing.transports:
            raise AccessDenied("retired transport is not enrolled for this machine")
        if replacement not in manifest.transports:
            raise AccessDenied(
                "replacement transport is not authorized by the manifest"
            )
        if replacement != retired and any(
            replacement in item.transports
            for item in self.machines()
            if item.machine_id != machine_id
        ):
            raise AccessDenied(
                "replacement transport identity already maps to a machine"
            )
        transports = tuple(item for item in existing.transports if item != retired)
        if replacement not in transports:
            transports += (replacement,)
        if not transports:
            raise AccessDenied("rotation must retain an authorized transport")
        record = MachineRecord(
            existing.machine_id,
            existing.label,
            existing.namespace,
            transports,
            active=True,
        )
        self._write(
            [
                record if item.machine_id == machine_id else item
                for item in self.machines()
            ]
        )
        self._audit("rotate_transport", "allowed", reason, machine_id)
        return record

    def revoke_bms(self, *, reason: str) -> None:
        records = self.machines()
        changed = False
        updated = []
        for item in records:
            if item.namespace == BMS_NAMESPACE and item.active:
                item = MachineRecord(**{**asdict(item), "active": False})
                changed = True
                self._audit("revoke", "allowed", reason, item.machine_id)
            updated.append(item)
        if changed:
            self._write(updated)

    def _write(self, records: list[MachineRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = (
            json.dumps({"machines": [asdict(item) for item in records]}, indent=2)
            + "\n"
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=".registry-", dir=self.path.parent, text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _audit(
        self,
        action: str,
        outcome: str,
        reason: str,
        machine_id: str | None = None,
        **extra: Any,
    ) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "outcome": outcome,
            "reason": reason,
            "machine_id": machine_id,
            **extra,
        }
        fd = os.open(self.audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _identity_hash(identity: TransportIdentity) -> str:
        return hashlib.sha256(f"{identity.kind}:{identity.value}".encode()).hexdigest()
