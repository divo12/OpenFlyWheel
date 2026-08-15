"""Agent correction write-back tests."""

from tests.agent_helpers import agent_source_id
from tests.book_helpers import boundary_id_for_slug, owner_identity, setup_book_pipeline

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.agent_session import CorrectionRecordRequest
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import ClaimId


def test_correction_unknown_claim_zero_writes(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    source_id = agent_source_id(home, workspace_id, PlatformKind.CLAUDE_CODE)

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        before_episodes = conn.execute("SELECT COUNT(*) AS cnt FROM episodes").fetchone()
        before_proposals = conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()
    assert before_episodes is not None and before_proposals is not None

    result = book.correction_record(
        CorrectionRecordRequest(
            workspace_id=workspace_id,
            claim_id=ClaimId("claim-does-not-exist"),
            correction_text="This should not persist",
            authority_identity_id=owner,
            boundary_id=boundary_id,
            source_id=source_id,
        )
    )
    assert result.error is not None
    assert result.error.code == "CORRECTION_CLAIM_UNKNOWN"

    with database.read() as conn:
        after_episodes = conn.execute("SELECT COUNT(*) AS cnt FROM episodes").fetchone()
        after_proposals = conn.execute("SELECT COUNT(*) AS cnt FROM proposals").fetchone()
    assert after_episodes is not None and after_proposals is not None
    assert int(after_episodes["cnt"]) == int(before_episodes["cnt"])
    assert int(after_proposals["cnt"]) == int(before_proposals["cnt"])
