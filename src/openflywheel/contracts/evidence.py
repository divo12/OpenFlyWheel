"""Evidence anchor contracts."""

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import LocatorKind
from openflywheel.contracts.ids import EpisodeId, EvidenceAnchorId


class EvidenceLocator(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: LocatorKind
    value: str


class EvidenceAnchorRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: EvidenceAnchorId
    episode_id: EpisodeId
    locator: EvidenceLocator
    label: str
