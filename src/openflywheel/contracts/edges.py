"""Claim graph edge contracts."""

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import EdgeKind
from openflywheel.contracts.ids import ClaimId, EdgeId


class EdgeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: EdgeId
    kind: EdgeKind
    from_claim_id: ClaimId
    to_claim_id: ClaimId
    note: str
