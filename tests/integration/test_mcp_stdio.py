"""MCP stdio subprocess integration tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tests.book_helpers import owner_identity, setup_book_pipeline
from typer.testing import CliRunner

from openflywheel.cli.main import app
from openflywheel.contracts.book import BookContextRequest
from openflywheel.contracts.pydantic_json import model_dump_object_dict
from openflywheel.mcp.server import FROZEN_VERBS, McpBookServer


async def _run_stdio_checks(
    home: Path,
    request: BookContextRequest,
    direct_envelope: dict[str, object],
) -> dict[str, object]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openflywheel.cli.main",
            "serve",
            "--surface",
            "verbs",
            "--home",
            str(home),
        ],
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert tool_names == set(FROZEN_VERBS)

        mcp_result = await session.call_tool(
            "book_context",
            arguments=request.model_dump(mode="json"),
        )
        assert mcp_result.is_error is False
        assert mcp_result.content
        mcp_text = mcp_result.content[0].text
        assert isinstance(mcp_text, str)
        payload = json.loads(mcp_text)
        assert payload == direct_envelope

        hidden = await session.call_tool("hidden_op", arguments={"value": "nope"})
        assert hidden.is_error is True
        assert hidden.content
        hidden_text = hidden.content[0].text
        assert isinstance(hidden_text, str)
        assert "Unknown tool" in hidden_text
        return payload


def test_mcp_stdio_subprocess_parity(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    request = BookContextRequest(
        workspace_id=workspace_id,
        identity_id=owner,
        query="architecture",
    )
    app_result = book.book_context(request)
    assert app_result.error is None

    server = McpBookServer(book)
    direct_envelope = model_dump_object_dict(server.call_tool("book_context", request))

    cli = CliRunner()
    cli_run = cli.invoke(
        app,
        [
            "book",
            "context",
            "architecture",
            "--home",
            str(home),
            "--identity",
            str(owner),
        ],
    )
    assert cli_run.exit_code == 0
    cli_payload = json.loads(cli_run.stdout)
    assert cli_payload["status"] == "success"
    assert cli_payload.get("data") is not None
    assert direct_envelope["data"] is not None
    assert cli_payload["data"]["markdown"] == direct_envelope["data"]["markdown"]

    mcp_payload = asyncio.run(_run_stdio_checks(home, request, direct_envelope))
    assert mcp_payload["data"] is not None
    assert mcp_payload["data"]["markdown"] == direct_envelope["data"]["markdown"]
    assert mcp_payload["data"]["packet"] == direct_envelope["data"]["packet"]
