"""Internal store record DTOs (not domain contracts)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import RejectReason
from openflywheel.contracts.ids import AuditRejectId, CheckpointId, SourceId, WorkspaceId


class CheckpointRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: CheckpointId
    source_id: SourceId
    cursor_value: str
    updated_at: datetime


class AuditRejectRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: AuditRejectId
    workspace_id: WorkspaceId
    source_id: SourceId
    external_id: str
    reason: RejectReason
    detail: str
    rejected_at: datetime
