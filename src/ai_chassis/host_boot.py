"""The explicit host-reboot signal on Linux, macOS and Windows (ISSUE-REF).

Design: ``docs/designs/issue-ref-typing-aware-launch-resume.md``, Host-reboot signal.

A boot identity is the pair (``boot_id``, ``boot_time``) one OS provider reads; the provider
is chosen by ``platform.system()`` and a caller may inject one. ``record`` stores the identity
in ``<resume_root>/launch/last-boot.json`` beside the one it last stored, so
``rebooted: true`` there is the durable "this host rebooted" signal on every OS.

Testability overrides, each read at call time: ``AIH_BOOT_ID_FILE``, ``AIH_PROC_ROOT`` and
``AIH_BOOT_TIME_EPOCH`` (read by the Linux provider, which a set ``AIH_BOOT_ID_FILE`` selects
on every OS, as ``reboot_resume`` always honoured it), ``AIH_RESUME_ROOT`` and
``AIH_REBOOT_RESUME_NOW``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

LAUNCH_DIRECTORY = "launch"
LAST_BOOT = "last-boot.json"
BOOT_STAGE = "CC-LAUNCH-RESUME-BOOT"
HOST_LOG_KEY = "host"
SYSCTL = "/usr/sbin/sysctl"
BOOTTIME_SECONDS = re.compile(r"\bsec\s*=\s*(\d+)")
# resume-lib.sh's `AIH_RESUME_LOG_MAX_LINES` default; the two trim the same files.
LOG_MAX_LINES_DEFAULT = 5000


class BootIdentityUnavailable(RuntimeError):
    """No provider could read this host's boot identity."""


@dataclass(frozen=True)
class BootIdentity:
    boot_id: str
    boot_time: int
    os: str
    source: str


class Provider(Protocol):
    def read(self) -> BootIdentity: ...


def _derived_id(boot_time: int) -> str:
    return f"boot-{boot_time}"


class LinuxProvider:
    def read(self) -> BootIdentity:
        path = Path(
            os.environ.get("AIH_BOOT_ID_FILE", "/proc/sys/kernel/random/boot_id")
        )
        try:
            boot_id = path.read_text(encoding="ascii", errors="replace").strip()
        except OSError as error:
            raise BootIdentityUnavailable(f"{path}: {error}") from error
        if not boot_id:
            raise BootIdentityUnavailable(f"{path} is empty")
        return BootIdentity(boot_id, self._boot_time(), "linux", "proc-boot-id")

    @staticmethod
    def _boot_time() -> int:
        override = os.environ.get("AIH_BOOT_TIME_EPOCH")
        if override:
            try:
                return int(override)
            except ValueError as error:
                raise BootIdentityUnavailable("AIH_BOOT_TIME_EPOCH") from error
        stat = Path(os.environ.get("AIH_PROC_ROOT", "/proc")) / "stat"
        try:
            for line in stat.read_text(encoding="ascii").splitlines():
                if line.startswith("btime "):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError) as error:
            raise BootIdentityUnavailable(f"{stat}: {error}") from error
        raise BootIdentityUnavailable(f"{stat} has no btime")


def _sysctl(argv: list[str]) -> str:
    """The read-only macOS read, refused anywhere else so no OS tool runs off Darwin."""
    if platform.system() != "Darwin":
        raise BootIdentityUnavailable("sysctl is read only on Darwin")
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


class DarwinProvider:
    def __init__(self, run: Callable[[list[str]], str] | None = None) -> None:
        self._run = run or _sysctl

    def _read(self, name: str) -> str:
        try:
            return self._run([SYSCTL, "-n", name]).strip()
        except BootIdentityUnavailable:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise BootIdentityUnavailable(f"sysctl {name}: {error}") from error

    def read(self) -> BootIdentity:
        match = BOOTTIME_SECONDS.search(self._read("kern.boottime"))
        if match is None:
            raise BootIdentityUnavailable("kern.boottime has no sec field")
        boot_time = int(match.group(1))
        try:
            uuid = self._read("kern.bootsessionuuid")
        except BootIdentityUnavailable:
            uuid = ""
        if uuid:
            return BootIdentity(uuid, boot_time, "darwin", "kern.bootsessionuuid")
        return BootIdentity(
            _derived_id(boot_time), boot_time, "darwin", "kern.boottime"
        )


def _tick_count_ms() -> int:
    if platform.system() != "Windows":
        raise BootIdentityUnavailable("GetTickCount64 is read only on Windows")
    import ctypes

    function = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
    function.restype = ctypes.c_uint64
    return int(function())


class WindowsProvider:
    def __init__(
        self,
        ticks_ms: Callable[[], int] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._ticks_ms = ticks_ms or _tick_count_ms
        self._now = now or time.time

    def read(self) -> BootIdentity:
        try:
            ticks = self._ticks_ms()
        except (OSError, AttributeError) as error:
            raise BootIdentityUnavailable(f"GetTickCount64: {error}") from error
        boot_time = round(self._now() - ticks / 1000)
        return BootIdentity(_derived_id(boot_time), boot_time, "windows", "tickcount")


PROVIDERS: dict[str, Callable[[], Provider]] = {
    "Linux": LinuxProvider,
    "Darwin": DarwinProvider,
    "Windows": WindowsProvider,
}


def identity(provider: Provider | None = None) -> BootIdentity:
    """This host's boot identity; raises :class:`BootIdentityUnavailable`, never guesses."""
    if provider is None:
        system = "Linux" if os.environ.get("AIH_BOOT_ID_FILE") else platform.system()
        factory = PROVIDERS.get(system)
        if factory is None:
            raise BootIdentityUnavailable(f"no boot identity provider for {system!r}")
        provider = factory()
    return provider.read()


# --- the durable record -----------------------------------------------------------------


def default_resume_root() -> Path:
    return Path(os.environ.get("AIH_RESUME_ROOT", Path.home() / ".claude" / "resume"))


def current_time() -> datetime:
    override = os.environ.get("AIH_REBOOT_RESUME_NOW")
    if override:
        try:
            parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def record_boot(
    resume_root: Path | None = None,
    *,
    boot: BootIdentity | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store this boot's identity; a second call on the same boot flips only ``rebooted``."""
    root = resume_root if resume_root is not None else default_resume_root()
    boot = boot if boot is not None else identity()
    moment = now if now is not None else current_time()
    path = root / LAUNCH_DIRECTORY / LAST_BOOT
    previous = read_json(path)
    if previous is not None and previous.get("boot_id") == boot.boot_id:
        record = {**previous, "rebooted": False}
    else:
        previous_id = previous.get("boot_id") if previous else None
        record = {
            **asdict(boot),
            "previous_boot_id": previous_id,
            "rebooted": previous_id is not None,
            "read_at": iso(moment),
        }
    write_json(path, record)
    return record


# --- logging ----------------------------------------------------------------------------


def _ring_trim(path: Path) -> None:
    """resume_log's ring buffer: keep the newest AIH_RESUME_LOG_MAX_LINES lines."""
    try:
        maximum = max(1, int(os.environ.get("AIH_RESUME_LOG_MAX_LINES", "")))
    except ValueError:
        maximum = LOG_MAX_LINES_DEFAULT
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= maximum:
        return
    # Never resume-lib.sh's fixed `<log>.tmp`: two trims staging under one name splice.
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text("\n".join(lines[-maximum:]) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return


def log(stage: str, key: str, detail: str) -> None:
    """One stage line to ``resume.log`` and ``logs/<key>.log``, the resume_log shape."""
    from ai_chassis.context_supervisor import _safe_marker_component, log_stage

    log_stage(stage, key, detail)
    root = default_resume_root()
    _ring_trim(root / "resume.log")
    _ring_trim(root / "logs" / f"{_safe_marker_component(key)}.log")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ai_chassis.host_boot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("identity")
    commands.add_parser("record")
    args = parser.parse_args(argv)
    try:
        boot = identity()
    except BootIdentityUnavailable as error:
        if args.command == "record":
            log(BOOT_STAGE, HOST_LOG_KEY, "reason=unavailable")
            return 0
        print(f"host_boot: {error}", file=sys.stderr)
        return 1
    if args.command == "identity":
        print(json.dumps(asdict(boot), sort_keys=True))
        return 0
    record = record_boot(boot=boot)
    log(
        BOOT_STAGE,
        HOST_LOG_KEY,
        f"rebooted={_flag(record['rebooted'])} boot_id={record['boot_id']} "
        f"previous={record.get('previous_boot_id') or 'none'}",
    )
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
