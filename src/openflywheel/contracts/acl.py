"""Access control labels."""

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.enums import VisibilityLevel
from openflywheel.contracts.ids import IdentityId


class AclLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    visibility: VisibilityLevel
    allowed_identities: tuple[IdentityId, ...] = Field(default_factory=tuple)
