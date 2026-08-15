"""Onboarding stage ordering helpers."""

from __future__ import annotations

from openflywheel.contracts.enums import OnboardingStage

_STAGE_ORDER: tuple[OnboardingStage, ...] = (
    OnboardingStage.WORKSPACE,
    OnboardingStage.CONNECT,
    OnboardingStage.LOCATE,
    OnboardingStage.LOCK,
    OnboardingStage.BOOTSTRAP,
    OnboardingStage.COMPLETE,
)


def stage_rank(stage: OnboardingStage) -> int:
    return _STAGE_ORDER.index(stage)


def stage_at_least(stage: OnboardingStage, minimum: OnboardingStage) -> bool:
    return stage_rank(stage) >= stage_rank(minimum)
