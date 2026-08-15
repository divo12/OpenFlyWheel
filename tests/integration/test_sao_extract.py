"""SaO extraction integration tests."""

from tests.book_helpers import list_proposals, setup_book_pipeline

from openflywheel.contracts.enums import TruthSection
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository


def test_sao_emits_u3_u4_only_with_anchors(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    proposals = list_proposals(home, workspace_id)
    assert len(proposals) >= 2
    for proposal in proposals:
        assert proposal.section in (TruthSection.U3, TruthSection.U4)
        assert proposal.anchor_ids
        assert proposal.boundary_id

    ws_repo = SqliteProposalRepository()
    claim_repo = SqliteClaimRepository()
    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        assert claim_repo.count_claims(conn, workspace_id) == 0
        assert ws_repo.count_proposals(conn, workspace_id) == len(proposals)


def test_sao_extraction_idempotent(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    first_count = len(list_proposals(home, workspace_id))
    second = book.extract(workspace_id=workspace_id)
    assert second.error is None
    assert second.data is not None
    assert second.data.proposals_created == 0
    assert second.data.proposals_skipped_idempotent >= first_count
    assert len(list_proposals(home, workspace_id)) == first_count
