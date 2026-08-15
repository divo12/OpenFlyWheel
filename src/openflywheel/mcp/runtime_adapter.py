"""Typed adapters isolating MCP SDK inference gaps."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, cast

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


class McpStdioServer(Protocol):
    def create_initialization_options(self) -> object: ...

    async def run(
        self,
        read_stream: object,
        write_stream: object,
        initialization_options: object,
    ) -> None: ...


def create_mcp_server(
    name: str,
    *,
    on_list_tools: Callable[
        [object, types.PaginatedRequestParams | None],
        Awaitable[types.ListToolsResult],
    ],
    on_call_tool: Callable[
        [object, types.CallToolRequestParams],
        Awaitable[types.CallToolResult],
    ],
) -> McpStdioServer:
    server: Server[object] = Server(
        name,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    return cast(McpStdioServer, server)


@asynccontextmanager
async def open_stdio_streams() -> AsyncIterator[tuple[object, object]]:
    ctx = cast(
        AbstractAsyncContextManager[tuple[object, object]],
        stdio_server(),
    )
    async with ctx as (read_stream, write_stream):
        yield read_stream, write_stream
