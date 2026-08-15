"""Coverage requirement contracts."""

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import SystemShape, TruthSection
from openflywheel.contracts.ids import BoundaryId, CoverageRequirementId, WorkspaceId


class CoverageRequirementRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: CoverageRequirementId
    workspace_id: WorkspaceId
    boundary_id: BoundaryId
    section: TruthSection
    slot_key: str
    description: str
    required_for_shape: SystemShape
    verified: bool
