"""Unified OpenFlywheel MCP permission surface."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool

from ofw.evaluation.langfuse import (
    OutcomeScoreSubmission,
    OutcomeStoreObservation,
    OutcomeStoreStatus,
)
from ofw.evaluation.outcome import (
    OutcomeEvaluation,
    OutcomeEvaluationError,
    TaskId,
    VerifierId,
)
from ofw.observability.langfuse.domain import ScoreId, TraceId
from ofw.runtime import EvidenceReference, VerifierVerdict


class OpenFlywheelMcpModule(Protocol):
    server: FastMCP[None]

    def record_outcome(
        self,
        trace_id: str,
        task_id: str,
        verifier_id: str,
        evaluated_at: datetime,
        verdict: VerifierVerdict,
        evidence: tuple[str, ...],
        score: float | None = None,
    ) -> OutcomeStoreObservation: ...


class _FakeOutcomeStore:
    def __init__(self) -> None:
        self.outcomes: list[OutcomeEvaluation] = []
        self.close_count = 0

    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        self.outcomes.append(outcome)
        return OutcomeScoreSubmission(ScoreId("score-1"), outcome.trace_id)

    def close(self) -> None:
        self.close_count += 1


def _module() -> OpenFlywheelMcpModule:
    path = Path(__file__).parents[1] / "plugins/openflywheel/scripts/mcp_server.py"
    spec = importlib.util.spec_from_file_location("openflywheel_mcp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(OpenFlywheelMcpModule, module)


def _server() -> FastMCP[None]:
    return _module().server


def _annotation_flags(tool: Tool) -> tuple[bool | None, bool | None, bool | None]:
    annotations = tool.annotations
    assert annotations is not None
    return (
        annotations.readOnlyHint,
        annotations.destructiveHint,
        annotations.idempotentHint,
    )


def test_mcp_exposes_scoped_read_and_outcome_write_tools() -> None:
    tools = asyncio.run(_server().list_tools())

    assert [tool.name for tool in tools] == [
        "list_traces",
        "get_trace_schema",
        "query_spans",
        "get_span_context",
        "record_outcome",
    ]
    assert tuple(map(_annotation_flags, tools)) == (
        (True, False, True),
        (True, False, True),
        (True, False, True),
        (True, False, True),
        (False, False, True),
    )


def test_record_outcome_maps_the_strict_contract_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    store = _FakeOutcomeStore()

    def outcome_store() -> _FakeOutcomeStore:
        return store

    monkeypatch.setattr(module, "_outcome_store", outcome_store)
    evaluated_at = datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC)

    result = module.record_outcome(
        trace_id="trace-1",
        task_id="task-1",
        verifier_id="verifier@v1",
        evaluated_at=evaluated_at,
        verdict=VerifierVerdict.PASS,
        score=1.0,
        evidence=("artifact://result",),
    )

    assert result == OutcomeStoreObservation(
        status=OutcomeStoreStatus.SUCCESS,
        summary="Stored authoritative pass outcome on the trace.",
        next_actions=("Continue only after retaining this score receipt.",),
        artifacts=("trace-1", "score-1"),
        trace_id="trace-1",
        score_id="score-1",
    )
    assert store.outcomes == [
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=evaluated_at,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=(EvidenceReference("artifact://result"),),
        )
    ]
    assert store.close_count == 1


def test_invalid_outcome_fails_before_opening_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    opened = False

    def outcome_store() -> _FakeOutcomeStore:
        nonlocal opened
        opened = True
        return _FakeOutcomeStore()

    monkeypatch.setattr(module, "_outcome_store", outcome_store)

    with pytest.raises(OutcomeEvaluationError):
        module.record_outcome(
            trace_id="trace-1",
            task_id="task-1",
            verifier_id="verifier@v1",
            evaluated_at=datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC),
            verdict=VerifierVerdict.PASS,
            score=None,
            evidence=("artifact://result",),
        )

    assert opened is False
