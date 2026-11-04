"""`BaseSkillSettings` -- generalized config precedence.

Hoists `mode_resolve.py`'s proven precedence chain -- CLI kwarg > env var >
per-user TOML > default -- into a reusable base. Each skill subclasses it, sets
``env_prefix`` + ``toml_file`` on ``model_config``, and declares its own fields.
Precedence is expressed as pydantic-settings *source order*: earlier sources
returned by ``settings_customise_sources`` win over later ones.
"""

from __future__ import annotations

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class BaseSkillSettings(BaseSettings):
    """Base settings with `init > env > TOML > default` source precedence."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )


class CallerSettings(BaseSkillSettings):
    """Fleet-general caller metadata with ``init > env > default`` precedence.

    Caller identity is an invocation fact, not a persisted preference.  It
    therefore deliberately has no TOML source; callers validate their own
    literal set because the permitted values vary by contract.
    """

    model_config = SettingsConfigDict(env_prefix="AIH_SKILL_", extra="ignore")

    caller: str = "direct"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings)
