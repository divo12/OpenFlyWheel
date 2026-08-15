"""Pin snapshot contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.ids import BoundaryId, ClaimId, ManifestVersion, PinId, WorkspaceId


class PinRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: PinId
    workspace_id: WorkspaceId
    boundary_id: BoundaryId
    manifest_version: ManifestVersion
    claim_ids: tuple[ClaimId, ...]
    created_at: datetime
