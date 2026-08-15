"""CLI"""

from __future__ import annotations

import json

import anyio
import mcp_types as types
from pydantic import BaseModel, TypeAdapter

from openflywheel.application.book_app import BookApplication
from openflywheel.contracts.agent_session import CorrectionRecordRequest, EpisodeRecordRequest
from openflywheel.contracts.book import BookContextRequest, ProposeManualRequest
from openflywheel.contracts.mcp import (
    BookGetToolInput,
    BookPinToolInput,
    BookVerifyToolInput,
    CoverageGapsToolInput,
)
from openflywheel.contracts.pydantic_json import model_dump_object_dict
from openflywheel.mcp.runtime_adapter import McpStdioServer, create_mcp_server, open_stdio_streams
from openflywheel.mcp.server import FROZEN_VERBS, McpBookServer

_BOOK_CONTEXT_ADAPTER: TypeAdapter[BookContextRequest] = TypeAdapter(BookContextRequest)
_BOOK_GET_ADAPTER: TypeAdapter[BookGetToolInput] = TypeAdapter(BookGetToolInput)
_COVERAGE_GAPS_ADAPTER: TypeAdapter[CoverageGapsToolInput] = TypeAdapter(CoverageGapsToolInput)
_EPISODE_RECORD_ADAPTER: TypeAdapter[EpisodeRecordRequest] = TypeAdapter(EpisodeRecordRequest)
_CLAIM_PROPOSE_ADAPTER: TypeAdapter[ProposeManualRequest] = TypeAdapter(ProposeManualRequest)
_CORRECTION_RECORD_ADAPTER: TypeAdapter[CorrectionRecordRequest] = TypeAdapter(
    CorrectionRecordRequest
)
_BOOK_VERIFY_ADAPTER: TypeAdapter[BookVerifyToolInput] = TypeAdapter(BookVerifyToolInput)
_BOOK_PIN_ADAPTER: TypeAdapter[BookPinToolInput] = TypeAdapter(BookPinToolInput)


def _tool_schema(name: str) -> types.Tool:
    return types.Tool(
        name=name,
        description=f"OpenFlyWheel frozen verb: {name}",
        input_schema={"type": "object", "additionalProperties": True},
    )


def _parse_tool_input(name: str, raw_args: dict[str, object]) -> BaseModel:
    if name == "book_context":
        return _BOOK_CONTEXT_ADAPTER.validate_python(raw_args)
    if name == "book_get":
        return _BOOK_GET_ADAPTER.validate_python(raw_args)
    if name == "coverage_gaps":
        return _COVERAGE_GAPS_ADAPTER.validate_python(raw_args)
    if name == "episode_record":
        if "envelope" in raw_args:
            return _EPISODE_RECORD_ADAPTER.validate_python(raw_args)
        envelope_payload: dict[str, object] = {"envelope": raw_args}
        return _EPISODE_RECORD_ADAPTER.validate_python(envelope_payload)
    if name == "claim_propose":
        return _CLAIM_PROPOSE_ADAPTER.validate_python(raw_args)
    if name == "correction_record":
        return _CORRECTION_RECORD_ADAPTER.validate_python(raw_args)
    if name == "book_verify":
        if "request" in raw_args:
            return _BOOK_VERIFY_ADAPTER.validate_python(raw_args)
        workspace_id: object = raw_args.get("workspace_id")
        request_body: dict[str, object] = {
            key: value for key, value in raw_args.items() if key != "workspace_id"
        }
        verify_payload: dict[str, object] = {
            "workspace_id": workspace_id,
            "request": request_body,
        }
        return _BOOK_VERIFY_ADAPTER.validate_python(verify_payload)
    if name == "book_pin":
        return _BOOK_PIN_ADAPTER.validate_python(raw_args)
    msg = f"Unknown tool {name}"
    raise ValueError(msg)


def build_mcp_server(book: BookApplication) -> tuple[McpStdioServer, McpBookServer]:
    server_impl = McpBookServer(book)

    async def list_tools(
        _ctx: object,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_tool_schema(name) for name in sorted(FROZEN_VERBS)])

    async def call_tool(
        _ctx: object,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        raw = params.arguments if isinstance(params.arguments, dict) else {}
        if name not in FROZEN_VERBS:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"Unknown tool: {name}",
                    )
                ],
                is_error=True,
            )
        try:
            parsed = _parse_tool_input(name, raw)
        except ValueError:
            envelope = server_impl.call_tool(name, _UnknownInput())
        else:
            envelope = server_impl.call_tool(name, parsed)
        text = json.dumps(model_dump_object_dict(envelope), indent=2)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    mcp_server = create_mcp_server(
        "openflywheel-verbs",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    return mcp_server, server_impl


class _UnknownInput(BaseModel):
    pass


async def run_stdio_mcp(book: BookApplication) -> None:
    mcp_server, _ = build_mcp_server(book)
    async with open_stdio_streams() as (read_stream, write_stream):
        init_options = mcp_server.create_initialization_options()
        await mcp_server.run(read_stream, write_stream, init_options)


def run_stdio_mcp_sync(book: BookApplication) -> None:
    anyio.run(run_stdio_mcp, book)
