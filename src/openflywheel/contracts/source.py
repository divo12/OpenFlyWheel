"""Source connector contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import ConnectorKind, SourceKind
from openflywheel.contracts.ids import SourceId, WorkspaceId


class ConnectorCapabilityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    connector_kind: ConnectorKind
    available: bool
    collections: tuple[str, ...]
    historical_bootstrap: bool
    incremental_updates: bool
    identity_fidelity: bool
    stable_source_ids: bool
    notes: str


class SourceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: SourceId
    workspace_id: WorkspaceId
    kind: SourceKind
    slug: str
    display_name: str
    capability: ConnectorCapabilityReport
    root_path: str | None
    created_at: datetime
