"""Verification, edges, and coverage integration tests."""

from datetime import UTC, datetime

from tests.book_helpers import (
    list_proposals,
    owner_identity,
    promote_proposal,
    setup_book_pipeline,
)

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.book import VerifyRequest
from openflywheel.contracts.enums import (
    ClaimState,
    TruthSection,
    VerificationDecision,
)
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository


def test_promote_creates_claim_reject_does_not(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    proposals = list_proposals(home, workspace_id)
    assert len(proposals) >= 2

    promoted = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner
    )
    assert promoted.error is None
    assert promoted.data is not None
    assert promoted.data.claim_id is not None

    rejected = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[1].id,
            decision=VerificationDecision.REJECT,
            verifier_identity_id=owner,
        ),
    )
    assert rejected.error is None

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        assert SqliteClaimRepository().count_claims(conn, workspace_id) == 1


def test_unauthorized_verifier_fails_closed(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    proposals = list_proposals(home, workspace_id)
    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        from openflywheel.contracts.enums import IdentityKind
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        stranger = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="stranger",
            created_at=datetime.now(tz=UTC),
        )

    result = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[0].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=stranger.id,
        ),
    )
    assert result.error is not None
    assert result.error.code == "VERIFY_UNAUTHORIZED"


def test_supersede_closes_interval_tension_preserves_both(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    proposals = [p for p in list_proposals(home, workspace_id) if p.section == TruthSection.U3]
    assert len(proposals) >= 2

    first = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner
    )
    assert first.data is not None
    old_claim_id = first.data.claim_id
    assert old_claim_id is not None

    second = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[1].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=owner,
            supersedes_claim_id=old_claim_id,
        ),
    )
    assert second.error is None

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        claims = SqliteClaimRepository()
        old = claims.get_claim(conn, old_claim_id)
        assert old is not None
        assert old.state == ClaimState.SUPERSEDED
        assert old.valid_to is not None

    third_props = [
        p
        for p in list_proposals(home, workspace_id)
        if p.id not in {proposals[0].id, proposals[1].id}
    ]
    if third_props:
        tension = book.book_verify(
            workspace_id=workspace_id,
            request=VerifyRequest(
                proposal_id=third_props[0].id,
                decision=VerificationDecision.LEAVE_IN_TENSION,
                verifier_identity_id=owner,
                tension_with_claim_id=second.data.claim_id if second.data else None,
            ),
        )
        assert tension.error is None
        with database.read() as conn:
            edges = SqliteEdgeRepository().list_edges_for_claim(
                conn,
                tension.data.claim_id,  # type: ignore[union-attr]
            )
            assert any(e.kind.value == "in_tension_with" for e in edges)


def test_coverage_ignores_proposal_count_and_reports_u5_u7_gaps(
    workspace_home, fixture_root
) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    before = book.coverage_gaps(workspace_id=workspace_id)
    assert before.error is None
    assert before.data is not None
    initial_ratio = before.data.report.overall_ratio

    for proposal in list_proposals(home, workspace_id)[:3]:
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner
        )

    after = book.coverage_gaps(workspace_id=workspace_id)
    assert after.data is not None
    gap_sections = {g.section for g in after.data.gaps if g.section is not None}
    assert TruthSection.U5 in gap_sections
    assert TruthSection.U7 in gap_sections
    assert after.data.report.overall_ratio >= initial_ratio
    assert after.data.report.overall_ratio < 1.0

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        proposal_count = SqliteProposalRepository().count_proposals(conn, workspace_id)
        assert proposal_count >= 3
