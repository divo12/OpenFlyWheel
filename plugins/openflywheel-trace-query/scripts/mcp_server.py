#!/usr/bin/env python3
"""Official typed MCP surface for read-only OpenFlyWheel trace queries."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Annotated, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ofw.observability.langfuse.contracts import LangfuseProject
from ofw.observability.langfuse.trace_query import (
    GetSpanContextInput,
    GetTraceSchemaInput,
    QuerySpansInput,
    SpanFilters,
    TraceQueryObservation,
    TraceQueryService,
)
from ofw.observability.langfuse.transport import LangfuseHttpClient

QueryInput = TypeVar("QueryInput")
TraceIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
SpanIdentifier = Annotated[str, Field(min_length=1, max_length=256)]

server = FastMCP[None](  # type: ignore[misc]  # MCP auth generics are untyped upstream.
    name="openflywheel-trace-query",
    instructions="Read-only structural ITSMBench trace queries. Never judges or mutates data.",
    log_level="DEBUG",
)
read_only = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _client() -> LangfuseHttpClient:
    project = LangfuseProject.from_env(
        environment=os.environ.get("LANGFUSE_ENVIRONMENT", "ofw-local"),
        allow_private_network=os.environ.get("LANGFUSE_ALLOW_PRIVATE_NETWORK") == "1",
    )
    return LangfuseHttpClient(project)


def _execute(
    query: QueryInput,
    operation: Callable[[TraceQueryService, QueryInput], TraceQueryObservation],
) -> TraceQueryObservation:
    client = _client()
    try:
        return operation(TraceQueryService(client), query)
    finally:
        client.close()


@server.tool(annotations=read_only, structured_output=True)
def get_trace_schema(trace_id: TraceIdentifier) -> TraceQueryObservation:
    """Skim bounded trace structure without loading span input or output."""
    return _execute(GetTraceSchemaInput(trace_id=trace_id), TraceQueryService.get_trace_schema)


@server.tool(annotations=read_only, structured_output=True)
def query_spans(
    trace_id: TraceIdentifier,
    filters: SpanFilters | None = None,
) -> TraceQueryObservation:
    """Find bounded span IDs using exact structural filters."""
    query = QuerySpansInput(trace_id=trace_id, filters=filters or SpanFilters())
    return _execute(query, TraceQueryService.query_spans)


@server.tool(annotations=read_only, structured_output=True)
def get_span_context(
    trace_id: TraceIdentifier,
    span_id: SpanIdentifier,
) -> TraceQueryObservation:
    """Read one span, its parent, and up to ten direct children with bounded excerpts."""
    query = GetSpanContextInput(trace_id=trace_id, span_id=span_id)
    return _execute(query, TraceQueryService.get_span_context)


if __name__ == "__main__":
    server.run(transport="stdio")
