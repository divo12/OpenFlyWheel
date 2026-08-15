"""Dashboard read-model contracts."""

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.book import BoundaryCoverageReport, OrgCoverageReport
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.coverage import CoverageRequirementRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.ids import BoundaryId, IdentityId, WorkspaceId
from openflywheel.contracts.pin import PinRecord
from openflywheel.contracts.retrieval import RetrievalGap


class DashboardIdentityContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    identity_id: IdentityId


class DashboardOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    identity_id: IdentityId
    org_coverage: OrgCoverageReport
    gaps: tuple[RetrievalGap, ...]
    claim_count: int
    tension_count: int
    pin_count: int


class BoundaryDashboardView(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundary_id: BoundaryId
    coverage: BoundaryCoverageReport
    claims: tuple[ClaimRecord, ...]
    gaps: tuple[RetrievalGap, ...]


class DashboardDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    overview: DashboardOverview
    boundaries: tuple[BoundaryDashboardView, ...]
    tensions: tuple[EdgeRecord, ...]
    pins: tuple[PinRecord, ...]
    unmet_requirements: tuple[CoverageRequirementRecord, ...] = Field(default_factory=tuple)
