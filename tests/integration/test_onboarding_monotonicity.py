"""Onboarding stage monotonicity and re-run safety tests."""

from pathlib import Path

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.enums import OnboardingStage, OperationStatus, SystemShape
from openflywheel.contracts.onboarding import LockBoundaryRequest
from openflywheel.onboarding.service import OnboardingService
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.onboarding_repo import SqliteOnboardingRepository


def _lock_beta_only(workspace_home: Path, fixture_root: Path) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)
    onboarding.run_connect(config.workspace_id)
    onboarding.run_locate(config.workspace_id, fixture_root)
    result = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug="repo-beta",
                purpose="Beta service boundary",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("Owner Beta",),
                primary_kpi="U4 data flow coverage",
                exclusions=("secrets/",),
            ),
        ),
    )
    assert result.error is None


def test_locate_after_lock_preserves_manifest_and_exclusion(
    workspace_home: Path, fixture_root: Path
) -> None:
    _lock_beta_only(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)

    with database.read() as conn:
        before = SqliteBoundaryRepository().get_by_slug(conn, config.workspace_id, "repo-beta")
        state_before = SqliteOnboardingRepository().get_active(conn, config.workspace_id)
    assert before is not None and before.manifest is not None
    assert before.manifest.exclusions == ("secrets/",)
    assert state_before is not None
    assert state_before.stage == OnboardingStage.LOCK

    relocate = onboarding.run_locate(config.workspace_id, fixture_root)
    assert relocate.status == OperationStatus.WARNING
    assert relocate.error is None

    with database.read() as conn:
        after = SqliteBoundaryRepository().get_by_slug(conn, config.workspace_id, "repo-beta")
        state_after = SqliteOnboardingRepository().get_active(conn, config.workspace_id)
    assert after is not None and after.manifest is not None
    assert after.manifest.exclusions == ("secrets/",)
    assert after.manifest.version == before.manifest.version
    assert state_after is not None
    assert state_after.stage == OnboardingStage.LOCK


def test_connect_after_lock_does_not_rewind_stage(workspace_home: Path, fixture_root: Path) -> None:
    _lock_beta_only(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)

    reconnect = onboarding.run_connect(config.workspace_id)
    assert reconnect.status == OperationStatus.WARNING
    assert reconnect.error is None

    with database.read() as conn:
        state = SqliteOnboardingRepository().get_active(conn, config.workspace_id)
    assert state is not None
    assert state.stage == OnboardingStage.LOCK


def test_locate_after_lock_does_not_rewind_stage(workspace_home: Path, fixture_root: Path) -> None:
    _lock_beta_only(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)

    relocate = onboarding.run_locate(config.workspace_id, fixture_root)
    assert relocate.status == OperationStatus.WARNING

    with database.read() as conn:
        state = SqliteOnboardingRepository().get_active(conn, config.workspace_id)
    assert state is not None
    assert state.stage == OnboardingStage.LOCK


def test_partial_relock_preserves_other_locked_boundaries(
    workspace_home: Path, fixture_root: Path
) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)
    onboarding.run_connect(config.workspace_id)
    onboarding.run_locate(config.workspace_id, fixture_root)
    first = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug="repo-alpha",
                purpose="Alpha",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("A",),
                primary_kpi="kpi-a",
            ),
            LockBoundaryRequest(
                candidate_slug="repo-beta",
                purpose="Beta",
                system_shape=SystemShape.LIBRARY,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=2),),
                owner_display_names=("B",),
                primary_kpi="kpi-b",
                exclusions=("secrets/",),
            ),
        ),
    )
    assert first.error is None and first.data is not None
    beta_id = first.data.locked_boundary_ids[1]
    beta_manifest = first.data.manifests[1]

    relock = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug="repo-alpha",
                purpose="Alpha v2",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("A",),
                primary_kpi="kpi-a",
            ),
        ),
    )
    assert relock.error is None and relock.data is not None
    assert len(relock.data.locked_boundary_ids) == 2
    assert len(relock.data.manifests) == 2
    assert relock.data.locked_boundary_ids[1] == beta_id
    assert relock.data.manifests[1].exclusions == beta_manifest.exclusions
    assert relock.data.manifests[1].version == beta_manifest.version
    assert relock.data.manifests[0].version == 2
