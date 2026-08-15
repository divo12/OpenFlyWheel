"""Pure SaO draft models (I/O-free)."""

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import TruthSection
from openflywheel.contracts.evidence import EvidenceLocator


class SaOProposalDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    extractor: str
    what: str
    how: str
    section: TruthSection
    locator: EvidenceLocator
    anchor_label: str
    content_fingerprint: str
