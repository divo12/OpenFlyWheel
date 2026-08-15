"""Expert notes connector tests."""

from pathlib import Path

from tests.book_helpers import boundary_id_for_slug, owner_identity, setup_book_pipeline

from openflywheel.connectors.notes.ingest import ExpertNotesService
from openflywheel.contracts.enums import SourceKind
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository


def test_note_ingest_creates_episode_and_proposal_not_claim(
    workspace_home, fixture_root, tmp_path: Path
) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")

    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, workspace_id, SourceKind.EXPERT_NOTES.value
        )
    assert source is not None

    note_path = tmp_path / "expert-note.md"
    note_path.write_text(
        "---\ntitle: Memory gate policy\nauthority: owner\n---\n\nPrivate notes about gating.\n",
        encoding="utf-8",
    )

    service = ExpertNotesService(database)
    first = service.ingest_note(
        workspace_id=workspace_id,
        source_id=source.id,
        boundary_id=boundary_id,
        authority_identity_id=owner,
        note_path=note_path,
    )
    second = service.ingest_note(
        workspace_id=workspace_id,
        source_id=source.id,
        boundary_id=boundary_id,
        authority_identity_id=owner,
        note_path=note_path,
    )
    assert first.error is None and second.error is None
    assert first.data is not None and second.data is not None
    assert first.data.episode_id == second.data.episode_id
    assert second.data.idempotent is True

    with database.read() as conn:
        claims = SqliteClaimRepository().list_active_for_workspace(conn, workspace_id)
    assert len(claims) == 0


def test_note_ingest_rejects_unknown_identity(workspace_home, fixture_root, tmp_path) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")

    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, workspace_id, SourceKind.EXPERT_NOTES.value
        )
    assert source is not None

    note_path = tmp_path / "expert-note.md"
    note_path.write_text(
        "---\ntitle: Auth gate\nauthority: owner\n---\n\nShould not ingest.\n",
        encoding="utf-8",
    )

    from openflywheel.contracts.ids import IdentityId

    service = ExpertNotesService(database)
    with database.read() as conn:
        before_episodes = conn.execute("SELECT COUNT(*) AS cnt FROM episodes").fetchone()
        before_proposals = conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()
    assert before_episodes is not None and before_proposals is not None

    result = service.ingest_note(
        workspace_id=workspace_id,
        source_id=source.id,
        boundary_id=boundary_id,
        authority_identity_id=IdentityId("forged-note-identity"),
        note_path=note_path,
    )
    assert result.error is not None
    assert result.error.code == "NOTE_IDENTITY_UNKNOWN"

    with database.read() as conn:
        after_episodes = conn.execute("SELECT COUNT(*) AS cnt FROM episodes").fetchone()
        after_proposals = conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()
    assert after_episodes is not None and after_proposals is not None
    assert int(after_episodes["cnt"]) == int(before_episodes["cnt"])
    assert int(after_proposals["cnt"]) == int(before_proposals["cnt"])
