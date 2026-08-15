"""Claim proposal contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import ProposalStatus, TruthSection
from openflywheel.contracts.ids import (
    BoundaryId,
    EvidenceAnchorId,
    IdentityId,
    ProposalId,
    WorkspaceId,
)


class ClaimProposalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: ProposalId
    workspace_id: WorkspaceId
    boundary_id: BoundaryId
    what: str
    how: str
    section: TruthSection
    proposer_identity_id: IdentityId
    anchor_ids: tuple[EvidenceAnchorId, ...]
    status: ProposalStatus
    idempotency_key: str
    created_at: datetime
