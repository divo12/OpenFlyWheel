"""Identity contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import IdentityKind
from openflywheel.contracts.ids import IdentityId, WorkspaceId


class IdentityRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: IdentityId
    workspace_id: WorkspaceId
    kind: IdentityKind
    display_name: str
    acl: AclLabel
    created_at: datetime
