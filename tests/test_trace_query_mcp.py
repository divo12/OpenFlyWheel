"""MCP surface exposes only typed read tools."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Protocol, cast

from mcp.server.fastmcp import FastMCP


class TraceQueryMcpModule(Protocol):
    server: FastMCP[None]


def _server() -> FastMCP[None]:
    path = (
        Path(__file__).parents[1]
        / "plugins/openflywheel-trace-query/scripts/mcp_server.py"
    )
    spec = importlib.util.spec_from_file_location("trace_query_mcp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(TraceQueryMcpModule, module).server


def test_mcp_lists_only_read_tools() -> None:
    tools = asyncio.run(_server().list_tools())

    assert [tool.name for tool in tools] == [
        "get_trace_schema",
        "query_spans",
        "get_span_context",
    ]
    assert all(tool.annotations is not None for tool in tools)
    assert all(tool.annotations.readOnlyHint is True for tool in tools if tool.annotations)
    assert all(tool.annotations.destructiveHint is False for tool in tools if tool.annotations)
