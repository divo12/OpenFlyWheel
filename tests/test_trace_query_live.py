"""Opt-in contract check against a real Langfuse v4 trace."""

from __future__ import annotations

import os

import pytest

from ofw import LangfuseProject
from ofw.observability.langfuse.domain import ObservationPage
from ofw.observability.langfuse.trace_query import (
    GetTraceSchemaInput,
    ObservationRead,
    QuerySpansInput,
    QueryStatus,
    TraceQueryService,
)
from ofw.observability.langfuse.transport import LangfuseHttpClient


class CountingLiveReader:
    def __init__(self, client: LangfuseHttpClient) -> None:
        self.client = client
        self.read_count = 0

    def read_observations(self, query: ObservationRead) -> ObservationPage:
        self.read_count += 1
        return self.client.read_observations(query)


@pytest.mark.live_langfuse
def test_live_langfuse_reader_is_read_only_and_ambiguity_stays_local() -> None:
    trace_id = os.environ.get("LANGFUSE_INTEGRATION_TRACE_ID")
    if trace_id is None:
        pytest.skip("LANGFUSE_INTEGRATION_TRACE_ID is not configured")
    project = LangfuseProject.from_env(
        environment=os.environ.get("LANGFUSE_INTEGRATION_ENVIRONMENT", "ofw-local"),
    )
    client = LangfuseHttpClient(project)
    reader = CountingLiveReader(client)
    service = TraceQueryService(reader)
    try:
        ambiguous = service.query_spans(QuerySpansInput(trace_id=trace_id))
        assert reader.read_count == 0
        schema = service.get_trace_schema(GetTraceSchemaInput(trace_id=trace_id))
    finally:
        client.close()

    assert ambiguous.status is QueryStatus.NEEDS_INPUT
    assert schema.status is QueryStatus.SUCCESS
    assert reader.read_count == 1
