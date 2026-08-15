"""Agent surface test helpers."""

from __future__ import annotations

from pathlib import Path

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.agent_session import EpisodeRecordRequest, SessionEnvelope
from openflywheel.contracts.enums import PlatformKind, VisibilityLevel
from openflywheel.contracts.ids import IdentityId
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from tests.book_helpers import owner_identity, setup_book_pipeline


def agent_source_id(home: Path, workspace_id, platform: PlatformKind):
    from openflywheel.application.workspace_service import WorkspaceService

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(conn, workspace_id, platform.value)
    assert source is not None
    return source.id


def episode_request(
    *,
    home: Path,
    workspace_id,
    platform: PlatformKind,
    session_ref: str,
    transcript_path: Path,
    identity_id: IdentityId | None = None,
    agent_home: str | None = None,
    project_root: str | None = None,
    fixture_root: Path | None = None,
) -> EpisodeRecordRequest:
    owner = identity_id or owner_identity(home, workspace_id)
    source_id = agent_source_id(home, workspace_id, platform)
    resolved_transcript = transcript_path.resolve()
    fixtures_root = resolved_transcript.parent.parent
    resolved_agent_home = agent_home or str(fixtures_root)
    resolved_project_root = project_root or str(fixture_root or fixtures_root)
    return EpisodeRecordRequest(
        envelope=SessionEnvelope(
            workspace_id=workspace_id,
            source_id=source_id,
            platform=platform,
            session_ref=session_ref,
            transcript_path=str(resolved_transcript),
            agent_home=resolved_agent_home,
            project_root=resolved_project_root,
            identity_id=owner,
            acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        )
    )


def setup_agent_pipeline(home: Path, fixture_root: Path):
    return setup_book_pipeline(home, fixture_root)
