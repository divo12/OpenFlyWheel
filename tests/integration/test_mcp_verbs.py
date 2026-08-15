"""MCP frozen verbs parity tests."""

from pydantic import BaseModel, ConfigDict
from tests.book_helpers import owner_identity, setup_book_pipeline

from openflywheel.contracts.book import BookContextRequest
from openflywheel.mcp.server import McpBookServer


class HiddenOpInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str


def test_mcp_book_context_matches_application(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    request = BookContextRequest(
        workspace_id=workspace_id,
        identity_id=owner,
        query="architecture",
    )
    cli_result = book.book_context(request)
    assert cli_result.error is None

    server = McpBookServer(book)
    mcp_result = server.call_tool("book_context", request)
    assert mcp_result.error_code is None
    assert mcp_result.summary == cli_result.summary


def test_mcp_hidden_op_rejected(workspace_home, fixture_root) -> None:
    _, book, _ = setup_book_pipeline(workspace_home, fixture_root)
    server = McpBookServer(book)
    result = server.call_tool("hidden_op", HiddenOpInput(value="nope"))
    assert result.error_code == "MCP_UNKNOWN_TOOL"


def test_mcp_lists_frozen_verbs_only(workspace_home, fixture_root) -> None:
    _, book, _ = setup_book_pipeline(workspace_home, fixture_root)
    server = McpBookServer(book)
    tools = server.list_tools()
    assert "book_context" in tools
    assert "hidden_op" not in tools
