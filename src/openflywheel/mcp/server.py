"""MCP server exposing frozen book verbs."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from openflywheel.application.book_app import BookApplication
from openflywheel.contracts.agent_session import (
    CorrectionRecordRequest,
    EpisodeRecordRequest,
)
from openflywheel.contracts.book import BookContextRequest, ProposeManualRequest
from openflywheel.contracts.ids import BoundaryId, ClaimId, IdentityId, PinId, WorkspaceId
from openflywheel.contracts.mcp import (
    BookGetToolInput,
    BookPinToolInput,
    BookVerifyToolInput,
    CoverageGapsToolInput,
    McpResultData,
    McpToolResultEnvelope,
)
from openflywheel.contracts.operation_result import OperationResult

FROZEN_VERBS: frozenset[str] = frozenset(
    {
        "book_context",
        "book_get",
        "coverage_gaps",
        "episode_record",
        "claim_propose",
        "correction_record",
        "book_verify",
        "book_pin",
    }
)

_BOOK_CONTEXT_ADAPTER: TypeAdapter[BookContextRequest] = TypeAdapter(BookContextRequest)
_BOOK_GET_ADAPTER: TypeAdapter[BookGetToolInput] = TypeAdapter(BookGetToolInput)
_COVERAGE_ADAPTER: TypeAdapter[CoverageGapsToolInput] = TypeAdapter(CoverageGapsToolInput)
_EPISODE_ADAPTER: TypeAdapter[EpisodeRecordRequest] = TypeAdapter(EpisodeRecordRequest)
_PROPOSE_ADAPTER: TypeAdapter[ProposeManualRequest] = TypeAdapter(ProposeManualRequest)
_CORRECTION_ADAPTER: TypeAdapter[CorrectionRecordRequest] = TypeAdapter(CorrectionRecordRequest)
_VERIFY_ADAPTER: TypeAdapter[BookVerifyToolInput] = TypeAdapter(BookVerifyToolInput)
_PIN_ADAPTER: TypeAdapter[BookPinToolInput] = TypeAdapter(BookPinToolInput)

_WidenT = TypeVar("_WidenT", bound=McpResultData)


class McpBookServer:
    def __init__(self, book: BookApplication) -> None:
        self._book = book

    def list_tools(self) -> tuple[str, ...]:
        return tuple(sorted(FROZEN_VERBS))

    def call_tool(self, name: str, arguments: BaseModel) -> McpToolResultEnvelope:
        if name not in FROZEN_VERBS:
            return McpToolResultEnvelope(
                status="error",
                summary="Unknown tool",
                error_code="MCP_UNKNOWN_TOOL",
                error_message=f"Tool {name} is not exposed on surface verbs",
                next_actions=("Use a frozen book verb",),
            )
        result = self._dispatch(name, arguments)
        return _to_envelope(result)

    def _dispatch(self, name: str, arguments: BaseModel) -> OperationResult[McpResultData]:
        payload_json = arguments.model_dump_json()
        if name == "book_context":
            request = _BOOK_CONTEXT_ADAPTER.validate_json(payload_json)
            return _widen(self._book.book_context(request))
        elif name == "book_get":
            book_get = _BOOK_GET_ADAPTER.validate_json(payload_json)
            return _widen(
                self._book.book_get(
                    workspace_id=WorkspaceId(book_get.workspace_id),
                    identity_id=IdentityId(book_get.identity_id),
                    claim_id=ClaimId(book_get.claim_id),
                    pin_id=PinId(book_get.pin_id) if book_get.pin_id else None,
                )
            )
        elif name == "coverage_gaps":
            coverage = _COVERAGE_ADAPTER.validate_json(payload_json)
            return _widen(self._book.coverage_gaps(workspace_id=WorkspaceId(coverage.workspace_id)))
        elif name == "episode_record":
            episode = _EPISODE_ADAPTER.validate_json(payload_json)
            return _widen(self._book.episode_record(episode))
        elif name == "claim_propose":
            propose = _PROPOSE_ADAPTER.validate_json(payload_json)
            return _widen(self._book.claim_propose(propose))
        elif name == "correction_record":
            correction = _CORRECTION_ADAPTER.validate_json(payload_json)
            return _widen(self._book.correction_record(correction))
        elif name == "book_verify":
            verify = _VERIFY_ADAPTER.validate_json(payload_json)
            return _widen(
                self._book.book_verify(
                    workspace_id=WorkspaceId(verify.workspace_id),
                    request=verify.request,
                )
            )
        elif name == "book_pin":
            pin = _PIN_ADAPTER.validate_json(payload_json)
            return _widen(
                self._book.book_pin(
                    workspace_id=WorkspaceId(pin.workspace_id),
                    boundary_id=BoundaryId(pin.boundary_id),
                )
            )
        return OperationResult.failure(
            code="MCP_UNKNOWN_TOOL",
            message=f"Tool {name} is not exposed",
            root_cause_hint="Use list_tools for frozen verbs",
            safe_retry=False,
            stop_condition="Call a frozen verb",
        )


def _widen(result: OperationResult[_WidenT]) -> OperationResult[McpResultData]:
    return OperationResult(
        status=result.status,
        summary=result.summary,
        next_actions=result.next_actions,
        artifacts=result.artifacts,
        data=result.data,
        error=result.error,
    )


def _to_envelope(result: OperationResult[McpResultData]) -> McpToolResultEnvelope:
    if result.error is not None:
        return McpToolResultEnvelope(
            status=result.status.value,
            summary=result.summary,
            next_actions=result.next_actions,
            artifacts=result.artifacts,
            error=result.error,
            error_code=result.error.code,
            error_message=result.error.message,
        )
    return McpToolResultEnvelope(
        status=result.status.value,
        summary=result.summary,
        next_actions=result.next_actions,
        artifacts=result.artifacts,
        data=result.data,
    )
