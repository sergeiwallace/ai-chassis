"""The ``ec2`` storage adapter: verify a declared volume by mount, fstype and EBS serial.

Needs no root and no ``nvme-cli``: ``findmnt -J`` gives mount, source and fstype; ``lsblk -J``
gives each block device's SERIAL, which AWS sets to the EBS volume id without its hyphen
(research doc § 4.2). The SERIAL sits on the parent disk while the MOUNTPOINT may sit on a child
partition, so a mount's serial is resolved through the lsblk tree: a node inherits the nearest
ancestor's SERIAL when it carries none of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..storage import DeclaredVolume, ObservedVolume, Verification

Runner = Callable[..., subprocess.CompletedProcess[str]]
FINDMNT = ("findmnt", "-J")
LSBLK = ("lsblk", "-J", "-o", "NAME,SERIAL,MOUNTPOINT,FSTYPE")


class AdapterCommandError(RuntimeError):
    """A probe command was unavailable or failed; the message carries its stderr."""


def _walk(nodes: Any, key: str) -> Iterator[dict[str, Any]]:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _walk(node.get(key), key)


def _lsblk_serials(devices: Any, inherited: str | None = None) -> dict[str, str | None]:
    """Map every lsblk MOUNTPOINT to its own-or-inherited SERIAL."""
    serials: dict[str, str | None] = {}
    if not isinstance(devices, list):
        return serials
    for device in devices:
        if not isinstance(device, dict):
            continue
        serial = device.get("serial") or inherited
        mountpoint = device.get("mountpoint")
        if isinstance(mountpoint, str) and mountpoint:
            serials[mountpoint] = serial
        for extra in device.get("mountpoints") or []:
            if isinstance(extra, str) and extra:
                serials[extra] = serial
        serials.update(_lsblk_serials(device.get("children"), serial))
    return serials


class Ec2Adapter:
    surface = "ec2"

    def __init__(
        self,
        run: Runner = subprocess.run,
        stat_fn: Callable[[Path], os.stat_result] = os.stat,
    ) -> None:
        self._run = run
        self._stat = stat_fn

    def _json(self, argv: tuple[str, ...]) -> Mapping[str, Any]:
        try:
            completed = self._run(
                list(argv), capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise AdapterCommandError(f"{argv[0]} unavailable: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise AdapterCommandError(
                f"{' '.join(argv)} exited {completed.returncode}: {detail or 'no stderr'}"
            )
        try:
            loaded = json.loads(completed.stdout)
        except ValueError as exc:
            raise AdapterCommandError(f"{argv[0]} printed invalid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise AdapterCommandError(f"{argv[0]} printed no object")
        return loaded

    def discover(self) -> tuple[ObservedVolume, ...]:
        """Read-only: every mounted filesystem with its fstype, source, serial and st_dev."""
        mounts = self._json(FINDMNT)
        serials = _lsblk_serials(self._json(LSBLK).get("blockdevices"))
        observed: list[ObservedVolume] = []
        for node in _walk(mounts.get("filesystems"), "children"):
            target = node.get("target")
            if not isinstance(target, str) or not target:
                continue
            try:
                device: int | None = self._stat(Path(target)).st_dev
            except OSError:
                device = None
            fstype = node.get("fstype")
            source = node.get("source")
            observed.append(
                ObservedVolume(
                    Path(target),
                    fstype if isinstance(fstype, str) else None,
                    source if isinstance(source, str) else None,
                    serials.get(target),
                    device,
                )
            )
        return tuple(observed)

    def verify(self, declared: DeclaredVolume) -> Verification:
        try:
            observed = self.discover()
        except AdapterCommandError as exc:
            return Verification("unverifiable", str(exc))
        match = next((o for o in observed if o.mount == declared.mount), None)
        if match is None:
            return Verification("mismatch", f"{declared.mount} is not mounted", "mount")
        want_fstype = declared.verify.get("fstype")
        if want_fstype and match.fstype != want_fstype:
            return Verification(
                "mismatch", f"{match.fstype!r} != declared {want_fstype!r}", "fstype"
            )
        want_serial = declared.verify.get("ebs_serial")
        if want_serial and match.serial != want_serial:
            return Verification(
                "mismatch",
                f"{match.serial!r} != declared {want_serial!r}",
                "ebs_serial",
            )
        if match.device is None:
            return Verification("unverifiable", f"cannot stat {declared.mount}")
        return Verification("ok", device=match.device)
