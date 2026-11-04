"""Shared pytest fixture for skill-CLI tests.

Consumers enable it with ``pytest_plugins = ["skill_cli_foundation.testing"]``
so every skill's precedence and failure-path tests drive one ``CliRunner``.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Yield a fresh `typer.testing.CliRunner` per test."""
    return CliRunner()
