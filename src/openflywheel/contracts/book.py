"""Book operation contracts for verify, coverage, and pin."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.coverage import CoverageRequirementRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import TruthSection, VerificationDecision
from openflywheel.contracts.evidence import EvidenceAnchorRecord
from openflywheel.contracts.ids import (
    BoundaryId,
    ClaimId,
    EdgeId,
    EvidenceAnchorId,
    IdentityId,
    PinId,
    ProposalId,
    WorkspaceId,
)
from openflywheel.contracts.retrieval import ContextPacket, RetrievalGap


class ExtractSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposals_created: int
    proposals_skipped_idempotent: int
    proposal_ids: tuple[ProposalId, ...] = Field(default_factory=tuple)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: ProposalId
    decision: VerificationDecision
    verifier_identity_id: IdentityId
    tension_with_claim_id: ClaimId | None = None
    supersedes_claim_id: ClaimId | None = None
    derived_from_claim_id: ClaimId | None = None
    acl: AclLabel | None = None


class VerifySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: ProposalId
    decision: VerificationDecision
    claim_id: ClaimId | None = None
    edge_ids: tuple[EdgeId, ...] = Field(default_factory=tuple)


class SectionCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    section: TruthSection
    verified_slots: int
    required_slots: int

    @property
    def ratio(self) -> float:
        if self.required_slots == 0:
            return 0.0
        return self.verified_slots / self.required_slots


class BoundaryCoverageReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundary_id: BoundaryId
    sections: tuple[SectionCoverage, ...]
    overall_ratio: float


class OrgCoverageReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundaries: tuple[BoundaryCoverageReport, ...]
    overall_ratio: float


class PinSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    pin_id: PinId
    boundary_id: BoundaryId
    claim_count: int
    manifest_version: int
    created_at: datetime


class ClaimDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim: ClaimRecord
    anchors: tuple[EvidenceAnchorRecord, ...]
    edges: tuple[EdgeRecord, ...]
    history: tuple[ClaimRecord, ...] = Field(default_factory=tuple)


class BookContextRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    identity_id: IdentityId
    query: str
    boundary_id: BoundaryId | None = None
    pin_id: PinId | None = None


class BookContextResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    packet: ContextPacket
    markdown: str


class ProposeManualRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    boundary_id: BoundaryId
    what: str
    how: str
    section: TruthSection
    proposer_identity_id: IdentityId
    anchor_ids: tuple[EvidenceAnchorId, ...]


class CoverageGapsResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    gaps: tuple[RetrievalGap, ...]
    report: OrgCoverageReport
    unmet_requirements: tuple[CoverageRequirementRecord, ...] = Field(default_factory=tuple)
