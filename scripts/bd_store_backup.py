# ruff: noqa: E402
"""Back up a Beads store's DOLT DATABASE, and verify the backup by reading it back.

ISSUE-REF. A backup is the precondition the maintainer attached to any store migration ("back the
data up first"), and it is the step the skew-preflight's refusal points at. This exists so
that instruction has something to name.

THE HARD PART IS FINDING WHAT TO COPY, AND THE OBVIOUS ANSWERS ARE BOTH WRONG.

  * ``bd info`` reports ``Database: <repo>/.beads/dolt``. Measured on example-project-ec2
    2026-09-27, for both ai-harness and example-client-host-workspace, THAT PATH DOES NOT EXIST. It is a
    nominal path, and a backup of it is a backup of nothing.
  * ``<repo>/.beads/embeddeddolt/<db>`` does exist for example-client-host-workspace and holds 292 MB,
    which makes it look like the store. Its newest file was written 2026-09-16; the live
    database had been written 11 days later, minutes before the measurement. It is a stale
    legacy embedded copy, and backing it up would silently lose every issue since.
  * ``.beads/issues.jsonl`` is a MIRROR, not the store, and it is known stale in BOTH
    directions -- the ai-harness mirror holds 169 JSONL-only records the Dolt store does not
    have, which is why bd's auto-export refuses outright. A JSONL-only backup is not a
    backup of the store.

The live data for a ``server``-mode store is in the shared dolt sql-server's own data-dir,
outside the repo entirely: ``$HOME/.beads/shared-server/dolt/<dolt_database>``. That
resolution is not invented here -- it is the one ``beads_machine_setup.sh`` already uses to
decide whether a store's database exists, and ``scripts/dolt_server.py`` already declares
the path as ``SHARED_SERVER_SUBPATH``. Both are read from ``.beads/metadata.json``
(``dolt_mode`` and ``dolt_database``), which is where bd records it.

A BACKUP NOBODY HAS RESTORED FROM IS A HYPOTHESIS, so this verifies by reading the COPY,
offline, with a second implementation -- ``dolt sql`` run inside the copied database
directory rather than ``bd`` through the server -- and compares the row count against the
live store. Measured on the smallest store as a calibration before this was written:
``bd list --all -n 0 --json`` returned 36 for project-template and the offline read of a
fresh copy returned 36. The two numbers are directly comparable, so a mismatch is real
evidence of a torn or stale copy rather than a units problem.

WHY VERIFICATION IS NOT OPTIONAL HERE. The copy is taken while the sql-server is live and
may be mid-journal, and nothing in a plain ``cp -a`` would tell you. The read-back IS the
thing that distinguishes a backup from a directory.
"""

from __future__ import annotations

try:
    from py_interpreter_bootstrap import ensure_supported_python
except ModuleNotFoundError:  # a copy of this script that did not carry its sibling

    def ensure_supported_python(minimum=(3, 11)):
        """Stand in for the bootstrap when this file was copied without it.

        A partial copy cannot re-exec, because the module that knows how is the one
        missing. Staying silent when the running interpreter ALREADY meets the floor is
        correct rather than lax: there was nothing to enforce. Below the floor there is
        no remedy left to apply, so say so and let the real failure happen where it can
        be read, instead of dying here on an import that is not the problem.
        """
        import sys as _sys

        if _sys.version_info < minimum:
            _sys.stderr.write(
                "warning: {} is running under Python {} without its interpreter "
                "bootstrap, so the {}.{}+ floor could not be enforced\n".format(
                    __file__,
                    ".".join(str(p) for p in _sys.version_info[:3]),
                    minimum[0],
                    minimum[1],
                )
            )


ensure_supported_python()

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bd_common import FULL_STORE_LIST_ARGS, bd_env  # noqa: E402

# Mirrors scripts/dolt_server.py's SHARED_SERVER_SUBPATH. Duplicated rather than imported to
# keep this runnable under a bare system python3, the same constraint the preflight carries.
SHARED_SERVER_SUBPATH = (".beads", "shared-server", "dolt")
EMBEDDED_SUBPATH = (".beads", "embeddeddolt")

# Sidecar files worth capturing next to the database. Not the store, but a restore needs
# them to point bd back at what was restored.
SIDECARS = ("config.yaml", "metadata.json", ".local_version", "issues.jsonl")

# A Dolt database name as bd writes one (AIH, CORE, AI_CLI, sw, ...): one path segment.
PLAIN_DATABASE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9_-]*\Z")

EXIT_OK = 0
EXIT_RESOLVE_FAILED = 2
EXIT_COPY_FAILED = 3
EXIT_VERIFY_FAILED = 4


@dataclass
class StoreLocation:
    """Where a repo's live Dolt database actually is."""

    repo: Path
    dolt_database: str
    dolt_mode: str
    database_dir: Path

    @property
    def shared(self) -> bool:
        return self.dolt_mode == "server"


def declared_database(repo: Path) -> str | None:
    """The Dolt database ``repo`` declares in bd's metadata, or None when it declares none.

    Only a missing metadata file is an absence. A file that is not JSON raises
    ``ValueError`` and any other read failure raises ``OSError``, so a caller can
    never mistake an unreadable store for one the repo does not hold (ISSUE-REF:
    the wake beads handler asks this through the registered publisher, so the
    store path stays out of the public core).
    """
    try:
        text = (repo / ".beads" / "metadata.json").read_text()
    except (FileNotFoundError, NotADirectoryError):
        return None
    metadata = json.loads(text)
    return metadata.get("dolt_database") if isinstance(metadata, dict) else None


def resolve_location(repo: Path, *, home: Path | None = None) -> StoreLocation:
    """Return where ``repo``'s live database is, from bd's own metadata.

    Raises ``FileNotFoundError`` when the metadata or the resolved directory is absent --
    never falls back to a guess, because every plausible guess here is one of the three
    wrong answers in the module docstring.
    """
    home = Path.home() if home is None else home
    metadata_path = repo / ".beads" / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"cannot read {metadata_path}: {exc}") from exc

    dolt_database = metadata.get("dolt_database")
    if not dolt_database:
        raise FileNotFoundError(f"{metadata_path} declares no dolt_database")
    if not isinstance(dolt_database, str) or not PLAIN_DATABASE.match(dolt_database):
        # Tracked metadata becomes a path component below; `..` or a separator in it would
        # point the lookup, and every backup or lock built on it, outside the store root.
        raise FileNotFoundError(
            f"{metadata_path} declares a dolt_database that is not a plain name: "
            f"{dolt_database!r}"
        )
    dolt_mode = str(metadata.get("dolt_mode") or "")

    if dolt_mode == "server":
        database_dir = home.joinpath(*SHARED_SERVER_SUBPATH) / dolt_database
    else:
        database_dir = repo.joinpath(*EMBEDDED_SUBPATH) / dolt_database

    if not (database_dir / ".dolt").is_dir():
        raise FileNotFoundError(
            f"resolved database dir has no .dolt: {database_dir} "
            f"(dolt_mode={dolt_mode!r}, dolt_database={dolt_database!r})"
        )
    return StoreLocation(
        repo=repo,
        dolt_database=dolt_database,
        dolt_mode=dolt_mode,
        database_dir=database_dir,
    )


def live_issue_count(repo: Path, *, runner=None) -> int | None:
    """Row count as the LIVE store reports it, through bd and the server.

    Must be the ``FULL_STORE_LIST_ARGS`` read, because this number is compared for
    EQUALITY against ``backup_issue_count``'s ``select count(*) from issues`` — a
    raw table count that excludes nothing. ``bd list --all`` still hides gates,
    infra beads and template molecules, so with the shorter spelling a byte-perfect
    copy of any gate-bearing store reported ``verified: false`` and the manifest
    recorded a live count smaller than the store (measured: live 3, copy 4, for a
    store of three ordinary issues and one gate).
    """
    run = subprocess.run if runner is None else runner
    try:
        done = run(
            ["bd", *FULL_STORE_LIST_ARGS],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(repo),
            env=bd_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        rows = json.loads(done.stdout)
    except ValueError:
        return None
    return len(rows) if isinstance(rows, list) else None


def backup_issue_count(database_dir: Path, *, runner=None) -> int | None:
    """Row count read OFFLINE out of a copied database directory.

    Deliberately a different code path from ``live_issue_count``: ``dolt sql`` inside the
    copy, with no server involved. A backup verified by the same client that wrote it, over
    the same connection, would not be verified against much.
    """
    run = subprocess.run if runner is None else runner
    try:
        done = run(
            ["dolt", "sql", "-r", "csv", "-q", "select count(*) from issues"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(database_dir),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in reversed(done.stdout.strip().splitlines()):
        token = line.strip().strip('"')
        if token.isdigit():
            return int(token)
    return None


def default_destination(location: StoreLocation, *, home: Path | None = None) -> Path:
    home = Path.home() if home is None else home
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return home / ".beads" / "backups" / f"{location.dolt_database}-{stamp}"


def take_backup(
    location: StoreLocation,
    destination: Path,
    *,
    runner=None,
) -> dict:
    """COPY the database and its sidecars to ``destination``, then verify the copy.

    Never a move, and never onto an existing directory: a backup that can overwrite a
    previous backup is a way to lose two copies instead of one.
    """
    if destination.exists():
        raise FileExistsError(f"refusing to write into an existing path: {destination}")

    live = live_issue_count(location.repo, runner=runner)
    destination.mkdir(parents=True)
    copied_db = destination / location.dolt_database
    shutil.copytree(location.database_dir, copied_db, symlinks=True)

    sidecar_dir = destination / "beads-sidecars"
    sidecar_dir.mkdir()
    captured = []
    for name in SIDECARS:
        source = location.repo / ".beads" / name
        if source.is_file():
            shutil.copy2(source, sidecar_dir / name)
            captured.append(name)

    restored = backup_issue_count(copied_db, runner=runner)
    verified = live is not None and restored is not None and live == restored

    manifest = {
        "schema": "aih-bd-store-backup/1",
        "taken_at_utc": datetime.now(timezone.utc).isoformat(),
        "repo": str(location.repo),
        "dolt_database": location.dolt_database,
        "dolt_mode": location.dolt_mode,
        "source_database_dir": str(location.database_dir),
        "backup_database_dir": str(copied_db),
        "sidecars_captured": captured,
        "live_issue_count": live,
        "backup_issue_count": restored,
        "verified": verified,
        "verification": (
            "Row count read OFFLINE from the copy with `dolt sql` inside "
            f"{copied_db}, compared against `bd list --all` on the live store."
        ),
        "restore": (
            f"Stop the shared dolt sql-server, then copy {copied_db} back over "
            f"{location.database_dir} (a copy, not a move -- keep this backup), and restart "
            "the server. Verify with `bd list --all -n 0 --json | wc -l` in "
            f"{location.repo}, which must report {live}."
        ),
    }
    (destination / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bd_store_backup.py",
        description="Copy a Beads store's Dolt database and verify the copy by reading it.",
    )
    parser.add_argument(
        "--repo", type=Path, required=True, help="Repo root whose store to back up."
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Where to write. Defaults to ~/.beads/backups/<db>-<utc-stamp>.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit the manifest as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = resolve_location(args.repo.resolve())
    except FileNotFoundError as exc:
        print(f"bd-store-backup: {exc}", file=sys.stderr)
        return EXIT_RESOLVE_FAILED

    destination = args.destination or default_destination(location)
    try:
        manifest = take_backup(location, destination)
    except (FileExistsError, OSError, shutil.Error) as exc:
        print(f"bd-store-backup: copy failed: {exc}", file=sys.stderr)
        return EXIT_COPY_FAILED

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(f"  source : {manifest['source_database_dir']}")
        print(f"  backup : {manifest['backup_database_dir']}")
        print(f"  live   : {manifest['live_issue_count']} issues")
        print(f"  copy   : {manifest['backup_issue_count']} issues (read offline)")
        print(f"  status : {'VERIFIED' if manifest['verified'] else 'NOT VERIFIED'}")
        print(f"  restore: {manifest['restore']}")

    if not manifest["verified"]:
        print(
            "bd-store-backup: the copy did NOT read back with the live row count. Treat it "
            "as NOT a backup -- the source may have been mid-write. Retry, and if it "
            "persists, quiesce the sql-server first.",
            file=sys.stderr,
        )
        return EXIT_VERIFY_FAILED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
