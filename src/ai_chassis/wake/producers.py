"""Shared producer integration, selected independently from session delivery.

Disabled profiles retain their original operations. The common outbox retains
notification intents when available; its failures never veto a primary write.
"""

import contextlib
import functools
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid

from .doorbell import Doorbell, OutboxOwner, Producer
from .dispatcher import Dispatcher
from .events import digest, sync_directory
from .registry import Registry
from .notifications import attempt, notifying
from .sessions import mail_root
from .targets import AssigneeTarget, MachineTarget, RoleTarget, SessionTarget

_scripts_lock = threading.RLock()
_publisher_module = None
_transport_restorer = None
SOURCE_COMMAND_TIMEOUT_S = 5


def registry():
    return Registry()


@functools.cache
def _former_hosts():
    """Renamed host labels, from the one registry the rest of the harness reads."""
    try:
        tables = tomllib.loads(
            (
                Path(__file__).resolve().parents[3]
                / "config/session-name-migrations.toml"
            ).read_text()
        )
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return tables.get("former-names", {})


def local_host(default=None):
    """This host's current label.

    A shell started before a rename keeps the old `AI_HOST`, and a bell addressed to a
    label no route table knows is retired as `no_route`. The label is canonicalised here
    once, so every producer addresses the same machine.
    """
    label = (
        os.environ["AI_HOST"] if default is None else os.environ.get("AI_HOST", default)
    )
    return _former_hosts().get(label.casefold(), label)


def settings(config=None):
    return (config or registry()).read().get("producers", {})


def enabled(config=None):
    return settings(config).get("enabled") is True


def root(config=None):
    value = settings(config).get("state_dir")
    return (
        Path(value)
        if value
        else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "ai-harness/wake-outbox"
    )


def default_signing_key_path():
    return mail_root() / "doorbell-signing.key"


def provision_signing_key():
    """Publish a complete private key only if absent; never rewrite existing state."""
    key = default_signing_key_path()
    if key.exists() or key.is_symlink():
        return
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".doorbell-key-", dir=key.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(os.urandom(32))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staged, key)
        except FileExistsError:
            return  # A competing installer won; its key must remain untouched.
        sync_directory(key.parent)
    finally:
        if staged.exists():
            # Only remove the exact temporary file created above.
            staged.unlink()


def secret(config=None):
    # Provisioned key material, never included in an intent or diagnostic.
    key = settings(config).get("signing_key")
    value = (Path(key) if key else default_signing_key_path()).read_bytes()
    if not value:
        raise ValueError("missing-doorbell-key")
    return value


def prepare(source, revision, targets, *, key, cursor, context, origin):  # noqa: PLR0913 - content-free operation descriptor
    if not enabled():
        return None
    bells = [
        (
            machine,
            Doorbell(source, key, revision, target, origin, int(time.time())).sign(
                secret()
            ),
        )
        for machine, target in targets
    ]
    client = Producer(root())
    operation = digest([source, revision, key, uuid.uuid4().hex])
    identity = client.prepare(
        operation, bells, cursor=cursor, candidate=revision, context=context
    )
    return client, identity


def complete(intent):
    if intent is not None:
        client, identity = intent
        client.complete(identity)


def git_targets(files, *, source):
    from ai_chassis import git_inbox

    targets = []
    keys = set()
    for path, text in files.items():
        if not git_inbox.is_message_path(path):
            continue
        envelope = git_inbox.parse_envelope(text, path=path, source=source)
        keys.add(envelope.id)
        for address in envelope.to:
            machines = (
                git_inbox.known_machines()
                if address.machine == "*"
                else [address.machine]
            )
            for machine in machines:
                target = (
                    MachineTarget(machine)
                    if address.slug == "*"
                    else SessionTarget(address.slug)
                )
                targets.append((machine, target))
    return targets, keys


def prepare_git(sender, files, candidate, parent):
    if not enabled():
        return None
    from ai_chassis import git_inbox, git_inbox_receipts
    from ai_chassis.git_inbox_watch import source_of

    bells = []
    for path, text in files.items():
        if git_inbox.is_message_path(path):
            targets, keys = git_targets({path: text}, source=source_of(sender.read_ref))
            key = next(iter(keys))
        else:
            record = git_inbox_receipts.parse(text)
            message_ref = str(record.extra.get("message_ref") or "")
            shown = sender._git(["show", message_ref])
            if shown.rc:
                raise ValueError("receipt-source-unverified")
            envelope = git_inbox.parse_envelope(
                shown.out,
                path=message_ref.partition(":")[2],
                source=source_of(message_ref.partition(":")[0]),
            )
            if envelope.id != record.message_id or envelope.from_ is None:
                raise ValueError("receipt-source-mismatch")
            recipient = git_inbox.parse_address(record.recipient)
            targets = [
                (address.machine, SessionTarget(address.slug))
                for address in (recipient, envelope.from_)
            ]
            key = record.message_id
        bells.extend(
            (
                machine,
                Doorbell(
                    "git-inbox",
                    key,
                    candidate,
                    target,
                    sender.machine,
                    int(time.time()),
                ).sign(secret()),
            )
            for machine, target in targets
        )
    client = Producer(root())
    identity = client.prepare(
        "git-inbox:" + candidate + ":" + uuid.uuid4().hex,
        list(dict.fromkeys(bells)),
        cursor=parent,
        candidate=candidate,
        context={
            "kind": "git",
            "repo": str(sender.repo),
            "remote": sender.remote,
            "ref": sender.ref,
        },
    )
    return client, identity


def reconcile(row):  # noqa: PLR0911 - three authoritative source protocols, unknown is explicit
    """An uncertain operation is inspected, never submitted again."""
    from ai_chassis.fleet_liveness import run_git

    context = row["context"]
    if context["kind"] == "pr":
        result = source_command(
            [
                "gh",
                "pr",
                "view",
                context["selector"],
                "--repo",
                context["repository"],
                "--json",
                "headRefOid,mergedAt,mergeCommit",
            ],
        )
        if result.returncode:
            return None
        fact = json.loads(result.stdout)
        if fact.get("headRefOid") != row["candidate"] or not fact.get("mergedAt"):
            return None
        proof = {
            **row,
            "candidate": fact["mergeCommit"]["oid"],
            "context": {**context, "kind": "git"},
        }
        return reconcile(proof)
    if context["kind"] == "dolt":
        module = _publisher()
        transport = _restore_transport(module, context)
        # Fetch moves only remote-tracking state, never main or the working set.
        # Do not acquire the publisher's lock: a producer awaiting ownership can
        # already hold it. Recovery never repeats its pull, commit or push.
        transport.sql("CALL DOLT_FETCH('origin')")
        contained, _ = module.ref_contains(
            transport, transport.remote_branch, row["candidate"]
        )
        return contained
    if context["kind"] == "stamp":
        return _reconcile_stamp(row)
    if context["kind"] != "git":
        return None
    repo = Path(context["repo"])
    private = "refs/wake-reconcile/" + digest(row["operation"])
    listed = run_git(["ls-remote", context["remote"], context["ref"]], repo, None)
    if listed.rc:
        return None
    if not listed.out.strip():
        return False
    fetched = run_git(
        ["fetch", "--quiet", context["remote"], f"+{context['ref']}:{private}"],
        repo,
        None,
    )
    if fetched.rc:
        return None
    proof = run_git(
        ["merge-base", "--is-ancestor", row["candidate"], private], repo, None
    )
    return True if proof.rc == 0 else False if proof.rc == 1 else None


def source_command(command, **options):
    """Bound source verification, including descendants holding captured pipes.

    These calls inspect/fetch facts, never repeat a primary publish operation.
    Reuse the registered process-group runner instead of inventing a new spawn.
    """
    from ai_chassis.dependency_bootstrap import EXPIRED, KILLED, run_bounded

    result = run_bounded(
        command,
        seconds=SOURCE_COMMAND_TIMEOUT_S,
        grace=0.5,
        capture=True,
        cwd=options.get("cwd"),
        env=options.get("env"),
    )
    if result.returncode in {EXPIRED, KILLED}:
        raise TimeoutError("source-verification-timeout")
    return result


def _reconcile_stamp(row):
    from ai_chassis.fleet_liveness import run_git

    context = row["context"]
    path = Path(context["path"])
    if not path.exists():
        return False
    installed = path.read_text().strip()
    if installed == row["candidate"]:
        return True
    proof = run_git(
        ["merge-base", "--is-ancestor", row["candidate"], installed],
        Path(context["repo"]),
        None,
    )
    return True if proof.rc == 0 else False if proof.rc == 1 else None


def _publisher():
    with _scripts_lock:
        if _publisher_module is None:
            _script("wake_source_helpers").register()
        if _publisher_module is None:
            raise ValueError("publisher-not-registered")
        return _publisher_module


def register_publisher(module, restore):
    """Register private source capabilities without naming their implementation."""
    if module is None or not callable(restore):
        raise ValueError("invalid-publisher-registration")
    global _publisher_module, _transport_restorer
    with _scripts_lock:
        _publisher_module, _transport_restorer = module, restore


def _script(script):
    with _scripts_lock:
        return _load_script(script)


def _load_script(script):
    if script not in {"wake_source_helpers", "harness_sync_census"}:
        raise ValueError("unregistered-source-helper")
    checkout = str(Path(__file__).resolve().parents[3])
    if checkout not in sys.path:
        sys.path.insert(0, checkout)
    # Static, call-time imports keep both helpers in the daemon fingerprint's
    # enumerable closure without loading either on a disabled producer path.
    if script == "wake_source_helpers":
        from scripts import wake_source_helpers

        return wake_source_helpers
    from scripts import harness_sync_census

    return harness_sync_census


def _restore_transport(module, context):
    if _transport_restorer is None:
        _publisher()
    if _transport_restorer is None:
        raise ValueError("publisher-restore-not-registered")
    return _transport_restorer(module, context)


def _local_assignees():
    from ai_chassis.agent_mail_notifier import (
        _carried_slugs,
        _claude_sessions,
        _observe_inventory,
    )
    from ai_chassis.fleet_liveness import state_identities
    from .sessions import bindings, eligible

    names = {row["slug"] for row in bindings(mail_root()) if eligible(mail_root(), row)}
    for session in (
        attempt(lambda: _observe_inventory(_claude_sessions().live_sessions)) or ()
    ):
        names.update(_carried_slugs(session))
    for identity in state_identities(mail_root() / "identities.json", local_host()):
        names.add(identity.partition("@")[0])
    return names


def _assignee_destinations():
    """Read existing registries once per change; never fetch or start a polling loop."""
    destinations = {}
    origin = local_host()
    for name in attempt(_local_assignees) or ():
        destinations.setdefault(name, set()).add(origin)
    peers = attempt(_fleet_assignees)
    for machine, identities in (peers or {}).items():
        for identity in identities:
            destinations.setdefault(identity.partition("@")[0], set()).add(machine)
    return destinations


def _fleet_assignees():
    from ai_chassis import buzz_channel, fleet_liveness, message_transports

    return fleet_liveness.live_buzz_sessions(
        fleet_liveness.daemon_liveness_state(),
        presence_file=buzz_channel.presence_file_path(buzz_channel.buzz_key_dir()),
        stale_after_s=fleet_liveness.ladder_stale_after_s(
            message_transports.load_config()
        ),
    )


def _assignee_destination(assignee, destinations):
    from ai_chassis.session_address import parse_address

    if "@" in assignee:
        machine = parse_address(assignee).machine
    else:
        machines = destinations.get(assignee, set())
        if len(machines) == 1:
            machine = next(iter(machines))
        elif machines:
            machine = None  # An ambiguous registry must not be overridden by a guess.
        else:
            machine = settings().get("assignee_machines", {}).get(assignee)
    # This is an accounting destination, never an inferred host or broadcast.
    return machine if machine and machine != "*" else "unresolved-assignee:" + assignee


def prepare_beads(transport, candidate, before, module=None):
    if not enabled():
        return None
    module = module if module is not None else _publisher()
    if before:
        rows = transport.sql(
            "select coalesce(to_id, from_id) as id, to_assignee as assignee, "
            "from_assignee as previous_assignee from dolt_diff("
            f"{module._quote(before)}, {module._quote(candidate)}, 'issues') "
            "where not (to_assignee <=> from_assignee) or "
            "not (from_status <=> to_status)"
        )
    else:
        rows = transport.sql(
            "select id, assignee from issues as of "
            + module._quote(candidate)
            + " where assignee <> ''"
        )
    destinations = None
    origin = local_host()
    bells = []
    for row in rows:
        for assignee in {row.get("assignee"), row.get("previous_assignee")} - {
            None,
            "",
        }:
            if destinations is None and "@" not in assignee:
                destinations = _assignee_destinations()
            machine = _assignee_destination(assignee, destinations or {})
            bells.append(
                (
                    machine,
                    Doorbell(
                        "beads",
                        str(getattr(transport, "database", "embedded"))
                        + ":"
                        + row["id"],
                        candidate,
                        AssigneeTarget(assignee),
                        origin,
                        int(time.time()),
                    ).sign(secret()),
                )
            )
    if not bells:
        return None
    context = {"kind": "dolt"}
    if hasattr(transport, "db_dir"):
        context["db_dir"] = str(transport.db_dir)
    else:
        context.update(
            repo=str(transport.repo),
            database=transport.database,
            endpoint=transport.endpoint.as_dict(),
        )
    client = Producer(root())
    identity = client.prepare(
        "beads:" + candidate + ":" + uuid.uuid4().hex,
        bells,
        cursor=before,
        candidate=candidate,
        context=context,
    )
    return client, identity


def prepare_main(  # noqa: PLR0913 - explicit source-of-truth coordinates
    repo, candidate, before, *, ref="refs/heads/main", remote="origin", local=False
):
    if not enabled():
        return None
    from ai_chassis.git_inbox import known_machines

    origin = local_host()
    return prepare(
        "harness-sync",
        candidate,
        [
            (machine, RoleTarget("installer_owner"))
            for machine in ([origin] if local else known_machines())
        ],
        key="main",
        cursor=before,
        origin=origin,
        context={"kind": "git", "repo": str(repo), "remote": remote, "ref": ref},
    )


def prepare_install(path, candidate, repo=None):
    if not enabled():
        return None
    origin = local_host("local")
    return prepare(
        "harness-sync",
        candidate,
        [(origin, RoleTarget("installer_owner"))],
        key="install",
        cursor=Path(path).read_text().strip() if Path(path).exists() else "",
        context={
            "kind": "stamp",
            "path": str(path),
            "repo": str(repo or Path(__file__).resolve().parents[3]),
        },
        origin=origin,
    )


def write_install_stamp(path, revision):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".installed-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(revision + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        sync_directory(path.parent)
    finally:
        if staged.exists():
            staged.unlink()


@contextlib.contextmanager
def installing(path, candidate, repo=None):
    with notifying("prepare_install", path, candidate, repo):
        yield


def _run_command(command, completion_bound=None, **options):
    return subprocess.run(command, timeout=completion_bound, check=False, **options)


def main(argv=None):
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        split = argv.index("--")
    except ValueError:
        return 2
    options, command = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("install", "merge", "main"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--repository")
    parser.add_argument("--selector")
    args = parser.parse_args(options)
    if not command:
        return 2
    if not attempt(enabled):
        return _run_command(command).returncode
    with notifying("_prepare_command", args):
        result = _run_command(command)
        if args.kind == "install" and result.returncode in {0, 2}:
            attempt(_record_install, args.candidate)
        return result.returncode


def _record_install(candidate):
    stamp = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "ai-harness/harness-sync/installed-commit"
    )
    write_install_stamp(stamp, candidate)


def _prepare_command(args):
    if not enabled():
        return None
    if args.kind == "install":
        stamp = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "ai-harness/harness-sync/installed-commit"
        )
        return prepare_install(stamp, args.candidate, args.repo)
    if args.kind == "main":
        resolved = subprocess.run(
            ["git", "-C", str(args.repo), "rev-parse", args.candidate],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        before = subprocess.run(
            ["git", "-C", str(args.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return prepare_main(args.repo, resolved, before, local=True)
    return prepare_merge(args.repo, args.candidate, args.repository, args.selector)


def prepare_merge(repo, candidate, repository, selector):
    if not enabled():
        return None
    if not candidate or not repository or not selector:
        raise ValueError("merge-source-unverified")
    from ai_chassis.fleet_liveness import run_git
    from ai_chassis.git_inbox import known_machines

    before = run_git(["ls-remote", "origin", "refs/heads/main"], Path(repo), None)
    if before.rc or not before.out.strip():
        raise ValueError("merge-cursor-unverified")
    return prepare(
        "harness-sync",
        candidate,
        [(machine, RoleTarget("installer_owner")) for machine in known_machines()],
        key="main",
        cursor=before.out.split()[0],
        context={
            "kind": "pr",
            "repo": str(repo),
            "remote": "origin",
            "ref": "refs/heads/main",
            "repository": repository,
            "selector": selector,
        },
        origin=local_host(),
    )


def owner(config=None, receiver_root=None, *, background_reconcile=False):
    config = config or registry()
    if not enabled(config):
        return None
    routes = config.load("routes")
    from .routes.local import LocalIntakeRoute
    from .sessions import mail_root

    routes.setdefault(
        local_host("local"),
        LocalIntakeRoute(receiver_root or mail_root() / "dispatch/doorbells"),
    )
    return OutboxOwner(
        root(config), reconcile, routes, background_reconcile=background_reconcile
    )


@contextlib.contextmanager
def legacy_owner(config=None):
    """Own producer receipts independently of either notifier delivery selector.

    This dispatcher waits only on outbox/route readiness and retained intents'
    deadlines. Source reconciliation runs off its receipt thread as well.
    """
    source = owner(config, background_reconcile=True)
    if source is None:
        yield
        return
    from .deadlines import PersistentDeadlineSet

    state = source.root / "dispatch"
    host = Dispatcher(
        state,
        lambda: frozenset(),
        contextlib.nullcontext,
        sources={"producer-outbox": source},
        deadlines=PersistentDeadlineSet(state / "deadlines"),
    )
    host.start()
    failed = []

    def serve():
        try:
            host.run()
        except Exception as exc:
            failed.append(type(exc).__name__)

    thread = threading.Thread(target=serve, daemon=True, name="wake-outbox-owner")
    thread.start()
    try:
        yield
    finally:
        host.stop()
        thread.join()
    if failed:
        raise RuntimeError("outbox-owner-failed:" + failed[0])


if __name__ == "__main__":
    raise SystemExit(main())
