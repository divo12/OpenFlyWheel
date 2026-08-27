#!/usr/bin/env python3
"""Typed OpenFlyWheel MCP surface for trace queries and outcome recording."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ofw.evaluation.langfuse import (
    LangfuseOutcomeStore,
    OutcomeStoreObservation,
    OutcomeStoreStatus,
)
from ofw.evaluation.outcome import OutcomeEvaluation, TaskId, VerifierId
from ofw.observability.langfuse.contracts import LangfuseProject
from ofw.observability.langfuse.domain import TraceId
from ofw.observability.langfuse.trace_query import (
    GetSpanContextInput,
    GetTraceSchemaInput,
    ListTracesInput,
    QuerySpansInput,
    SessionIdentifier,
    SpanFilters,
    TraceListObservation,
    TraceQueryObservation,
    TraceQueryService,
    TraceTimeRange,
)
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.runtime import EvidenceReference, VerifierResult, VerifierVerdict

QueryInput = TypeVar("QueryInput")
QueryOutput = TypeVar("QueryOutput", bound=BaseModel)
_QUERY_TIMEOUT_SECONDS = 60.0
TraceIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
SpanIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
CursorIdentifier = Annotated[str, Field(min_length=1, max_length=4096)]
TracePageLimit = Annotated[int, Field(strict=True, ge=1, le=50)]
TaskIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
VerifierIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
OutcomeScore = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
EvidenceIdentifier = Annotated[str, Field(min_length=1, max_length=1024)]
OutcomeEvidence = Annotated[tuple[EvidenceIdentifier, ...], Field(min_length=1, max_length=10)]

server = FastMCP[None](  # type: ignore[misc]  # MCP auth generics are untyped upstream.
    name="openflywheel",
    instructions=(
        "Read bounded Langfuse trace evidence and record only authoritative external-verifier "
        "outcomes. Never infer outcomes or mutate traces."
    ),
    log_level="DEBUG",
)
read_only = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
record_write = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _project() -> LangfuseProject:
    return LangfuseProject.from_env(
        environment=os.environ.get("LANGFUSE_ENVIRONMENT", "ofw-local"),
        allow_private_network=os.environ.get("LANGFUSE_ALLOW_PRIVATE_NETWORK") == "1",
    )


def _client() -> LangfuseHttpClient:
    return LangfuseHttpClient(_project(), timeout_seconds=_QUERY_TIMEOUT_SECONDS)


def _outcome_store() -> LangfuseOutcomeStore:
    return LangfuseOutcomeStore.from_project(_project())


def _execute(
    query: QueryInput,
    operation: Callable[[TraceQueryService, QueryInput], QueryOutput],
) -> QueryOutput:
    client = _client()
    try:
        return operation(TraceQueryService(client), query)
    finally:
        client.close()


@server.tool(annotations=read_only, structured_output=True)
def list_traces(
    session_id: SessionIdentifier,
    time_range: TraceTimeRange,
    environment: TraceIdentifier | None = None,
    release: TraceIdentifier | None = None,
    cursor: CursorIdentifier | None = None,
    limit: TracePageLimit = 20,
) -> TraceListObservation:
    """List bounded logical-root traces for one session and time range."""
    query = ListTracesInput(
        session_id=session_id,
        environment=environment,
        release=release,
        time_range=time_range,
        cursor=cursor,
        limit=limit,
    )
    return _execute(query, TraceQueryService.list_traces)


@server.tool(annotations=read_only, structured_output=True)
def get_trace_schema(
    trace_id: TraceIdentifier,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Skim bounded trace structure without loading span input or output."""
    query = GetTraceSchemaInput(trace_id=trace_id, cursor=cursor)
    return _execute(query, TraceQueryService.get_trace_schema)


@server.tool(annotations=read_only, structured_output=True)
def query_spans(
    trace_id: TraceIdentifier,
    filters: SpanFilters | None = None,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Find bounded span IDs using exact structural filters."""
    query = QuerySpansInput(
        trace_id=trace_id,
        filters=filters or SpanFilters(),
        cursor=cursor,
    )
    return _execute(query, TraceQueryService.query_spans)


@server.tool(annotations=read_only, structured_output=True)
def get_span_context(
    trace_id: TraceIdentifier,
    span_id: SpanIdentifier,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Read one span, its parent, and up to ten direct children with bounded excerpts."""
    query = GetSpanContextInput(trace_id=trace_id, span_id=span_id, cursor=cursor)
    return _execute(query, TraceQueryService.get_span_context)


@server.tool(annotations=record_write, structured_output=True)
def record_outcome(
    trace_id: TraceIdentifier,
    task_id: TaskIdentifier,
    verifier_id: VerifierIdentifier,
    evaluated_at: datetime,
    verdict: VerifierVerdict,
    evidence: OutcomeEvidence,
    score: OutcomeScore | None = None,
) -> OutcomeStoreObservation:
    """Record one authoritative external-verifier outcome on its exact trace."""
    result = VerifierResult(
        verdict=verdict,
        score=score,
        feedback="Recorded by the OpenFlywheel outcome tool.",
        evidence=tuple(EvidenceReference(reference) for reference in evidence),
    )
    outcome = OutcomeEvaluation.from_verifier_result(
        trace_id=TraceId(trace_id),
        task_id=TaskId(task_id),
        verifier_id=VerifierId(verifier_id),
        evaluated_at=evaluated_at,
        result=result,
    )
    store = _outcome_store()
    try:
        submission = store.store(outcome)
    finally:
        store.close()
    return OutcomeStoreObservation(
        status=OutcomeStoreStatus.SUCCESS,
        summary=f"Stored authoritative {verdict.value} outcome on the trace.",
        next_actions=("Continue only after retaining this score receipt.",),
        artifacts=(trace_id, submission.score_id.value),
        trace_id=trace_id,
        score_id=submission.score_id.value,
    )


if __name__ == "__main__":
    server.run(transport="stdio")
