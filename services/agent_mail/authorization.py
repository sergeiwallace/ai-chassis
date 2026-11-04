from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Board-scoped namespace for the enrolled example-client-host machines (example-project-ec2, example-client-host-mac). Renamed from the
# retired machine name "example-project-sagemaker" under ISSUE-REF (the maintainer ruled option (a), 2026-10-08);
# a registry row enrolled under the old value no longer matches and must be re-enrolled.
BMS_NAMESPACE = "example-client-host"
ALL_SURFACES = frozenset(
    {"http", "mcp", "web_ui", "search", "events", "attachments", "admin"}
)


class AccessDenied(PermissionError):
    """A fail-closed machine admission or namespace decision."""


@dataclass(frozen=True)
class TransportIdentity:
    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in {"mtls", "tailscale"} or not self.value.strip():
            raise ValueError(
                "transport identity must be a non-empty mTLS fingerprint or Tailscale public key"
            )


def authorize_resource(
    *,
    requester_namespace: str,
    resource_namespace: str,
    surface: str,
    classifier_advisory: dict[str, Any] | None = None,
) -> None:
    """Apply the same structural namespace boundary to every exposed surface.

    ``classifier_advisory`` is deliberately ignored for authorization. It is accepted only so
    callers cannot accidentally turn future classifier output into release authority.
    """

    del classifier_advisory
    if surface not in ALL_SURFACES:
        raise AccessDenied("unknown access surface")
    if resource_namespace == BMS_NAMESPACE and requester_namespace != BMS_NAMESPACE:
        raise AccessDenied("cross-machine access to the example-client-host namespace is denied")
