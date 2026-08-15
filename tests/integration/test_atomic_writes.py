"""Atomic write-back transaction tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.agent_helpers import agent_source_id, episode_request, setup_agent_pipeline
from tests.book_helpers import boundary_id_for_slug, owner_identity, setup_book_pipeline

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.connectors.notes.ingest import ExpertNotesService
from openflywheel.contracts.agent_session import CorrectionRecordRequest
from openflywheel.contracts.enums import PlatformKind, SourceKind
from openflywheel.ingest.agent_episode import AgentEpisodeService
from openflywheel.store.checkpoint_hook import AbortCheckpointCommitHook
from openflywheel.store.exceptions import IngestTransactionError
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.uow import IngestUnitOfWork


@pytest.fixture
def transcript_root() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "agent-transcripts"


def _count_rows(database, sql: str) -> int:
    with database.read() as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_episode_record_aborts_without_partial_writes(
    workspace_home, fixture_root, transcript_root
) -> None:
    workspace_id, _, home = setup_agent_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    before_episodes = _count_rows(database, "SELECT COUNT(*) FROM episodes")
    before_anchors = _count_rows(database, "SELECT COUNT(*) FROM evidence_anchors")
    before_sessions = _count_rows(database, "SELECT COUNT(*) FROM agent_sessions")
    before_jobs = _count_rows(database, "SELECT COUNT(*) FROM background_jobs")

    failing_uow = IngestUnitOfWork(database, checkpoint_hook=AbortCheckpointCommitHook())
    service = AgentEpisodeService(database, uow=failing_uow)
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-atomic-fail",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )

    with pytest.raises(IngestTransactionError):
        service.record_episode(request)

    assert _count_rows(database, "SELECT COUNT(*) FROM episodes") == before_episodes
    assert _count_rows(database, "SELECT COUNT(*) FROM evidence_anchors") == before_anchors
    assert _count_rows(database, "SELECT COUNT(*) FROM agent_sessions") == before_sessions
    assert _count_rows(database, "SELECT COUNT(*) FROM background_jobs") == before_jobs


def test_note_ingest_aborts_without_partial_writes(
    workspace_home, fixture_root, tmp_path: Path
) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, workspace_id, SourceKind.EXPERT_NOTES.value
        )
    assert source is not None

    note_path = tmp_path / "atomic-note.md"
    note_path.write_text(
        "---\ntitle: Atomic note\nauthority: owner\n---\n\nNo partial writes.\n",
        encoding="utf-8",
    )

    before_episodes = _count_rows(database, "SELECT COUNT(*) FROM episodes")
    before_proposals = _count_rows(database, "SELECT COUNT(*) FROM proposals")

    failing_uow = IngestUnitOfWork(database, checkpoint_hook=AbortCheckpointCommitHook())
    service = ExpertNotesService(database, uow=failing_uow)

    with pytest.raises(IngestTransactionError):
        service.ingest_note(
            workspace_id=workspace_id,
            source_id=source.id,
            boundary_id=boundary_id,
            authority_identity_id=owner,
            note_path=note_path,
        )

    assert _count_rows(database, "SELECT COUNT(*) FROM episodes") == before_episodes
    assert _count_rows(database, "SELECT COUNT(*) FROM proposals") == before_proposals


def test_correction_record_aborts_without_partial_writes(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    source_id = agent_source_id(home, workspace_id, PlatformKind.CLAUDE_CODE)

    from tests.book_helpers import list_proposals, promote_proposal

    proposal = list_proposals(home, workspace_id)[0]
    promoted = promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=proposal.id,
        verifier_id=owner,
    )
    assert promoted.error is None
    assert promoted.data is not None
    claim_id = promoted.data.claim_id
    assert claim_id is not None

    database = WorkspaceService().load_database(home)
    failing_uow = IngestUnitOfWork(database, checkpoint_hook=AbortCheckpointCommitHook())
    service = AgentEpisodeService(database, uow=failing_uow)

    before_episodes = _count_rows(database, "SELECT COUNT(*) FROM episodes")
    before_proposals = _count_rows(database, "SELECT COUNT(*) FROM proposals")

    request = CorrectionRecordRequest(
        workspace_id=workspace_id,
        claim_id=claim_id,
        correction_text="Atomic correction should roll back",
        authority_identity_id=owner,
        boundary_id=boundary_id,
        source_id=source_id,
    )

    with pytest.raises(IngestTransactionError):
        service.record_correction(request)

    assert _count_rows(database, "SELECT COUNT(*) FROM episodes") == before_episodes
    assert _count_rows(database, "SELECT COUNT(*) FROM proposals") == before_proposals
