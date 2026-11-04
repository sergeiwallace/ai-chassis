"""The ``linux`` storage adapter: verify a declared volume by mount, fstype and filesystem UUID.

A workstation has no EBS serial, so the hint that ties a declaration to one filesystem is the
filesystem UUID ``findmnt`` reports (``fs_uuid``). Two btrfs subvolumes of one filesystem (``/``
and ``/home`` on Fedora) share that UUID but carry different ``st_dev`` values, which is what the
containment guard compares, so each mount is still its own verified volume. Needs no root and no
``lsblk``: one ``findmnt -J`` gives target, source, fstype and UUID.
"""

from __future__ import annotations

from pathlib import Path

from ..storage import DeclaredVolume, ObservedVolume, Verification
from .ec2 import AdapterCommandError, Ec2Adapter, _walk

FINDMNT = ("findmnt", "-J", "-o", "TARGET,SOURCE,FSTYPE,UUID")


class LinuxAdapter(Ec2Adapter):
    surface = "linux"

    def _filesystems(self) -> list[dict[str, object]]:
        mounts = self._json(FINDMNT)
        return [
            node
            for node in _walk(mounts.get("filesystems"), "children")
            if isinstance(node.get("target"), str) and node.get("target")
        ]

    def discover(self) -> tuple[ObservedVolume, ...]:
        """Read-only: every mounted filesystem with its fstype, source and st_dev (no serial)."""
        observed: list[ObservedVolume] = []
        for node in self._filesystems():
            target = Path(str(node["target"]))
            try:
                device: int | None = self._stat(target).st_dev
            except OSError:
                device = None
            fstype, source = node.get("fstype"), node.get("source")
            observed.append(
                ObservedVolume(
                    target,
                    fstype if isinstance(fstype, str) else None,
                    source if isinstance(source, str) else None,
                    None,
                    device,
                )
            )
        return tuple(observed)

    def verify(self, declared: DeclaredVolume) -> Verification:
        try:
            nodes = self._filesystems()
        except AdapterCommandError as exc:
            return Verification("unverifiable", str(exc))
        match = next(
            (n for n in nodes if Path(str(n["target"])) == declared.mount), None
        )
        if match is None:
            return Verification("mismatch", f"{declared.mount} is not mounted", "mount")
        want_fstype = declared.verify.get("fstype")
        if want_fstype and match.get("fstype") != want_fstype:
            return Verification(
                "mismatch",
                f"{match.get('fstype')!r} != declared {want_fstype!r}",
                "fstype",
            )
        want_uuid = declared.verify.get("fs_uuid")
        if want_uuid and match.get("uuid") != want_uuid:
            return Verification(
                "mismatch",
                f"{match.get('uuid')!r} != declared {want_uuid!r}",
                "fs_uuid",
            )
        try:
            device = self._stat(declared.mount).st_dev
        except OSError:
            return Verification("unverifiable", f"cannot stat {declared.mount}")
        return Verification("ok", device=device)
