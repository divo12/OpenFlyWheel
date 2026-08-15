"""Onboarding flow integration tests."""

from pathlib import Path

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.enums import SystemShape
from openflywheel.contracts.onboarding import LockBoundaryRequest
from openflywheel.onboarding.service import OnboardingService
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository


def test_locate_requires_connect(workspace_home: Path, fixture_root: Path) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    service = OnboardingService(database)
    result = service.run_locate(config.workspace_id, fixture_root)
    assert result.error is not None
    assert result.error.code == "LOCATE_PRECONDITION"


def test_lock_requires_locate(workspace_home: Path) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    service = OnboardingService(database)
    service.run_connect(config.workspace_id)
    result = service.run_lock(
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
        ),
    )
    assert result.error is not None
    assert result.error.code == "LOCK_PRECONDITION"


def test_locate_proposes_two_boundaries(workspace_home: Path, fixture_root: Path) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    service = OnboardingService(database)
    service.run_connect(config.workspace_id)
    result = service.run_locate(config.workspace_id, fixture_root)
    assert result.error is None
    assert result.data is not None
    assert len(result.data.candidates) >= 2


def test_extraction_refused_before_lock(workspace_home: Path, fixture_root: Path) -> None:
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    onboarding = OnboardingService(database)
    onboarding.run_connect(config.workspace_id)
    onboarding.run_locate(config.workspace_id, fixture_root)
    refused = onboarding.refuse_extraction_without_lock(config.workspace_id)
    assert refused.error is not None
    assert refused.error.code == "EXTRACT_BEFORE_LOCK"


def test_multi_boundary_lock_and_versioning(workspace_home: Path, fixture_root: Path) -> None:
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
    assert result.error is None
    assert result.data is not None
    assert len(result.data.locked_boundary_ids) == 2
    assert result.data.manifests[0].version == 1
    assert result.data.manifests[1].version == 1

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
    assert relock.error is None
    assert relock.data is not None
    assert relock.data.manifests[0].version == 2


def test_lock_batch_rolls_back_on_unknown_slug(workspace_home: Path, fixture_root: Path) -> None:
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
                candidate_slug="repo-alpha",
                purpose="Alpha",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("A",),
                primary_kpi="kpi-a",
            ),
            LockBoundaryRequest(
                candidate_slug="missing-slug",
                purpose="Missing",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("X",),
                primary_kpi="kpi-x",
            ),
        ),
    )
    assert result.error is not None
    assert result.error.code == "LOCK_UNKNOWN_BOUNDARY"

    with database.read() as conn:
        alpha = SqliteBoundaryRepository().get_by_slug(conn, config.workspace_id, "repo-alpha")
    assert alpha is not None
    assert alpha.manifest is None


def test_relock_reuses_owner_identity(workspace_home: Path, fixture_root: Path) -> None:
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
                owner_display_names=("Same Owner",),
                primary_kpi="kpi-a",
            ),
        ),
    )
    assert first.error is None
    first_owner = first.data.manifests[0].owner_identity_ids[0] if first.data else None

    second = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug="repo-alpha",
                purpose="Alpha relock",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("Same Owner",),
                primary_kpi="kpi-a",
            ),
        ),
    )
    assert second.error is None
    assert second.data is not None
    assert second.data.manifests[0].owner_identity_ids[0] == first_owner
