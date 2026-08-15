"""Retrieval and pin integration tests."""

from tests.book_helpers import (
    boundary_id_for_slug,
    list_proposals,
    owner_identity,
    promote_proposal,
    setup_book_pipeline,
)

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.book import BookContextRequest, VerifyRequest
from openflywheel.contracts.enums import VerificationDecision, VisibilityLevel


def _promote_all_for_boundary(home, workspace_id, book, slug: str):
    owner = owner_identity(home, workspace_id, f"Owner {slug.replace('repo-', '').title()}")
    boundary_id = boundary_id_for_slug(home, workspace_id, slug)
    proposals = [
        p for p in list_proposals(home, workspace_id) if str(p.boundary_id) == str(boundary_id)
    ]
    claim_ids = []
    for proposal in proposals[:2]:
        result = book.book_verify(
            workspace_id=workspace_id,
            request=VerifyRequest(
                proposal_id=proposal.id,
                decision=VerificationDecision.PROMOTE,
                verifier_identity_id=owner,
            ),
        )
        assert result.error is None
        if result.data and result.data.claim_id:
            claim_ids.append(result.data.claim_id)
    return owner, boundary_id, claim_ids


def test_pin_immutable_after_later_ingest(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner, boundary_id, _ = _promote_all_for_boundary(home, workspace_id, book, "repo-alpha")
    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.error is None
    assert pin_result.data is not None
    pin_id = pin_result.data.pin_id
    pin_count = pin_result.data.claim_count

    assert book.extract(workspace_id=workspace_id).error is None
    owner2 = owner_identity(home, workspace_id)
    for proposal in list_proposals(home, workspace_id):
        if proposal.status.value == "pending":
            promote_proposal(
                book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner2
            )

    ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=owner,
            query="package",
            boundary_id=boundary_id,
            pin_id=pin_id,
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    assert len(ctx.data.packet.claims) == pin_count


def test_missing_identity_fails_closed(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    from openflywheel.contracts.ids import IdentityId

    result = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=IdentityId("00000000-0000-0000-0000-000000000099"),
            query="package",
        )
    )
    assert result.error is not None
    assert result.error.code == "CONTEXT_IDENTITY_UNKNOWN"


def test_two_identities_get_different_packets(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner_alpha = owner_identity(home, workspace_id, "Owner Alpha")
    owner_beta = owner_identity(home, workspace_id, "Owner Beta")
    alpha_boundary = boundary_id_for_slug(home, workspace_id, "repo-alpha")

    proposals = list_proposals(home, workspace_id)
    alpha_prop = next(p for p in proposals if p.boundary_id == alpha_boundary)

    from datetime import UTC, datetime

    from openflywheel.contracts.enums import IdentityKind

    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        private_reader = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="private-reader",
            created_at=datetime.now(tz=UTC),
        )

    private_acl = AclLabel(
        visibility=VisibilityLevel.PRIVATE,
        allowed_identities=(private_reader.id,),
    )
    promoted = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=alpha_prop.id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=owner_alpha,
            acl=private_acl,
        ),
    )
    assert promoted.error is None
    assert promoted.data is not None
    claim_id = promoted.data.claim_id
    assert claim_id is not None

    term = alpha_prop.what.split()[0]
    public_ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=owner_beta,
            query=term,
            boundary_id=alpha_boundary,
        )
    )
    private_ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=private_reader.id,
            query=term,
            boundary_id=alpha_boundary,
        )
    )
    assert public_ctx.error is None and private_ctx.error is None
    public_ids = {c.id for c in public_ctx.data.packet.claims}  # type: ignore[union-attr]
    private_ids = {c.id for c in private_ctx.data.packet.claims}  # type: ignore[union-attr]
    assert claim_id in private_ids
    assert claim_id not in public_ids


def test_fts_finds_exact_term_and_packet_has_gaps_no_why_gold(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    for proposal in list_proposals(home, workspace_id)[:4]:
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner
        )

    ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=owner,
            query="alphacore",
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    markdown = ctx.data.markdown.lower()
    assert "alphacore" in markdown or "package name" in markdown
    assert "why probes are never included" in markdown
    assert ctx.data.packet.gaps
    assert "why:" not in markdown or "why probes" in markdown
