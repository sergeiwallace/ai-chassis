from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
MODULE = ROOT / "skills" / "_shared" / "ask-fidelity" / "ask_fidelity.py"
spec = importlib.util.spec_from_file_location("ask_fidelity", MODULE)
assert spec and spec.loader
af = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = af
spec.loader.exec_module(af)


def detector(status: int):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], status)

    return run


def detector_saying(status: int, stderr: bytes):
    """A detector whose exit status carries diagnostic text, as the real one does."""

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], status, stderr=stderr)

    return run


# The exact bytes /usr/bin/gitleaks emitted on corp-sagemaker (2025-06-23 build)
# for the pinned `gitleaks stdin ...` invocation.
STALE_BINARY_STDERR = b'Error: unknown command "stdin" for "gitleaks"\n'


@pytest.mark.parametrize("kind", ["initial", "follow-up", "correction"])
@pytest.mark.parametrize(
    ("status", "confirmed", "state"),
    [
        (2, True, "detector-match"),
        (9, True, "detector-failure"),
    ],
)
def test_capture_blocked_outcomes_expose_no_persistable_bytes(
    tmp_path, kind, status, confirmed, state
):
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    capture = f"private-{kind}".encode()
    result = af.authorize_capture(
        capture, tmp_path, confirmed_safe=confirmed, runner=detector(status)
    )
    assert result.state == state
    assert not result.launch_allowed
    assert result.bytes_to_persist is None
    assert list(tmp_path.iterdir()) == [tmp_path / ".gitleaks.toml"]


def test_a_detector_lacking_the_pinned_subcommand_is_named_as_a_capability_gap(
    tmp_path,
):
    """A too-old gitleaks must block AND say why.

    Reported as a bare configuration failure it is indistinguishable from a broken
    ``.gitleaks.toml``, which sent a session hunting the config while the real cause
    was the binary.  It stays blocking either way.
    """
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    capture = b"private ask nobody else may read"
    result = af.authorize_capture(
        capture,
        tmp_path,
        confirmed_safe=False,
        runner=detector_saying(1, STALE_BINARY_STDERR),
    )
    assert result.state == "detector-capability-absent"
    assert not result.launch_allowed
    assert result.bytes_to_persist is None
    assert "stdin" in result.reason
    # Classifying from the detector's diagnostics must not turn the reason into a
    # channel for the captured bytes.
    assert "private ask" not in result.reason
    assert list(tmp_path.iterdir()) == [tmp_path / ".gitleaks.toml"]


def test_a_generic_detector_failure_is_not_relabelled_as_a_capability_gap(tmp_path):
    """Negative control: a config error must not be blamed on a stale binary."""
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    result = af.authorize_capture(
        b"private ask",
        tmp_path,
        confirmed_safe=False,
        runner=detector_saying(1, b"Error: failed to load config\n"),
    )
    assert result.state == "detector-failure"
    assert not result.launch_allowed
    assert result.bytes_to_persist is None


def test_a_missing_detector_config_uses_the_builtin_rules(tmp_path):
    """A missing local policy still runs the real detector, with its defaults."""

    calls = []

    def detector(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    result = af.authorize_capture(
        b"exact capture bytes",
        tmp_path,
        confirmed_safe=False,
        runner=detector,
    )

    assert result.state == "authorized"
    assert result.launch_allowed
    assert result.bytes_to_persist == b"exact capture bytes"
    assert calls[0][0][0] == ["gitleaks", "stdin", "--exit-code", "2", "--redact=100"]
    assert list(tmp_path.iterdir()) == []


def test_a_repository_local_detector_config_remains_the_exact_scan_target(tmp_path):
    config = tmp_path / ".gitleaks.toml"
    config.write_text("[extend]\nuseDefault = true\n")
    calls = []

    def detector(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    result = af.authorize_capture(
        b"exact capture bytes", tmp_path, confirmed_safe=False, runner=detector
    )

    assert result.launch_allowed
    assert calls[0][0][0] == [
        "gitleaks",
        "stdin",
        "--config",
        str(config),
        "--exit-code",
        "2",
        "--redact=100",
    ]


def test_detector_clear_auto_authorizes_without_confirmation_and_preserves_exact_bytes(
    tmp_path,
):
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    capture = "exact bytes — typo adn all".encode()
    result = af.authorize_capture(
        capture, tmp_path, confirmed_safe=False, runner=detector(0)
    )
    assert result.state == "authorized"
    assert result.launch_allowed
    assert result.bytes_to_persist == capture


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def committed_record(tmp_path: Path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Fixture")
    path = tmp_path / "docs" / "asks" / "TASK-1.md"
    path.parent.mkdir(parents=True)
    content = b"entry_id: A001\nkind: ORIGINAL-ASK\nbody_sha256: x\n"
    path.write_bytes(content)
    git(tmp_path, "add", path.relative_to(tmp_path).as_posix())
    git(tmp_path, "commit", "-qm", "fixture")
    commit = git(tmp_path, "rev-parse", "HEAD")
    blob = git(tmp_path, "rev-parse", f"{commit}:docs/asks/TASK-1.md")
    carrier, digest = af.make_root(
        task_id="TASK-1",
        repository="fixture",
        path="docs/asks/TASK-1.md",
        capture_commit=commit,
        blob_oid=blob,
        content=content,
        entry_ids=["A001"],
    )
    return carrier, digest


def test_root_is_canonical_and_binds_committed_path_blob_digest_and_entries(tmp_path):
    carrier, digest = committed_record(tmp_path)
    root = af.validate_root(carrier, tmp_path, digest)
    assert root["entry_ids"] == ["A001"]
    assert carrier == af.canonical_json(root)


@pytest.mark.parametrize(
    "field", ["path", "capture_commit", "blob_oid", "content_sha256", "entry_ids"]
)
def test_root_mutations_fail_naming_field(tmp_path, field):
    carrier, _digest = committed_record(tmp_path)
    root = json.loads(carrier)
    root[field] = {
        "path": "missing",
        "capture_commit": "0" * 40,
        "blob_oid": "0" * 40,
        "content_sha256": "0" * 64,
        "entry_ids": ["A999"],
    }[field]
    mutated = af.canonical_json(root)
    with pytest.raises(af.FidelityError, match=field):
        af.validate_root(mutated, tmp_path, af.sha256_bytes(mutated))


def test_noncanonical_or_yaml_root_is_rejected(tmp_path):
    carrier, _ = committed_record(tmp_path)
    parsed = json.loads(carrier)
    with pytest.raises(af.FidelityError, match="canonical"):
        af.parse_canonical_root(json.dumps(parsed, indent=2).encode())
    with pytest.raises(af.FidelityError, match="canonical JSON"):
        af.parse_canonical_root(b"schema_version: 1\n")


def aligned_receipt():
    evidence = [
        {"kind": "citation", "value": "ask:A001", "source": "docs/asks/TASK-1.md"}
    ]
    return {
        "schema_version": 1,
        "run_id": "run",
        "stage": "doc",
        "transition": "research->doc",
        "founding_ask_ref_sha256": "1" * 64,
        "subject": {
            "artifact": {"state": "present", "ref": "report.md", "sha256": "2" * 64},
            "next_brief": {"state": "present", "sha256": "3" * 64},
        },
        "reviewer": {
            "family": "codex",
            "model": "model",
            "effort": "high",
            "persona": "audit",
        },
        "requirements": [
            {
                "requirement_id": "R1",
                "source_entry": "A001",
                "disposition": "SATISFIED",
                "applicability": "DIRECT",
                "rationale": "covered",
                "evidence": evidence,
                "authority_ref": None,
            }
        ],
        "scope_changes": [],
        "verdict": "ALIGNED",
        "confidence": "high",
        "blocking": False,
        "evidence": evidence,
    }


def test_promotion_binds_exact_root_artifact_brief_and_transition():
    af.validate_promotion(
        aligned_receipt(),
        root_digest="1" * 64,
        transition="research->doc",
        artifact_sha256="2" * 64,
        next_brief_sha256="3" * 64,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "nested",
        "nested-evidence",
        "scope-evidence",
        "union",
        "sentinel",
        "evidence",
        "stale",
        "blocking",
    ],
)
def test_receipt_mutations_fail(mutation):
    receipt = aligned_receipt()
    if mutation == "unknown":
        receipt["extra"] = True
    elif mutation == "nested":
        receipt["reviewer"]["extra"] = True
    elif mutation == "nested-evidence":
        receipt["requirements"][0]["evidence"][0]["extra"] = True
    elif mutation == "scope-evidence":
        receipt["scope_changes"] = [
            {
                "kind": "NARROWING",
                "description": "authorized",
                "evidence": [],
                "authority_ref": "decision:D-1",
            }
        ]
    elif mutation == "union":
        receipt["subject"]["artifact"] = {"state": "absent", "sha256": "2" * 64}
    elif mutation == "sentinel":
        receipt["subject"]["next_brief"]["sha256"] = "N/A"
    elif mutation == "evidence":
        receipt["evidence"] = []
    elif mutation == "blocking":
        receipt.update(verdict="MISMATCH", blocking=True)
    args = {
        "root_digest": "1" * 64,
        "transition": "research->doc",
        "artifact_sha256": "2" * 64,
        "next_brief_sha256": "3" * 64,
    }
    if mutation == "stale":
        args["next_brief_sha256"] = "4" * 64
    with pytest.raises(af.FidelityError):
        af.validate_promotion(receipt, **args)


def test_tier_one_brief_preserves_six_field_compatibility():
    rendered = af.render_brief_carrier(
        tier=1,
        exact_ask=b"fix exactly this",
        stage_framing="1. Objective\n2. Context\n3. Output\n4. Tools\n5. Boundaries\n6. Failure",
    )
    assert "FOUNDING ASK" in rendered and "fix exactly this" in rendered
    for field in (
        "1. Objective",
        "2. Context",
        "3. Output",
        "4. Tools",
        "5. Boundaries",
        "6. Failure",
    ):
        assert field in rendered


def test_tier_three_brief_has_distinct_physical_blocks_and_parent_identity(tmp_path):
    carrier, digest = committed_record(tmp_path)
    record = tmp_path / "docs" / "asks" / "TASK-1.md"
    rendered = af.render_brief_carrier(
        tier=3,
        root_carrier=carrier,
        root_digest=digest,
        resolved_path=record,
        excerpts=[b"ORIGINAL-ASK"],
        stage_framing="write the design",
        alignment_obligation="compare raw ask, artifact, and next brief",
        parent_root_carrier=carrier,
        repo_root=tmp_path,
    )
    assert (
        rendered.index("FOUNDING ASK")
        < rendered.index("STAGE FRAMING")
        < rendered.index("ALIGNMENT OBLIGATION")
    )
    assert carrier.decode() in rendered
    assert str(record) in rendered


def test_unreadable_pointer_requires_verified_full_fallback(tmp_path):
    carrier, digest = committed_record(tmp_path)
    with pytest.raises(af.FidelityError, match="unreadable"):
        af.render_brief_carrier(
            tier=2,
            root_carrier=carrier,
            root_digest=digest,
            resolved_path=tmp_path / "gone",
            excerpts=[],
            stage_framing="stage",
            alignment_obligation="check",
            repo_root=tmp_path,
        )
    record = (tmp_path / "docs" / "asks" / "TASK-1.md").read_bytes()
    assert "full_verified_record" in af.render_brief_carrier(
        tier=2,
        root_carrier=carrier,
        root_digest=digest,
        resolved_path=tmp_path / "gone",
        excerpts=[],
        stage_framing="stage",
        alignment_obligation="check",
        full_record_fallback=record,
        repo_root=tmp_path,
    )


def test_changed_worktree_record_and_nonverbatim_excerpt_block(tmp_path):
    carrier, digest = committed_record(tmp_path)
    record = tmp_path / "docs" / "asks" / "TASK-1.md"
    record.write_text("uncommitted replacement\n")
    with pytest.raises(af.FidelityError, match="unreadable"):
        af.render_brief_carrier(
            tier=2,
            root_carrier=carrier,
            root_digest=digest,
            resolved_path=record,
            stage_framing="stage",
            alignment_obligation="check",
            repo_root=tmp_path,
        )
    committed = subprocess.check_output(
        ["git", "-C", str(tmp_path), "show", "HEAD:docs/asks/TASK-1.md"]
    )
    with pytest.raises(af.FidelityError, match="excerpt"):
        af.render_brief_carrier(
            tier=2,
            root_carrier=carrier,
            root_digest=digest,
            resolved_path=record,
            excerpts=[b"agent-authored paraphrase"],
            full_record_fallback=committed,
            stage_framing="stage",
            alignment_obligation="check",
            repo_root=tmp_path,
        )


def test_child_root_mutation_fails_without_human_generation(tmp_path):
    carrier, _digest = committed_record(tmp_path)
    changed = carrier.replace(b'"task_id":"TASK-1"', b'"task_id":"TASK-2"')
    with pytest.raises(af.FidelityError, match="parent"):
        af.render_brief_carrier(
            tier=3,
            root_carrier=changed,
            root_digest=af.sha256_bytes(changed),
            resolved_path=tmp_path / "docs" / "asks" / "TASK-1.md",
            excerpts=[],
            stage_framing="stage",
            alignment_obligation="check",
            parent_root_carrier=carrier,
            repo_root=tmp_path,
        )


def test_tier_three_renderer_rejects_uncommitted_root(tmp_path):
    carrier, _digest = committed_record(tmp_path)
    root = json.loads(carrier)
    root["capture_commit"] = "0" * 40
    changed = af.canonical_json(root)
    with pytest.raises(af.FidelityError, match="capture_commit"):
        af.render_brief_carrier(
            tier=3,
            root_carrier=changed,
            root_digest=af.sha256_bytes(changed),
            resolved_path=tmp_path / "docs" / "asks" / "TASK-1.md",
            stage_framing="stage",
            alignment_obligation="check",
            repo_root=tmp_path,
        )


def test_given_detector_exits_outside_verdict_codes_when_gate_runs_then_reason_names_code_and_stderr(
    tmp_path,
):
    """A blocking detector failure must be actionable, not the generic phrase alone.

    Reported as bare ``detector/configuration failure`` it names neither the exit code
    nor the tool's complaint, which is what sent a session hunting a missing config
    while gitleaks was installed and exiting 0 on the same argv.
    """
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    result = af.authorize_capture(
        b"a plain founding ask",
        tmp_path,
        confirmed_safe=True,
        runner=detector_saying(1, b"\x1b[31mError\x1b[0m: failed to load config\n"),
    )
    assert result.state == "detector-failure"
    assert not result.launch_allowed
    assert result.bytes_to_persist is None
    assert "exit code 1" in result.reason
    assert "failed to load config" in result.reason
    assert "\x1b[" not in result.reason


def test_given_detector_stderr_echoes_the_capture_when_reason_is_built_then_payload_is_dropped(
    tmp_path,
):
    """Naming the cause must never become a route for payload bytes to reach a log."""
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    result = af.authorize_capture(
        b"super-secret-token-value\n",
        tmp_path,
        confirmed_safe=True,
        runner=detector_saying(
            1, b"context: super-secret-token-value\nError: bad config\n"
        ),
    )
    assert result.state == "detector-failure"
    assert "super-secret-token-value" not in result.reason
    assert "bad config" in result.reason


def test_given_the_detector_cannot_be_invoked_when_gate_runs_then_reason_says_no_exit_code(
    tmp_path,
):
    """An OSError means nothing ran, so there is no exit code to quote."""
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")

    def explode(*args, **kwargs):
        raise OSError("No such file or directory: 'gitleaks'")

    result = af.authorize_capture(
        b"a plain founding ask", tmp_path, confirmed_safe=True, runner=explode
    )
    assert result.state == "detector-failure"
    assert not result.launch_allowed
    assert result.bytes_to_persist is None
    assert "no exit code" in result.reason
    assert "gitleaks" in result.reason


def test_given_the_detector_matches_when_gate_runs_then_it_blocks_and_names_the_match(
    tmp_path,
):
    """The gate must not weaken into passing on a match, and must say it matched."""
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    result = af.authorize_capture(
        b"a capture with a secret", tmp_path, confirmed_safe=True, runner=detector(2)
    )
    assert result.state == "detector-match"
    assert not result.launch_allowed
    assert result.bytes_to_persist is None
    assert "detector match" in result.reason
    assert "exit code 2" in result.reason


def test_given_a_clear_scan_when_scan_capture_returns_then_it_carries_no_diagnostic(
    tmp_path,
):
    """A verdict inside {0, 2} has nothing to diagnose, so the detail stays empty."""
    verdict, diagnostic = af.scan_capture(b"a plain ask", tmp_path, runner=detector(0))
    assert verdict == "clear"
    assert diagnostic == ""


# ISSUE-REF: an ALIGNED receipt may not narrow an explicit founding-ask requirement on an AI
# decision's authority, and may not leave a carried excerpt out. The excerpt below is the real
# clause the corp harness D-4 dropped (example-client-host-workspace docs/asks/ISSUE-REF.md
# A001); before this fix an ALIGNED receipt marking it NARROWED on authority "D-4" promoted.
D4_EXCERPT = (
    "it can use this cc session and do prompt injection and auto-submission of actual prompts "
    "in the cc session using the shared prompt injection library"
)
PROMOTION = {
    "root_digest": "1" * 64,
    "transition": "research->doc",
    "artifact_sha256": "2" * 64,
    "next_brief_sha256": "3" * 64,
}


def narrowed_receipt(authority_ref):
    receipt = aligned_receipt()
    receipt["requirements"][0].update(
        disposition="NARROWED",
        rationale=f"D-4 drives the client headlessly instead: {D4_EXCERPT}",
        authority_ref=authority_ref,
    )
    return receipt


@pytest.mark.parametrize("authority", ["D-4", "OQ-6", "docs/plans/harness.md#d-4"])
def test_given_aligned_narrowing_on_ai_authority_when_promoted_then_it_is_refused(
    authority,
):
    with pytest.raises(af.FidelityError, match="far:"):
        af.validate_promotion(narrowed_receipt(authority), **PROMOTION)
    assert (
        af.promotion_mismatch_subject(narrowed_receipt(authority), **PROMOTION)
        == "semantic"
    )


def test_given_aligned_narrowing_on_the_humans_far_entry_when_promoted_then_it_passes():
    af.validate_promotion(
        narrowed_receipt("far:A001"), **PROMOTION, human_entry_ids=["A001"]
    )


def test_given_a_far_entry_the_record_lacks_when_promoted_then_it_is_refused():
    with pytest.raises(af.FidelityError, match="A009"):
        af.validate_promotion(
            narrowed_receipt("far:A009"), **PROMOTION, human_entry_ids=["A001"]
        )


@pytest.mark.parametrize("kind", ["NARROWING", "REJECTION"])
def test_given_an_aligned_scope_narrowing_on_ai_authority_then_it_is_refused(kind):
    receipt = aligned_receipt()
    receipt["scope_changes"] = [
        {
            "kind": kind,
            "description": "injection is not used",
            "evidence": receipt["evidence"],
            "authority_ref": "decision:D-4",
        }
    ]
    with pytest.raises(af.FidelityError, match="far:"):
        af.validate_promotion(receipt, **PROMOTION)


def test_given_a_receipt_omitting_a_carried_excerpt_when_promoted_then_it_is_refused():
    receipt = aligned_receipt()
    with pytest.raises(af.FidelityError, match="prompt injection"):
        af.validate_promotion(receipt, **PROMOTION, excerpts=[D4_EXCERPT])
    assert (
        af.promotion_mismatch_subject(receipt, **PROMOTION, excerpts=[D4_EXCERPT])
        == "unverified"
    )


def test_given_a_receipt_quoting_every_excerpt_when_promoted_then_it_passes():
    receipt = aligned_receipt()
    receipt["requirements"][0]["rationale"] = (
        "Kept: " + "  ".join(D4_EXCERPT.upper().split(" ")) + ", in the AI UAT tier."
    )
    af.validate_promotion(receipt, **PROMOTION, excerpts=[D4_EXCERPT])


def test_given_a_carrier_when_read_then_its_excerpts_are_returned_verbatim():
    prompt = (
        "## FOUNDING ASK — fidelity tier 3\n\nrelevant_verbatim_excerpts_incomplete:\n"
        f"```text\n{D4_EXCERPT}\n```\n```text\nsecond clause\n```\n\n"
        "## STAGE FRAMING — non-authoritative\n\nwork\n"
    )
    assert af.carrier_excerpts(prompt) == [D4_EXCERPT, "second clause"]
