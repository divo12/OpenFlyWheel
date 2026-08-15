"""Verified claim contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import ClaimState, TruthSection
from openflywheel.contracts.ids import BoundaryId, ClaimId, IdentityId, ProposalId, WorkspaceId


class ClaimRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: ClaimId
    workspace_id: WorkspaceId
    boundary_id: BoundaryId
    what: str
    how: str
    section: TruthSection
    state: ClaimState
    authority_identity_id: IdentityId
    acl: AclLabel
    valid_from: datetime
    valid_to: datetime | None
    source_proposal_id: ProposalId | None = None
