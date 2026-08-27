"""Bounded, read-only trace query contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.domain import (
    JsonDocument,
    ObservationContent,
    ObservationContentReference,
    ObservationId,
    ObservationLevel,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    PageCursor,
    ProjectId,
    TraceId,
)
from ofw.observability.langfuse.trace_query import (
    AppliedSpanFilters,
    GetSpanContextInput,
    GetTraceSchemaInput,
    ObservationFieldGroup,
    ObservationRead,
    QueryOrdering,
    QuerySpansInput,
    QueryStatus,
    SpanFilters,
    TraceQueryService,
)


def _record(
    observation_id: str,
    *,
    name: str,
    kind: ObservationType,
    parent_id: str | None = None,
    level: ObservationLevel = ObservationLevel.DEFAULT,
    second: int = 0,
    input_text: str | None = None,
    output_text: str | None = None,
) -> tuple[ObservationRecord, tuple[ObservationContent, ...]]:
    contents = tuple(
        ObservationContent(ObservationContentReference.for_text(text), text)
        for text in (input_text, output_text)
        if text is not None
    )
    input_reference = (
        None if input_text is None else ObservationContentReference.for_text(input_text)
    )
    output_reference = (
        None if output_text is None else ObservationContentReference.for_text(output_text)
    )
    start = datetime(2026, 8, 27, 12, 0, second, tzinfo=UTC)
    return (
        ObservationRecord(
            id=ObservationId(observation_id),
            trace_id=TraceId("trace-1"),
            start_time=start,
            end_time=start + timedelta(seconds=1),
            project_id=ProjectId("project-1"),
            parent_observation_id=None if parent_id is None else ObservationId(parent_id),
            type=kind,
            is_root=parent_id is None,
            name=name,
            level=level,
            version=None,
            environment="ofw-local",
            user_id=None,
            session_id="itsm-session",
            created_at=None,
            updated_at=None,
            metadata=JsonDocument("{}"),
            usage=None,
            costs=None,
            total_cost=None,
            tags=(),
            release="itsm-bench",
            trace_name="itsm-task",
            raw=JsonDocument("{}"),
            digest=Sha256Digest("sha256:" + "0" * 64),
            input_content=input_reference,
            output_content=output_reference,
        ),
        contents,
    )


class FakeClient:
    def __init__(self, pages: list[ObservationPage]) -> None:
        self.pages = pages
        self.calls: list[ObservationRead] = []

    def read_observations(self, query: ObservationRead) -> ObservationPage:
        self.calls.append(query)
        return self.pages.pop(0)


def test_contracts_are_strict_and_bounded() -> None:
    with pytest.raises(ValidationError):
        GetTraceSchemaInput.model_validate_json('{"trace_id":"trace-1","unknown":true}')
    with pytest.raises(ValidationError):
        SpanFilters(max_results=51)
    with pytest.raises(ValidationError):
        SpanFilters(start_time=datetime(2026, 8, 27), end_time=datetime(2026, 8, 28))
    with pytest.raises(ValidationError):
        SpanFilters(
            start_time=datetime(2026, 8, 28, tzinfo=UTC),
            end_time=datetime(2026, 8, 27, tzinfo=UTC),
        )


def test_ambiguous_query_requests_one_field_without_reading() -> None:
    client = FakeClient([])
    result = TraceQueryService(client).query_spans(QuerySpansInput(trace_id="trace-1"))

    assert result.missing_fields == ("span_type",)
    assert result.status is QueryStatus.NEEDS_INPUT
    assert result.spans_found == ()
    assert client.calls == []

    partial_time = TraceQueryService(client).query_spans(
        QuerySpansInput(
            trace_id="trace-1",
            filters=SpanFilters(start_time=datetime(2026, 8, 27, tzinfo=UTC)),
        )
    )
    assert partial_time.missing_fields == ("end_time",)
    assert client.calls == []


def test_schema_skims_structure_without_io() -> None:
    root, _ = _record("root", name="Agent", kind=ObservationType.AGENT)
    tool, _ = _record(
        "tool", name="Tool: get_ticket", kind=ObservationType.TOOL, parent_id="root", second=1
    )
    client = FakeClient([ObservationPage((root, tool), None)])

    result = TraceQueryService(client).get_trace_schema(GetTraceSchemaInput(trace_id="trace-1"))

    assert [(span.span_id, span.label) for span in result.spans_found] == [
        ("tool", "TOOL · Tool: get_ticket · parent=root"),
        ("root", "AGENT · Agent · root"),
    ]
    assert result.truncated is False
    assert result.next_cursor is None
    assert result.ordering is QueryOrdering.PAGE_START_TIME_DESC_ID_DESC
    assert client.calls == [
        ObservationRead(
            trace_id=TraceId("trace-1"),
            fields=(ObservationFieldGroup.CORE, ObservationFieldGroup.BASIC),
            limit=50,
        )
    ]


def test_schema_cursor_finds_late_types_and_orders_ties_by_id() -> None:
    newest_b, _ = _record("b", name="Agent B", kind=ObservationType.AGENT, second=2)
    newest_a, _ = _record("a", name="Agent A", kind=ObservationType.AGENT, second=2)
    late, _ = _record("late", name="Guard", kind=ObservationType.GUARDRAIL, second=1)
    client = FakeClient(
        [
            ObservationPage((newest_a, newest_b), PageCursor("page-2")),
            ObservationPage((late,), None),
        ]
    )
    service = TraceQueryService(client)

    first = service.get_trace_schema(GetTraceSchemaInput(trace_id="trace-1"))
    second = service.get_trace_schema(
        GetTraceSchemaInput(trace_id="trace-1", cursor=first.next_cursor)
    )

    assert [span.span_id for span in first.spans_found] == ["b", "a"]
    assert first.next_cursor == "page-2"
    assert first.truncated is True
    assert first.span_types[0].span_type is ObservationType.AGENT
    assert first.span_types[0].count == 2
    assert second.next_cursor is None
    assert second.span_types[0].span_type is ObservationType.GUARDRAIL
    assert client.calls[1].cursor == PageCursor("page-2")


def test_query_spans_applies_deterministic_server_filters() -> None:
    tool, _ = _record(
        "tool",
        name="Tool: get_ticket",
        kind=ObservationType.TOOL,
        parent_id="root",
        level=ObservationLevel.ERROR,
    )
    client = FakeClient([ObservationPage((tool,), PageCursor("next-page"))])
    filters = SpanFilters(tool_name="get_ticket", error=True, max_results=5)

    result = TraceQueryService(client).query_spans(
        QuerySpansInput(trace_id="trace-1", filters=filters, cursor="current-page")
    )

    assert result.filters_applied == AppliedSpanFilters(
        tool_name="get_ticket", error=True, max_results=5
    )
    assert [span.span_id for span in result.spans_found] == ["tool"]
    assert result.next_cursor == "next-page"
    assert client.calls == [
        ObservationRead(
            trace_id=TraceId("trace-1"),
            fields=(ObservationFieldGroup.CORE, ObservationFieldGroup.BASIC),
            limit=6,
            name="Tool: get_ticket",
            observation_type=ObservationType.TOOL,
            error=True,
            cursor=PageCursor("current-page"),
        )
    ]


def test_span_context_is_bounded_and_includes_raw_excerpts() -> None:
    anchor, anchor_content = _record(
        "tool",
        name="Tool: get_ticket",
        kind=ObservationType.TOOL,
        parent_id="root",
        input_text="x" * 700,
        output_text="ticket found",
    )
    parent, parent_content = _record(
        "root", name="Agent", kind=ObservationType.AGENT, input_text="resolve ticket"
    )
    child, child_content = _record(
        "generation",
        name="LLM call",
        kind=ObservationType.GENERATION,
        parent_id="tool",
        output_text="next action",
    )
    client = FakeClient(
        [
            ObservationPage((anchor,), None, anchor_content),
            ObservationPage((parent,), None, parent_content),
            ObservationPage((child,), PageCursor("next-children"), child_content),
        ]
    )

    result = TraceQueryService(client).get_span_context(
        GetSpanContextInput(trace_id="trace-1", span_id="tool", cursor="current-children")
    )

    assert [span.span_id for span in result.spans_found] == ["root", "tool", "generation"]
    assert len(result.spans_found[1].input_excerpt or "") == 512
    assert result.spans_found[1].output_excerpt == "ticket found"
    assert result.next_cursor == "next-children"
    assert result.ordering is QueryOrdering.PARENT_ANCHOR_CHILDREN_DESC
    assert all(call.limit <= 11 for call in client.calls)
    assert all(
        call.fields
        == (
            ObservationFieldGroup.CORE,
            ObservationFieldGroup.BASIC,
            ObservationFieldGroup.IO,
        )
        for call in client.calls
    )
    assert client.calls[-1].cursor == PageCursor("current-children")
