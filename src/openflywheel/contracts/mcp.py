"""MCP boundary DTOs for frozen book verbs."""

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.agent_session import (
    CorrectionRecordRequest,
    CorrectionRecordSummary,
    EpisodeRecordRequest,
    EpisodeRecordSummary,
)
from openflywheel.contracts.book import (
    BookContextRequest,
    BookContextResult,
    ClaimDetail,
    CoverageGapsResult,
    PinSummary,
    ProposeManualRequest,
    VerifyRequest,
    VerifySummary,
)
from openflywheel.contracts.ids import ProposalId
from openflywheel.contracts.operation_result import OperationError

McpResultData = (
    BookContextResult
    | ClaimDetail
    | CoverageGapsResult
    | EpisodeRecordSummary
    | ProposalId
    | CorrectionRecordSummary
    | VerifySummary
    | PinSummary
)


class McpToolResultEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    summary: str
    next_actions: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[str, ...] = Field(default_factory=tuple)
    data: McpResultData | None = None
    error: OperationError | None = None
    error_code: str | None = None
    error_message: str | None = None


class BookContextToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: BookContextRequest


class BookGetToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    identity_id: str
    claim_id: str
    pin_id: str | None = None


class CoverageGapsToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str


class BookVerifyToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    request: VerifyRequest


class BookPinToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    boundary_id: str


class EpisodeRecordToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: EpisodeRecordRequest


class ClaimProposeToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: ProposeManualRequest


class CorrectionRecordToolInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: CorrectionRecordRequest
