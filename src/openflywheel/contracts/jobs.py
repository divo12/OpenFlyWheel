"""Background job queue contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import BackgroundJobKind, BackgroundJobStatus
from openflywheel.contracts.ids import BackgroundJobId, WorkspaceId

MAX_JOB_ATTEMPTS = 3


class TranscriptExtractPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: str
    session_id: str
    boundary_id: str | None = None
    disable_recursion: bool = True


class JobLease(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: BackgroundJobId
    owner: str
    expires_at: datetime


class BackgroundJobRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: BackgroundJobId
    workspace_id: WorkspaceId
    kind: BackgroundJobKind
    payload_json: str
    status: BackgroundJobStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
