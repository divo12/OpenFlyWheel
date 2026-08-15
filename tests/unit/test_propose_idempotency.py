"""Manual proposal idempotency key tests."""

from openflywheel.book.propose import manual_proposal_idempotency_key
from openflywheel.contracts.book import ProposeManualRequest
from openflywheel.contracts.enums import TruthSection
from openflywheel.contracts.ids import BoundaryId, EvidenceAnchorId, IdentityId, WorkspaceId


def test_manual_proposal_idempotency_key_includes_section_how_sorted_anchors() -> None:
    ws = WorkspaceId("ws-1")
    boundary = BoundaryId("b-1")
    owner = IdentityId("owner-1")
    anchor_b = EvidenceAnchorId("00000000-0000-0000-0000-000000000002")
    anchor_a = EvidenceAnchorId("00000000-0000-0000-0000-000000000001")
    request = ProposeManualRequest(
        workspace_id=ws,
        boundary_id=boundary,
        what="Same what",
        how="How A",
        section=TruthSection.U5,
        proposer_identity_id=owner,
        anchor_ids=(anchor_b, anchor_a),
    )
    key = manual_proposal_idempotency_key(request)
    assert "|U5|" in key
    assert "|How A|" in key
    assert str(anchor_a) in key
    assert str(anchor_b) in key
    assert key.index(str(anchor_a)) < key.index(str(anchor_b))

    other = manual_proposal_idempotency_key(request.model_copy(update={"how": "How B"}))
    assert key != other
