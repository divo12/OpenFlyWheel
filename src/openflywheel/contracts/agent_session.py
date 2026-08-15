"""Agent session and write-back contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import (
    AgentSessionId,
    BoundaryId,
    ClaimId,
    EpisodeId,
    EvidenceAnchorId,
    IdentityId,
    ProposalId,
    SourceId,
    WorkspaceId,
)


class SessionEnvelope(BaseModel):
    """Typed session reference for episode admission."""

    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    source_id: SourceId
    platform: PlatformKind
    session_ref: str
    transcript_path: str
    agent_home: str
    project_root: str
    identity_id: IdentityId
    boundary_id: BoundaryId | None = None
    acl: AclLabel


class EpisodeRecordSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: EpisodeId
    session_id: AgentSessionId
    job_scheduled: bool
    claims_created: int = 0


class EpisodeRecordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    envelope: SessionEnvelope


class CorrectionRecordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    claim_id: ClaimId
    correction_text: str
    authority_identity_id: IdentityId
    boundary_id: BoundaryId
    anchor_ids: tuple[EvidenceAnchorId, ...] = Field(default_factory=tuple)
    source_id: SourceId


class CorrectionRecordSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: EpisodeId
    proposal_id: ProposalId


class CanonicalMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    text: str
    message_index: int


class CanonicalAgentSession(BaseModel):
    """Human/assistant transcript projection only."""

    model_config = ConfigDict(frozen=True)

    session_id: AgentSessionId
    platform: PlatformKind
    session_ref: str
    project_root: str | None
    started_at: datetime | None
    messages: tuple[CanonicalMessage, ...] = Field(default_factory=tuple)

    def render_content_text(self) -> str:
        lines: list[str] = []
        for message in self.messages:
            lines.append(f"[{message.role}] {message.text}")
        return "\n".join(lines)
