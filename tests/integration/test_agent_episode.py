"""Agent episode write-back integration tests."""

from pathlib import Path

import pytest
from tests.agent_helpers import episode_request, setup_agent_pipeline

from openflywheel.application.agent_worker import BackgroundWorkerService
from openflywheel.contracts.enums import PlatformKind
from openflywheel.store.repos.claim_repo import SqliteClaimRepository


@pytest.fixture
def transcript_root() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "agent-transcripts"


def test_episode_record_creates_episode_not_claim(
    workspace_home, fixture_root, transcript_root
) -> None:
    workspace_id, book, home = setup_agent_pipeline(workspace_home, fixture_root)
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-001",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )
    result = book.episode_record(request)
    assert result.error is None
    assert result.data is not None
    assert result.data.claims_created == 0

    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        claims = SqliteClaimRepository().list_active_for_workspace(conn, workspace_id)
    assert len(claims) == 0


def test_worker_creates_proposals_not_claims(workspace_home, fixture_root, transcript_root) -> None:
    workspace_id, book, home = setup_agent_pipeline(workspace_home, fixture_root)
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-worker",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )
    recorded = book.episode_record(request)
    assert recorded.error is None

    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    worker = BackgroundWorkerService(database)
    processed = worker.process_next()
    assert processed.error is None
    assert processed.data is not None
    assert processed.data >= 1

    with database.read() as conn:
        claims = SqliteClaimRepository().list_active_for_workspace(conn, workspace_id)
        proposal_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM proposals WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
    assert len(claims) == 0
    assert proposal_count is not None
    assert int(proposal_count["cnt"]) >= 1


def test_episode_record_requires_locked_boundary(workspace_home, transcript_root) -> None:
    from openflywheel.application.book_app import BookApplication
    from openflywheel.application.workspace_service import WorkspaceService
    from openflywheel.onboarding.service import OnboardingService

    home = workspace_home
    ws = WorkspaceService()
    database = ws.load_database(home)
    config = ws.read_config(home)
    onboarding = OnboardingService(database)
    assert onboarding.run_connect(config.workspace_id).error is None

    book = BookApplication(database)
    with database.read() as conn:
        row = conn.execute(
            "SELECT id FROM identities WHERE workspace_id = ? LIMIT 1",
            (str(config.workspace_id),),
        ).fetchone()
    assert row is not None
    from openflywheel.contracts.ids import IdentityId

    admin_id = IdentityId(str(row["id"]))
    request = episode_request(
        home=home,
        workspace_id=config.workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-prelock",
        transcript_path=transcript_root / "claude-session.jsonl",
        identity_id=admin_id,
    )
    result = book.episode_record(request)
    assert result.error is not None
    assert result.error.code == "EPISODE_PRECONDITION"


def test_episode_record_rejects_secrets_in_transcript(
    workspace_home, fixture_root, tmp_path
) -> None:
    import json

    from openflywheel.application.book_app import BookApplication
    from openflywheel.application.workspace_service import WorkspaceService
    from openflywheel.contracts.enums import PlatformKind

    workspace_id, _, home = setup_agent_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    book = BookApplication(database)
    secret_transcript = tmp_path / "secret.jsonl"
    fake_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    secret_transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": f"Use key {fake_aws_key} for access",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-secret",
        transcript_path=secret_transcript,
        agent_home=str(tmp_path),
        project_root=str(tmp_path),
    )
    result = book.episode_record(request)
    assert result.error is not None
    assert result.error.code == "EPISODE_REJECTED"
