"""Retrieval packet contracts."""

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import TruthSection
from openflywheel.contracts.evidence import EvidenceAnchorRecord
from openflywheel.contracts.ids import PinId


class RetrievalGap(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot_key: str
    description: str
    section: TruthSection | None = None


class ContextPacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    pin_id: PinId | None
    claims: tuple[ClaimRecord, ...] = Field(default_factory=tuple)
    anchors: tuple[EvidenceAnchorRecord, ...] = Field(default_factory=tuple)
    tensions: tuple[EdgeRecord, ...] = Field(default_factory=tuple)
    gaps: tuple[RetrievalGap, ...] = Field(default_factory=tuple)
    markdown_body: str = ""
