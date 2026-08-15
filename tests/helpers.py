"""Shared onboarding helper for integration tests."""

from __future__ import annotations

from pathlib import Path

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.enums import SystemShape
from openflywheel.contracts.onboarding import LockBoundaryRequest
from openflywheel.onboarding.locate import scan_fixture_root
from openflywheel.onboarding.service import OnboardingService


def _lock_slugs_for_fixture(fixture_root: Path) -> tuple[str, str]:
    slugs = [candidate.slug for candidate in scan_fixture_root(fixture_root)]
    if "repo-alpha" in slugs and "repo-beta" in slugs:
        return "repo-alpha", "repo-beta"
    if len(slugs) < 2:
        msg = "Fixture must expose at least two locate candidates"
        raise AssertionError(msg)
    return slugs[0], slugs[1]


def onboard_and_lock(
    home: Path,
    fixture_root: Path,
    *,
    beta_exclusions: tuple[str, ...] = (),
) -> None:
    ws = WorkspaceService()
    database = ws.load_database(home)
    config = ws.read_config(home)
    onboarding = OnboardingService(database)
    assert onboarding.run_connect(config.workspace_id).error is None
    assert onboarding.run_locate(config.workspace_id, fixture_root).error is None
    alpha_slug, beta_slug = _lock_slugs_for_fixture(fixture_root)
    lock = onboarding.run_lock(
        config.workspace_id,
        (
            LockBoundaryRequest(
                candidate_slug=alpha_slug,
                purpose="Alpha service boundary",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("Owner Alpha",),
                primary_kpi="U3 architecture coverage",
            ),
            LockBoundaryRequest(
                candidate_slug=beta_slug,
                purpose="Beta service boundary",
                system_shape=SystemShape.MULTI_REPO,
                source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
                owner_display_names=("Owner Beta",),
                primary_kpi="U4 data flow coverage",
                exclusions=beta_exclusions,
            ),
        ),
    )
    assert lock.error is None
