"""Episode contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.ids import EpisodeId, SourceId, WorkspaceId


class SourceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: SourceId
    external_id: str
    uri: str


class EpisodeRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: EpisodeId
    workspace_id: WorkspaceId
    source_ref: SourceReference
    content_text: str
    acl: AclLabel
    event_time: datetime
    ingest_time: datetime
    checksum: str
    content_type: str
