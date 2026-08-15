"""Single-boundary component path scoping during ingest."""

from pathlib import Path

from openflywheel.application.ingest_app import IngestApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.enums import SourceKind, SystemShape
from openflywheel.contracts.onboarding import LockBoundaryRequest
from openflywheel.onboarding.service import OnboardingService
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository


def test_ingest_only_locked_boundary_component_paths(
    workspace_home: Path, fixture_root: Path
) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)
    onboarding.run_connect(config.workspace_id)
    onboarding.run_locate(config.workspace_id, fixture_root)
    lock = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug="repo-alpha",
                purpose="Alpha only",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("Alpha Owner",),
                primary_kpi="alpha coverage",
            ),
        ),
    )
    assert lock.error is None

    app = IngestApplication(database)
    result = app.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=fixture_root,
    )
    assert result.error is None
    assert result.data is not None
    assert result.data.skipped_out_of_scope_count > 0

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        episodes = SqliteEpisodeRepository().list_episodes_for_source(conn, source.id)
        stored_paths = {ep.source_ref.external_id for ep in episodes}

    assert any(path.startswith("repo-alpha/") for path in stored_paths)
    assert not any(path.startswith("repo-beta/") for path in stored_paths)
