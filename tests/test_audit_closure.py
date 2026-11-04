import pytest

from scripts.audit_closure import ClosureError, validate_packet


def test_closure_refuses_unknown_cells() -> None:
    packet = {
        key: []
        for key in (
            "finding_keys",
            "invariant_ids",
            "counterexample_ids",
            "producers",
            "transforms",
            "consumers",
            "authority_lifecycle_surfaces",
            "artifact_family_members",
            "states_polarities",
            "support_frontier",
            "invalidated_evidence_ids",
            "unchanged_cells",
            "out_of_scope_cells",
        )
    }
    packet.update(packet_id="p", challenge_evidence_id="e", unchanged_cells=["unknown"])
    with pytest.raises(ClosureError, match="unknown"):
        validate_packet(packet)
