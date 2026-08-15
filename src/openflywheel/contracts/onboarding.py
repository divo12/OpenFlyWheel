"""Onboarding stage contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.boundary import BoundaryCandidate, BoundaryManifest, SourceAuthorityRule
from openflywheel.contracts.enums import OnboardingStage, SystemShape
from openflywheel.contracts.ids import BoundaryId, IdentityId, OnboardingRunId, WorkspaceId
from openflywheel.contracts.source import ConnectorCapabilityReport


class ConnectStageData(BaseModel):
    model_config = ConfigDict(frozen=True)

    reports: tuple[ConnectorCapabilityReport, ...] = Field(default_factory=tuple)


class LocateStageData(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidates: tuple[BoundaryCandidate, ...] = Field(default_factory=tuple)
    fixture_root: str


class LockStageData(BaseModel):
    model_config = ConfigDict(frozen=True)

    locked_boundary_ids: tuple[BoundaryId, ...] = Field(default_factory=tuple)
    manifests: tuple[BoundaryManifest, ...] = Field(default_factory=tuple)


class OnboardingState(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: OnboardingRunId
    workspace_id: WorkspaceId
    stage: OnboardingStage
    connect: ConnectStageData | None = None
    locate: LocateStageData | None = None
    lock: LockStageData | None = None
    updated_at: datetime


class LockBoundaryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_slug: str
    purpose: str
    system_shape: SystemShape
    source_authorities: tuple[SourceAuthorityRule, ...]
    owner_display_names: tuple[str, ...] = Field(default_factory=tuple)
    owner_identity_ids: tuple[IdentityId, ...] = Field(default_factory=tuple)
    primary_kpi: str
    exclusions: tuple[str, ...] = Field(default_factory=tuple)
