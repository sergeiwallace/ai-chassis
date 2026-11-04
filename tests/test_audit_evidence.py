import pytest

from scripts.audit_evidence import EvidenceError, route


def test_unsupported_capability_routes_no_product_finding() -> None:
    with pytest.raises(EvidenceError, match="unsupported capability"):
        route({"browser"}, {"local": {"filesystem-read"}})
