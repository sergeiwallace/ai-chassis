"""Bounded observation of installer-owned MCP processes, never existing sessions."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import psutil

from .restart_mcp import Server, Unverified, identities
from .manual_steps import sanitize

SETTLE_SECONDS = 1.0
TIMEOUT_SECONDS = 5.0
SAMPLE_SECONDS = 0.1
STABLE_SAMPLES = 3


def _package_name(server: Server) -> str:
    args = list(server.args)
    while args and args[0] in {"-y", "--yes", "--no", "--offline", "--"}:
        args.pop(0)
    if not args or not (
        match := re.fullmatch(
            r"(@[^/@\s]+/[^/@\s]+|[^/@\s-][^/@\s]*)(?:@[^/\s]+)?", args[0]
        )
    ):
        raise Unverified("npm package specification unsupported")
    return match[1]


def npm_installations(server: Server, home: Path) -> list[Server]:
    """Expand an npx package into independently verified, canonical installations."""
    if server.blocked or Path(server.command).stem not in {"npx", "npx-cli"}:
        return [server]
    package = _package_name(server)
    root = _npm_root(home)
    cache = root / "_npx" if root else home / ".npm/_npx"
    candidates = []
    try:
        if cache.is_symlink():
            raise Unverified("npm _npx cache is not canonical")
        for installation in sorted(cache.iterdir()) if cache.is_dir() else []:
            manifest = installation / "node_modules" / package / "package.json"
            if not manifest.is_file():
                continue
            if (
                installation.resolve() != installation
                or not manifest.resolve().is_relative_to(installation)
            ):
                raise Unverified("npm installation escapes _npx cache")
            data = json.loads(manifest.read_bytes())
            if data.get("name") != package:
                continue
            candidates.append(
                replace(
                    server,
                    name=server.name
                    if not candidates
                    else f"{server.name}:npx:{hashlib.sha256(installation.name.encode()).hexdigest()[:16]}",
                    installation=installation,
                    package=package,
                )
            )
    except Unverified:
        raise
    except (OSError, ValueError, AttributeError):
        raise Unverified("npm _npx package metadata unreadable") from None
    if not candidates:
        raise Unverified("npm _npx cache has no installed package")
    return candidates


def _metadata(server: Server) -> dict:
    assert server.installation is not None
    manifest = server.installation / "node_modules" / server.package / "package.json"
    try:
        data = json.loads(manifest.read_bytes())
        version, name, binary = data["version"], data["name"], data["bin"]
        if (
            name != server.package
            or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.+-]+)?", version)
        ):
            raise ValueError
        if not isinstance(binary, (str, dict)):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise Unverified("npm _npx package metadata unreadable") from None
    # No user config, lockfile or registry response contributes metadata.
    package = {
        "name": name,
        "version": version,
        "bin": binary,
        "dist": {
            "tarball": f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
        },
    }
    return {
        "name": name,
        "dist-tags": {"latest": version},
        "versions": {version: package},
    }


def _registry_main():
    """Executed only INSIDE bwrap's private network namespace, with stdlib alone."""
    import http.server
    import json
    import os
    import subprocess
    import sys
    import threading
    import urllib.parse

    metadata = json.loads(sys.argv[1])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.unquote(self.path) != "/" + metadata["name"]:
                self.send_error(404)
                return
            body = json.dumps(metadata).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args) -> None:
            _ = format

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as registry:
        os.environ["npm_config_registry"] = f"http://127.0.0.1:{registry.server_port}"
        threading.Thread(target=registry.serve_forever, daemon=True).start()
        # No shell, no argv rewriting, and stdin remains open from the probe.
        with subprocess.Popen(sys.argv[3:], cwd=sys.argv[2]) as child:
            result = child.wait()
        registry.shutdown()
    sys.exit(result)


def _registry_code() -> str:
    return inspect.getsource(_registry_main) + "\n_registry_main()\n"


def command_path(command: str, cwd: Path) -> Path:
    path = Path(command).expanduser()
    if not path.is_absolute():
        found = shutil.which(command) if path.parent == Path() else cwd / path
        if not found:
            raise Unverified("command unavailable")
        path = Path(found)
    try:
        path.resolve(strict=True)
        return path
    except OSError:
        raise Unverified("command unavailable") from None


def _npm_root(home: Path) -> Path | None:
    """The directory ``home/.npm`` resolves to, or None when there is none.

    A host may symlink ~/.npm onto another volume (example-project-ec2: /data/cache/npm), so the
    resolved directory is the cache. One that resolves nowhere, to a non-directory or
    to another user's directory is refused (ISSUE-REF)."""
    npm = home / ".npm"
    if not (npm.is_symlink() or npm.exists()):
        return None
    try:
        npm = npm.resolve(strict=True)
        canonical = npm.is_dir() and npm.stat().st_uid == os.getuid()
    except OSError:
        canonical = False
    if not canonical:
        raise Unverified("npm cache is not canonical")
    return npm


def _sandbox(argv: list[str], temporary: Path) -> list[str]:
    """No network, no writes outside the disposable working directory."""
    if sys.platform == "linux":
        binary = shutil.which("bwrap")
        if not binary:
            raise Unverified("network isolation unavailable (bwrap required)")
        npm = _npm_root(Path.home())
        masks = []
        if npm:
            masks = ["--tmpfs", str(npm)]
            if (npm / "_npx").is_symlink():
                raise Unverified("npm _npx cache is not canonical")
            if (npm / "_npx").is_dir():
                masks += ["--ro-bind", str(npm / "_npx"), str(npm / "_npx")]
            masks += ["--remount-ro", str(npm)]
        npmrc = Path.home() / ".npmrc"
        if npmrc.exists():
            masks += ["--ro-bind", "/dev/null", str(npmrc)]
        return [
            binary,
            "--die-with-parent",
            "--unshare-net",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--bind",
            str(temporary),
            str(temporary),
            *masks,
            "--chdir",
            str(temporary),
            "--",
            *argv,
        ]
    if sys.platform == "darwin":
        binary = shutil.which("sandbox-exec")
        if not binary:
            raise Unverified("network isolation unavailable (sandbox-exec required)")
        # Path is generated by tempfile, never interpolated from configuration.
        policy = (
            "(version 1)(allow default)(deny network*)(deny file-write*)"
            f'(allow file-write* (subpath "{temporary}"))'
        )
        return [binary, "-p", policy, *argv]
    raise Unverified("unsupported observation platform")


def _root(owned: psutil.Process) -> psutil.Process:
    """Unwrap only the sandbox we started, never choose an arbitrary descendant."""
    root = owned
    while Path(root.exe()).name in {"bwrap", "sandbox-exec"} or root.cmdline()[1:3] == [
        "-c",
        _registry_code(),
    ]:
        children = root.children()
        if len(children) != 1:
            raise Unverified("sandbox startup incomplete")
        root = children[0]
    return root


def _files(processes: list[psutil.Process], cwd: Path) -> list[Path]:
    files = []
    for process in processes:
        files.append(Path(process.exe()).resolve(strict=True))
        for arg in process.cmdline():
            if not arg or arg.startswith("-"):
                continue
            candidate = Path(arg)
            candidate = candidate if candidate.is_absolute() else cwd / candidate
            if candidate.is_file():
                files.append(candidate.resolve(strict=True))
    return files


def _check_wrapper_exec(
    configured: Path, argv: list[str], executable: Path, cwd: Path
) -> None:
    with configured.open("rb") as source:
        header = source.readline(256)
    if not header.startswith(b"#!"):
        return
    try:
        interpreter = shlex.split(header[2:].decode("utf-8"))
    except ValueError:
        raise Unverified("launcher shebang unreadable") from None
    if not any(
        Path(word).name in {"bash", "sh", "dash", "zsh", "ksh"} for word in interpreter
    ):
        return
    try:
        invoked = command_path(argv[0], cwd).resolve(strict=True)
    except Unverified:
        invoked = None
    if invoked != executable:
        # The shell's exec target is gone from argv. Its implementation must not
        # be guessed from a rewritten title or from an unrelated child script.
        raise Unverified("wrapper exec target hidden by rewritten runtime title")


def _snapshot(owned: psutil.Process, configured: Path, cwd: Path) -> dict:
    root = _root(owned)
    argv = root.cmdline()
    if not argv or not argv[0]:
        raise Unverified("runtime argv unavailable")
    text = " ".join(argv)
    if sanitize(text) != text:
        raise Unverified("credential-bearing runtime arguments refused")
    executable = Path(root.exe()).resolve(strict=True)
    _check_wrapper_exec(configured, argv, executable, cwd)
    processes = [root, *root.children(recursive=True)]
    files = _files(processes, cwd)
    # npm rewrites the launcher title, so the actual installed tool entry must
    # be visible in its owned child tree. Never infer it from a package name.
    if configured.stem in {"npm", "npx", "npm-cli", "npx-cli"} and not any(
        p.suffix in {".js", ".mjs", ".cjs"} and p != configured for p in files
    ):
        raise Unverified("installed tool entry script not observable")
    stamps = identities([configured, executable, *files])
    return {"executable": str(executable), "argv": argv, "identities": stamps}


def _stop(owned: psutil.Process, child: subprocess.Popen) -> None:
    """Signal specific owned Process objects (psutil also checks PID reuse)."""
    try:
        processes = [*reversed(owned.children(recursive=True)), owned]
    except psutil.NoSuchProcess:
        processes = []
    except psutil.Error:
        processes = [owned]
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=0.5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=0.5)
    child.wait(timeout=1)
    if alive:
        raise Unverified("owned process cleanup incomplete")


def _settled(owned: psutil.Process, configured: Path, cwd: Path) -> dict:
    started = time.monotonic()
    previous = None
    stable = 0
    reason = "runtime shape unstable"
    while time.monotonic() - started < TIMEOUT_SECONDS:
        time.sleep(SAMPLE_SECONDS)
        try:
            current = _snapshot(owned, configured, cwd)
        except Unverified as exc:
            reason = str(exc)
            previous = None
            stable = 0
            continue
        stable = stable + 1 if current == previous else 1
        previous = current
        if stable >= STABLE_SAMPLES and time.monotonic() - started >= SETTLE_SECONDS:
            return current
    raise Unverified(f"runtime did not settle within observation bound: {reason}")


def observe(server: Server, temporary: Path) -> dict:
    """Start the configured command once; retain the complete observed argv."""
    if server.blocked:
        raise Unverified(server.blocked)
    command = command_path(server.command, server.cwd)
    configured = command.resolve(strict=True)
    watched = [configured]
    invocation = [str(command), *server.args]
    cwd = temporary
    if server.installation is not None:
        if sys.platform != "linux":
            raise Unverified("npm registry probe requires a private network namespace")
        metadata = _metadata(server)
        watched.append(
            server.installation / "node_modules" / server.package / "package.json"
        )
        cwd = server.installation
        invocation = [
            sys.executable,
            "-c",
            _registry_code(),
            json.dumps(metadata),
            str(cwd),
            *invocation,
        ]
    before = identities(watched)
    argv = _sandbox(invocation, temporary)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(temporary),
        "TMPDIR": str(temporary),
        "XDG_CONFIG_HOME": str(temporary / "config"),
        "XDG_CACHE_HOME": str(temporary / "cache"),
        "XDG_STATE_HOME": str(temporary / "state"),
        "npm_config_offline": "false" if server.installation else "true",
        "npm_config_logs_dir": str(temporary / "npm-logs"),
        "npm_config_cache": str(temporary / "npm-cache"),
        "npm_config_userconfig": str(temporary / "npmrc"),
        "npm_config_globalconfig": str(temporary / "npm-globalrc"),
        "npm_config_fetch_retries": "0",
        "npm_config_update_notifier": "false",
    }
    # Only receipt-verified core registrations supply this non-secret setting.
    # All other configured environments remain blocked by discovery.
    env.update(
        {"AI_HARNESS_CONFIG_HOME": str(server.config_home)}
        if server.config_home is not None
        else {}
    )
    try:
        child = subprocess.Popen(
            argv,
            cwd=temporary,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise Unverified("isolated startup failed") from None
    owned = None
    try:
        owned = psutil.Process(child.pid)
        result = _settled(owned, configured, cwd)
        if before != identities(watched):
            raise Unverified("implementation changed during observation")
        return _npm_result(server, result, watched)
    except (OSError, psutil.Error, subprocess.SubprocessError):
        raise Unverified(
            "isolated observation failed (credentials/network unavailable)"
        ) from None
    finally:
        try:
            if owned is None:
                child.kill()
                child.wait(timeout=1)
            else:
                _stop(owned, child)
        except (OSError, psutil.Error, subprocess.SubprocessError):
            raise Unverified("owned process cleanup incomplete") from None
        finally:
            if child.stdin is not None:
                child.stdin.close()


def _npm_result(server: Server, result: dict, watched: list[Path]) -> dict:
    if server.installation is not None:
        package_root = server.installation / "node_modules" / server.package
        paths = [Path(stamp["path"]) for stamp in result["identities"]]
        if not any(
            path.is_relative_to(package_root) and path.suffix in {".js", ".mjs", ".cjs"}
            for path in paths
        ):
            raise Unverified("selected npm installation entry script not observable")
        result["identities"] = identities([*watched, *paths])
    return result


def verify(server: Server) -> dict:
    """Independent starts must agree on both shape and implementation stamps."""
    with tempfile.TemporaryDirectory(prefix="aih-mcp-") as directory:
        temporary = Path(directory).resolve()
        first = observe(server, temporary)
        second = observe(server, temporary)
        if first != second:
            raise Unverified("runtime shape or implementation differs between starts")
        if any(
            Path(stamp["path"]).is_relative_to(temporary)
            for stamp in first["identities"]
        ):
            raise Unverified(
                "runtime implementation exists only in the temporary directory"
            )
        if any(str(temporary) in argument for argument in first["argv"]):
            raise Unverified("runtime argv depends on the temporary directory")
    return {"name": server.name, **first}
