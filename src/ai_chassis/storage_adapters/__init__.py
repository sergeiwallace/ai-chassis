"""Storage adapters: one per machine surface, verify-only plus read-only discovery (D-2 (c))."""

from __future__ import annotations

from collections.abc import Mapping

from ..storage import REGISTERED_SURFACES, StorageAdapter, UnimplementedAdapter
from .ec2 import Ec2Adapter
from .linux import LinuxAdapter


def default_adapters() -> Mapping[str, StorageAdapter]:
    """Every registered surface maps to an adapter; unimplemented ones report `unverifiable`."""
    adapters: dict[str, StorageAdapter] = {
        surface: UnimplementedAdapter(surface) for surface in REGISTERED_SURFACES
    }
    adapters["ec2"] = Ec2Adapter()
    adapters["linux"] = LinuxAdapter()
    return adapters


__all__ = ["Ec2Adapter", "LinuxAdapter", "default_adapters"]
