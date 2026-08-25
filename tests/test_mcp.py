"""Codex-facing MCP transport for failure mining."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import Client

from ofw.mcp import FailureMiningMcpServer
from ofw.mine import (
    CompletionStatus,
    FailureSourceKind,
    MiningTools,
    TraceMiningCase,
)
from test_mine import (
    RecordedEnvironmentVerifier,
    _collection,
    _context,
    _nomination,
    _revision,
)


def test_codex_can_discover_and_call_failure_mining_tools(tmp_path: Path) -> None:
    revision = _revision(tmp_path)
    collection = _collection(
        tmp_path,
        revision,
        ("Close ticket", "update failed", "Ticket remains open"),
    )
    nomination = _nomination(FailureSourceKind.DOWNSTREAM_FAILURE)
    tools = MiningTools(
        TraceMiningCase(
            nomination.task,
            _context(collection.traces[0].observation_ids),
            nomination.sources,
        ),
        collection,
        RecordedEnvironmentVerifier(
            CompletionStatus.NOT_COMPLETED,
            "Ticket remains open",
        ),
        nomination.sources,
    )
    server = FailureMiningMcpServer(tools)

    async def exercise() -> None:
        async with Client(server.server) as client:
            discovered = await client.list_tools()
            assert {tool.name for tool in discovered.tools} == {
                "adapt",
                "get_mining_case",
                "read_trajectory",
                "search_prior_trajectories",
                "search_trajectory",
                "verify_environment",
            }
            result = await client.call_tool("get_mining_case")
            assert result.is_error is False

    asyncio.run(exercise())
    assert server.get_mining_case().task_id == "close-ticket"
    assert server.search_trajectory("failed", limit=5).status.value == "ok"
    assert server.search_prior_trajectories("failed", limit=5).status.value == "not_found"
    assert len(server.read_trajectory(limit=2).observations) == 2
    assert (
        server.verify_environment("itsm-production", "ticket-closed").completion_status
        is CompletionStatus.NOT_COMPLETED
    )
    assert server.adapt((FailureSourceKind.DOWNSTREAM_FAILURE,), 5).status.value == "ok"
