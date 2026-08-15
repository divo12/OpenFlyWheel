"""Shared book pipeline helpers for integration tests."""

from __future__ import annotations

from pathlib import Path

from openflywheel.application.book_app import BookApplication
from openflywheel.application.ingest_app import IngestApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.book import VerifyRequest
from openflywheel.contracts.enums import VerificationDecision
from openflywheel.contracts.ids import BoundaryId, IdentityId, ProposalId, WorkspaceId
from openflywheel.contracts.proposal import ClaimProposalRecord
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository
from tests.helpers import onboard_and_lock


def setup_book_pipeline(
    home: Path, fixture_root: Path
) -> tuple[WorkspaceId, BookApplication, Path]:
    onboard_and_lock(home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(home)
    config = ws.read_config(home)
    ingest = IngestApplication(database)
    assert (
        ingest.run_fixture_ingest(
            workspace_id=config.workspace_id,
            fixture_root=fixture_root,
        ).error
        is None
    )
    book = BookApplication(database)
    assert book.extract(workspace_id=config.workspace_id).error is None
    return config.workspace_id, book, home


def owner_identity(
    home: Path, workspace_id: WorkspaceId, owner_name: str = "Owner Alpha"
) -> IdentityId:
    ws = WorkspaceService()
    database = ws.load_database(home)
    with database.read() as conn:
        identity = SqliteWorkspaceRepository().find_identity_by_display_name(
            conn, workspace_id, owner_name
        )
    assert identity is not None
    return identity.id


def boundary_id_for_slug(home: Path, workspace_id: WorkspaceId, slug: str) -> BoundaryId:
    ws = WorkspaceService()
    database = ws.load_database(home)
    with database.read() as conn:
        boundary = SqliteBoundaryRepository().get_by_slug(conn, workspace_id, slug)
    assert boundary is not None
    return boundary.id


def list_proposals(home: Path, workspace_id: WorkspaceId) -> tuple[ClaimProposalRecord, ...]:
    ws = WorkspaceService()
    database = ws.load_database(home)
    with database.read() as conn:
        rows = conn.execute(
            "SELECT id FROM proposals WHERE workspace_id = ? ORDER BY created_at",
            (str(workspace_id),),
        ).fetchall()
        repo = SqliteProposalRepository()
        return tuple(
            proposal
            for raw in rows
            if (proposal := repo.get_proposal(conn, ProposalId(str(raw["id"])))) is not None
        )


def promote_proposal(
    book: BookApplication,
    *,
    workspace_id: WorkspaceId,
    proposal_id: ProposalId,
    verifier_id: IdentityId,
    **kwargs,
):

    request = VerifyRequest(
        proposal_id=proposal_id,
        decision=VerificationDecision.PROMOTE,
        verifier_identity_id=verifier_id,
        **kwargs,
    )
    return book.book_verify(workspace_id=workspace_id, request=request)
