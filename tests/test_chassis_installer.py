"""The bootstrap installer: choosing a release, placing the checkout, installing the tool.

Everything runs against a local git repository standing in for the remote and a stand-in
``uv`` that records its calls, with ``HOME`` inside the test directory, so nothing outside it
is touched and no network is used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# In the source tree the public installer sits in export-docs (the root one is the private
# machine bootstrap); in the published repository it is the root install.sh.
USAGE_ERROR = 2
INSTALLER = next(
    path
    for path in (ROOT / "export-docs" / "install.sh", ROOT / "install.sh")
    if path.is_file()
)
TOOLS = (
    "bash",
    "git",
    "sed",
    "grep",
    "head",
    "cp",
    "mkdir",
    "basename",
    "dirname",
    "cat",
    "uname",
    "env",
    "sh",
    "tr",
    "id",
    "chmod",
    "sort",
    "mktemp",
    "rm",
)

FAKE_UV = """#!/usr/bin/env bash
echo "uv $*" >> "$FAKE_LOG"
if [ "$1 $2 $3" = "tool dir --bin" ]; then
  echo "$HOME/.local/bin"
  exit 0
fi
if [ "$1 $2" = "tool install" ]; then
  echo "UV_TOOL_BIN_DIR=${UV_TOOL_BIN_DIR:-unset}" >> "$FAKE_LOG"
  target="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
  mkdir -p "$target"
  printf '#!/usr/bin/env bash\\necho "ai-chassis $*" >> "$FAKE_LOG"\\n' > "$target/ai-chassis"
  chmod +x "$target/ai-chassis"
fi
"""


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return value or ""


def _run(
    argv: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command, through the managed spawn helper when the source test plugin loaded it.

    The helper is looked up in ``sys.modules`` rather than imported, so this file stays
    free of a dependency on a package the published repository does not carry.
    """
    spawn = sys.modules.get("fleet_testkit.spawn")
    managed = None
    if spawn is not None:
        try:
            managed = spawn.run(argv, env=env)
        except Exception as error:
            if type(error).__name__ != "SupervisorError":
                raise
    if managed is not None:
        return subprocess.CompletedProcess(
            argv, managed.returncode, _text(managed.stdout), _text(managed.stderr)
        )
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, env=env, check=False
    )


def git(cwd: Path, *args: str) -> str:
    result = _run(["git", "-C", str(cwd), *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "remote"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgsign", "false")
    for tag in ("v0.1.0", "v0.2.0", "v0.3.0-rc1"):
        (path / "VERSION").write_text(tag + "\n")
        git(path, "add", "VERSION")
        git(path, "commit", "-q", "-m", tag)
        git(path, "tag", tag)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _tool_dir(tmp_path: Path, *, with_uv: bool) -> Path:
    path = tmp_path / ("tools-uv" if with_uv else "tools")
    path.mkdir(exist_ok=True)
    for name in TOOLS:
        found = shutil.which(name)
        assert found, name
        link = path / name
        if not link.exists():
            link.symlink_to(found)
    if with_uv:
        fake = path / "uv"
        fake.write_text(FAKE_UV)
        fake.chmod(0o755)
    return path


class Installer:
    def __init__(self, tmp_path: Path, home: Path, remote: Path) -> None:
        self.home = home
        self.remote = remote
        self.log = tmp_path / "calls.log"
        self.log.write_text("")
        self.checkout = tmp_path / "checkout"
        self.tmp_path = tmp_path

    def run(
        self, *args: str, with_uv: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        tools = _tool_dir(self.tmp_path, with_uv=with_uv)
        environment = {
            "PATH": str(tools),
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            **(env or {}),
        }
        return _run(
            [
                str(tools / "bash"),
                str(INSTALLER),
                "--repo",
                str(self.remote),
                "--dir",
                str(self.checkout),
                *args,
            ],
            environment,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def installer(tmp_path: Path, home: Path, remote: Path) -> Installer:
    return Installer(tmp_path, home, remote)


def test_help_lists_the_options(installer: Installer) -> None:
    result = installer.run("--help")
    assert result.returncode == 0
    for option in (
        "--ref",
        "--dir",
        "--bin-dir",
        "--config-dir",
        "--templates",
        "--dry-run",
    ):
        assert option in result.stdout


def test_an_unknown_option_is_a_usage_error(installer: Installer) -> None:
    result = installer.run("--no-such-option")
    assert result.returncode == USAGE_ERROR
    assert "unknown option" in result.stderr


def test_an_option_without_its_value_is_a_usage_error(installer: Installer) -> None:
    result = installer.run("--ref")
    assert result.returncode == USAGE_ERROR
    assert "needs a value" in result.stderr


def test_a_dry_run_names_the_steps_and_changes_nothing(installer: Installer) -> None:
    result = installer.run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "git clone" in result.stdout
    assert "uv tool install --force --editable" in result.stdout
    assert not installer.checkout.exists()
    assert installer.calls() == []


def test_the_newest_stable_release_is_installed_by_default(
    installer: Installer,
) -> None:
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert (installer.checkout / "VERSION").read_text().strip() == "v0.2.0"
    assert (
        f"uv tool install --force --editable {installer.checkout}" in installer.calls()
    )


def test_a_named_ref_is_installed(installer: Installer) -> None:
    result = installer.run("--ref", "v0.1.0")
    assert result.returncode == 0, result.stderr
    assert (installer.checkout / "VERSION").read_text().strip() == "v0.1.0"


def test_running_again_moves_the_checkout_to_the_new_release(
    installer: Installer, remote: Path
) -> None:
    assert installer.run().returncode == 0
    (remote / "VERSION").write_text("v0.4.0\n")
    git(remote, "add", "VERSION")
    git(remote, "commit", "-q", "-m", "v0.4.0")
    git(remote, "tag", "v0.4.0")
    assert installer.run().returncode == 0
    assert (installer.checkout / "VERSION").read_text().strip() == "v0.4.0"


def test_a_branch_ref_follows_the_remote(installer: Installer, remote: Path) -> None:
    assert installer.run("--ref", "main").returncode == 0
    (remote / "VERSION").write_text("newer\n")
    git(remote, "add", "VERSION")
    git(remote, "commit", "-q", "-m", "newer")
    assert installer.run("--ref", "main").returncode == 0
    assert (installer.checkout / "VERSION").read_text().strip() == "newer"


def test_the_initial_configuration_is_created_once(
    installer: Installer, home: Path
) -> None:
    assert installer.run().returncode == 0
    assert "ai-chassis init" in installer.calls()
    config = home / ".config" / "ai-harness"
    config.mkdir(parents=True)
    (config / "ai-harness.toml").write_text("config_schema_version = 1\n")
    installer.log.write_text("")
    result = installer.run()
    assert result.returncode == 0
    assert "keeping existing configuration" in result.stdout
    assert "ai-chassis init" not in installer.calls()


def test_no_init_skips_the_configuration(installer: Installer) -> None:
    assert installer.run("--no-init").returncode == 0
    assert not any(call.startswith("ai-chassis") for call in installer.calls())


def test_bin_dir_and_config_dir_are_passed_through(
    installer: Installer, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    config_dir = tmp_path / "config-here"
    result = installer.run("--bin-dir", str(bin_dir), "--config-dir", str(config_dir))
    assert result.returncode == 0, result.stderr
    assert f"UV_TOOL_BIN_DIR={bin_dir}" in installer.calls()
    assert f"ai-chassis --config {config_dir} init" in installer.calls()


def test_templates_are_copied_only_where_the_file_is_absent(
    installer: Installer, tmp_path: Path
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "new.toml").write_text("from-template\n")
    (templates / "kept.toml").write_text("from-template\n")
    assert installer.run().returncode == 0
    config = installer.checkout / "config"
    config.mkdir(exist_ok=True)
    (config / "kept.toml").write_text("local edit\n")
    result = installer.run("--templates", str(templates), "--no-init")
    assert result.returncode == 0, result.stderr
    assert (config / "new.toml").read_text() == "from-template\n"
    assert (config / "kept.toml").read_text() == "local edit\n"


def test_a_missing_templates_directory_is_refused(
    installer: Installer, tmp_path: Path
) -> None:
    result = installer.run("--templates", str(tmp_path / "nowhere"))
    assert result.returncode == 1
    assert "templates directory not found" in result.stderr


FAKE_CURL = """#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift; fi
  shift
done
echo "curl download" >> "$FAKE_LOG"
if [ "${FAKE_CURL_FAIL:-0}" = "1" ]; then
  # The connection drops after a fragment that is, by itself, a runnable script.
  printf '#!/bin/sh\\necho uv-installer-ran-partial >> "$FAKE_LOG"\\n' > "$out"
  exit 18
fi
cat > "$out" <<'SCRIPT'
#!/bin/sh
echo uv-installer-ran >> "$FAKE_LOG"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uv" <<'UV'
%s
UV
chmod +x "$HOME/.local/bin/uv"
SCRIPT
""" % FAKE_UV.rstrip("\n")


def _with_fake_curl(installer: Installer) -> None:
    tools = _tool_dir(installer.tmp_path, with_uv=False)
    curl = tools / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)


def test_install_uv_runs_the_downloaded_installer_after_a_complete_download(
    installer: Installer,
) -> None:
    _with_fake_curl(installer)
    result = installer.run("--install-uv", with_uv=False)
    assert result.returncode == 0, result.stderr
    calls = installer.calls()
    assert calls.index("curl download") < calls.index("uv-installer-ran")
    assert any(call.startswith("uv tool install") for call in calls)


def test_an_interrupted_uv_download_runs_nothing(installer: Installer) -> None:
    _with_fake_curl(installer)
    result = installer.run("--install-uv", with_uv=False, env={"FAKE_CURL_FAIL": "1"})
    assert result.returncode == 1
    assert "nothing was run" in result.stderr
    assert not any("uv-installer-ran" in call for call in installer.calls())
    assert not installer.checkout.exists()


def test_a_checkout_of_another_repository_is_refused(
    installer: Installer, tmp_path: Path
) -> None:
    assert installer.run().returncode == 0
    other = tmp_path / "other-remote"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", "user.email", "test@example.invalid")
    git(other, "config", "user.name", "Test")
    git(other, "config", "commit.gpgsign", "false")
    git(other, "config", "tag.gpgsign", "false")
    (other / "VERSION").write_text("v0.2.0\n")
    git(other, "add", "VERSION")
    git(other, "commit", "-q", "-m", "v0.2.0")
    git(other, "tag", "v0.2.0")
    before = git(installer.checkout, "rev-parse", "HEAD")
    result = _run(
        [
            str(_tool_dir(tmp_path, with_uv=True) / "bash"),
            str(INSTALLER),
            "--repo",
            str(other),
            "--dir",
            str(installer.checkout),
        ],
        {
            "PATH": str(_tool_dir(tmp_path, with_uv=True)),
            "HOME": str(installer.home),
            "FAKE_LOG": str(installer.log),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    assert result.returncode == 1
    assert "not " + str(other) in result.stderr
    assert git(installer.checkout, "rev-parse", "HEAD") == before


def test_a_missing_uv_is_reported_with_the_way_forward(installer: Installer) -> None:
    result = installer.run(with_uv=False)
    assert result.returncode == 1
    assert "--install-uv" in result.stderr
    assert not installer.checkout.exists()


def test_a_repository_without_a_release_tag_asks_for_a_ref(
    installer: Installer, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-remote"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    git(empty, "config", "user.email", "test@example.invalid")
    git(empty, "config", "user.name", "Test")
    git(empty, "config", "commit.gpgsign", "false")
    git(empty, "commit", "-q", "--allow-empty", "-m", "start")
    installer.remote = empty
    result = installer.run()
    assert result.returncode == 1
    assert "--ref" in result.stderr
