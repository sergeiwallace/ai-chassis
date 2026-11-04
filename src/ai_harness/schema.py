"""Schema compatibility checks for external ai-harness configuration."""

from dataclasses import dataclass

CURRENT_SCHEMA_VERSION = 1
MINIMUM_SUPPORTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION - 1


class SchemaCompatibilityError(ValueError):
    """A configuration schema cannot be rendered by this core."""


def validate_mcp_settings(settings: object) -> None:
    """Installer and server share the packaged client/tool names and hard limits."""
    from .mcp.policy import load_policy

    if not isinstance(settings, dict):
        raise SchemaCompatibilityError("[mcp] must be a table")
    try:
        load_policy(settings)
    except ValueError:
        raise SchemaCompatibilityError("Invalid [mcp] settings") from None


@dataclass(frozen=True)
class SchemaCompatibility:
    version: int


def validate_schema_version(version: object) -> SchemaCompatibility:
    """Validate the supported N-1 through current schema range."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaCompatibilityError("config_schema_version must be an integer")
    if version < MINIMUM_SUPPORTED_SCHEMA_VERSION:
        raise SchemaCompatibilityError(
            f"config_schema_version {version} is below the supported minimum "
            f"{MINIMUM_SUPPORTED_SCHEMA_VERSION}; run 'ai-harness migrate-config'."
        )
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaCompatibilityError(
            f"config_schema_version {version} is newer than this core supports "
            f"({CURRENT_SCHEMA_VERSION}); run 'uv tool upgrade ai-harness'."
        )
    return SchemaCompatibility(version)
