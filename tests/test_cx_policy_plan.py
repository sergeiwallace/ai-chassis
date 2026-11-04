"""Phase-0 regression coverage for the immutable attached-agent policy plan."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.cx_policy_plan import (
    PolicyError,
    PolicyPlanOverrides,
    resolve_attached_policy_plan,
    resolve_agent_settings,
    resolve_policy_plan,
)


def _profile(path: Path, *, model: str = "gpt-5.6-terra") -> None:
    path.write_text(
        "\n".join(
            [
                f'model = "{model}"',
                'model_reasoning_effort = "medium"',
                'approval_policy = "never"',
                'sandbox_mode = "workspace-write"',
                'developer_instructions = "full role instructions"',
                "",
                "[sandbox_workspace_write]",
                "network_access = false",
            ]
        ),
        encoding="utf-8",
    )


def test_policy_plan_preserves_profile_instructions_and_renders_both_backends(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "implement.config.toml"
    _profile(profile)
    root = tmp_path / "worktree"
    root.mkdir()

    plan = resolve_policy_plan("implement", profile, cwd=root, effort="high")

    assert plan.developer_instructions == "full role instructions"
    assert plan.model == "gpt-5.6-terra"
    assert plan.effort == "high"
    assert plan.render_exec_argv() == [
        "-p",
        "implement",
        "-C",
        str(root),
        "-c",
        'model_reasoning_effort="high"',
    ]
    assert plan.render_app_server().thread["cwd"] == str(root)
    assert (
        plan.render_app_server().thread["developerInstructions"]
        == "full role instructions"
    )


@pytest.mark.parametrize("contents", ("not = [valid", 'model = "x"'))
def test_policy_plan_fails_closed_for_malformed_or_incomplete_profile(
    tmp_path: Path, contents: str
) -> None:
    profile = tmp_path / "implement.config.toml"
    profile.write_text(contents, encoding="utf-8")

    with pytest.raises(PolicyError):
        resolve_policy_plan("implement", profile, cwd=tmp_path)


def test_agent_config_is_call_time_toml_not_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[codex.agent]\nsupervisor_topology = "shared"\nthread_persistence = "recovery"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CX_AGENT_TOPOLOGY", "per-lease")
    monkeypatch.setenv("CX_AGENT_PERSISTENCE", "ephemeral")

    assert resolve_agent_settings(config).topology == "shared"
    assert resolve_agent_settings(config).persistence == "recovery"
    config.write_text(
        '[codex.agent]\nsupervisor_topology = "per-lease"\n', encoding="utf-8"
    )
    assert resolve_agent_settings(config).topology == "per-lease"
    assert resolve_agent_settings(config).persistence == "ephemeral"


def _attached_config(path: Path, backend: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[agents.attached]",
                f'default_backend = "{backend}"',
                "",
                f"[agents.backends.{backend}]",
                'adapter = "fake-local"'
                if backend == "fake-local"
                else 'adapter = "codex-app-server"',
                'engine = "fake"' if backend == "fake-local" else 'engine = "codex"',
                "",
                f"[agents.backends.{backend}.role_profiles]",
                'implement = "local-implement"'
                if backend == "fake-local"
                else 'implement = "implement"',
            ]
        ),
        encoding="utf-8",
    )


def test_attached_default_preserves_legacy_codex_plan_and_digest(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "implement.config.toml"
    config = tmp_path / "config.toml"
    _profile(profile)
    _attached_config(config, "codex-cloud")

    legacy = resolve_policy_plan("implement", profile, cwd=tmp_path)
    attached = resolve_attached_policy_plan(
        "implement",
        profile,
        config_path=config,
        overrides=PolicyPlanOverrides(cwd=tmp_path),
    )

    assert attached.policy == legacy
    assert attached.policy.digest == legacy.digest
    assert attached.backend_id == "codex-cloud"
    assert attached.engine == "codex"


def test_registered_fake_backend_resolves_one_complete_immutable_plan(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "implement.config.toml"
    local_profile = tmp_path / "local-implement.config.toml"
    config = tmp_path / "config.toml"
    _profile(profile)
    _profile(local_profile, model="fake-model")
    _attached_config(config, "fake-local")

    plan = resolve_attached_policy_plan(
        "implement",
        profile,
        config_path=config,
        overrides=PolicyPlanOverrides(cwd=tmp_path),
        adapter_kinds={"codex-app-server", "fake-local"},
    )

    assert (plan.backend_id, plan.adapter_kind, plan.engine, plan.profile_id) == (
        "fake-local",
        "fake-local",
        "fake",
        "local-implement",
    )
    assert (plan.model, plan.effort, plan.sandbox_mode) == (
        "fake-model",
        "medium",
        "workspace-write",
    )
    assert plan.sandbox_mechanism == "sandbox_mode"
    assert plan.network_posture == "disabled"
    assert plan.digest.startswith("sha256:")


def test_registered_ollama_backend_uses_the_local_mechanical_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "mechanical.config.toml"
    local_profile = tmp_path / "local.config.toml"
    config = tmp_path / "config.toml"
    _profile(profile)
    _profile(local_profile, model="qwen-local")
    config.write_text(
        "\n".join(
            [
                "[agents.attached]",
                'default_backend = "codex-cloud"',
                "",
                "[agents.backends.codex-local-ollama]",
                'adapter = "codex-ollama-exec"',
                'engine = "codex-local"',
                "",
                "[agents.backends.codex-local-ollama.role_profiles]",
                'mechanical = "local"',
            ]
        ),
        encoding="utf-8",
    )

    plan = resolve_attached_policy_plan(
        "mechanical",
        profile,
        config_path=config,
        backend_id="codex-local-ollama",
        overrides=PolicyPlanOverrides(cwd=tmp_path),
    )

    assert (plan.backend_id, plan.adapter_kind, plan.engine, plan.profile_id) == (
        "codex-local-ollama",
        "codex-ollama-exec",
        "codex-local",
        "local",
    )
    assert plan.model == "qwen-local"


@pytest.mark.parametrize(
    "backend, extra, adapter_kinds",
    [
        ("codex-cloud", 'command = "unsafe shell"', {"codex-app-server"}),
        ("codex-cloud", 'adapter = "unknown"', {"codex-app-server"}),
        ("fake-local", "", {"codex-app-server", "fake-local"}),
    ],
)
def test_attached_resolution_rejects_unsafe_or_missing_profiles_before_start(
    tmp_path: Path, backend: str, extra: str, adapter_kinds: set[str]
) -> None:
    profile = tmp_path / "implement.config.toml"
    config = tmp_path / "config.toml"
    _profile(profile)
    _attached_config(config, backend)
    if extra:
        contents = config.read_text(encoding="utf-8")
        if extra.startswith("adapter"):
            config.write_text(
                contents.replace('adapter = "codex-app-server"', extra),
                encoding="utf-8",
            )
        else:
            table = f"[agents.backends.{backend}.role_profiles]"
            config.write_text(
                contents.replace(table, f"{extra}\n\n{table}"), encoding="utf-8"
            )
    if backend == "fake-local":
        # The configured mapping points at local-implement, which deliberately does not exist.
        assert not (tmp_path / "local-implement.config.toml").exists()

    with pytest.raises(PolicyError):
        resolve_attached_policy_plan(
            "implement",
            profile,
            config_path=config,
            adapter_kinds=adapter_kinds,
            overrides=PolicyPlanOverrides(cwd=tmp_path),
        )
