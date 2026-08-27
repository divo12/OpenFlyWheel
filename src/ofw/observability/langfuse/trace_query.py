"""Typed, bounded observations for read-only trace navigation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from ofw.observability.langfuse.contracts import TraceWindow
from ofw.observability.langfuse.domain import (
    ObservationContent,
    ObservationContentReference,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    PageCursor,
    TraceId,
)

_STRUCTURE_LIMIT = 50
_CONTEXT_CHILD_LIMIT = 10
_EXCERPT_LIMIT = 512
_CURSOR_LIMIT = 4096
_TYPE_SAMPLE_LIMIT = 3

IdentifierValue = Annotated[StrictStr, Field(min_length=1, max_length=256)]
SessionIdentifier = Annotated[StrictStr, Field(min_length=1, max_length=199)]
CursorValue = Annotated[StrictStr, Field(min_length=1, max_length=_CURSOR_LIMIT)]
PageLimit = Annotated[StrictInt, Field(ge=1, le=_STRUCTURE_LIMIT)]
TextValue = Annotated[StrictStr, Field(min_length=1, max_length=1024)]
MetadataKey = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class QueryStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    NOT_FOUND = "not_found"


class QueryOrdering(StrEnum):
    NONE = "none"
    PAGE_START_TIME_DESC_ID_DESC = "page_start_time_desc_id_desc"
    PARENT_ANCHOR_CHILDREN_DESC = "parent_anchor_children_desc"


class ObservationFieldGroup(StrEnum):
    CORE = "core"
    BASIC = "basic"
    IO = "io"
    TRACE_CONTEXT = "trace_context"


class SpanTextField(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    METADATA = "metadata"


class SpanTextMatch(StrEnum):
    EXACT = "exact"
    TOKEN_PHRASE = "token_phrase"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceTimeRange(StrictModel):
    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def validate_range(self) -> TraceTimeRange:
        _validate_utc(self.start_time)
        _validate_utc(self.end_time)
        _validate_range(self.start_time, self.end_time)
        return self


class SpanTextFilter(StrictModel):
    field: SpanTextField
    match: SpanTextMatch
    text: TextValue
    metadata_key: MetadataKey | None = None

    @model_validator(mode="after")
    def validate_metadata_key(self) -> SpanTextFilter:
        _validate_metadata_key(self.field, self.metadata_key)
        return self


class SpanFilters(StrictModel):
    observation_id: IdentifierValue | None = None
    tool_name: IdentifierValue | None = None
    span_type: ObservationType | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    error: StrictBool | None = None
    text: SpanTextFilter | None = None
    max_results: PageLimit = 20

    @model_validator(mode="after")
    def validate_timestamps(self) -> SpanFilters:
        _validate_utc(self.start_time)
        _validate_utc(self.end_time)
        _validate_range(self.start_time, self.end_time)
        return self

    def missing_field(self) -> str | None:
        if (self.start_time is None) != (self.end_time is None):
            return "start_time" if self.start_time is None else "end_time"
        return None if self.has_selector() else "span_type"

    def has_selector(self) -> bool:
        return any(
            value is not None
            for value in (
                self.observation_id,
                self.tool_name,
                self.span_type,
                self.start_time,
                self.error,
                self.text,
            )
        )

    def applied(self) -> AppliedSpanFilters:
        return AppliedSpanFilters(
            observation_id=self.observation_id,
            tool_name=self.tool_name,
            span_type=self.span_type,
            start_time=self.start_time,
            end_time=self.end_time,
            error=self.error,
            text=self.text,
            max_results=self.max_results,
        )


class AppliedSpanFilters(StrictModel):
    observation_id: IdentifierValue | None = None
    tool_name: IdentifierValue | None = None
    span_type: ObservationType | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    error: StrictBool | None = None
    text: SpanTextFilter | None = None
    max_results: PageLimit | None = None
    span_id: IdentifierValue | None = None


class GetTraceSchemaInput(StrictModel):
    trace_id: IdentifierValue
    cursor: CursorValue | None = None


class QuerySpansInput(StrictModel):
    trace_id: IdentifierValue
    filters: SpanFilters = Field(default_factory=SpanFilters)
    cursor: CursorValue | None = None


class GetSpanContextInput(StrictModel):
    trace_id: IdentifierValue
    span_id: IdentifierValue
    cursor: CursorValue | None = None


class ListTracesInput(StrictModel):
    session_id: SessionIdentifier
    environment: IdentifierValue | None = None
    release: IdentifierValue | None = None
    time_range: TraceTimeRange
    cursor: CursorValue | None = None
    limit: PageLimit = 20


class AppliedTraceFilters(StrictModel):
    session_id: SessionIdentifier
    environment: IdentifierValue | None = None
    release: IdentifierValue | None = None
    time_range: TraceTimeRange
    limit: PageLimit


class TraceFound(StrictModel):
    trace_id: IdentifierValue
    sample_span_id: IdentifierValue
    label: StrictStr = Field(max_length=600)
    start_time: datetime
    environment: IdentifierValue | None = None
    release: IdentifierValue | None = None
    session_id: SessionIdentifier | None = None


class TraceListObservation(StrictModel):
    status: QueryStatus
    summary: StrictStr = Field(max_length=256)
    next_actions: tuple[StrictStr, ...] = Field(max_length=2)
    artifacts: tuple[IdentifierValue, ...] = Field(max_length=_STRUCTURE_LIMIT)
    ordering: QueryOrdering
    filters_applied: AppliedTraceFilters
    traces_found: tuple[TraceFound, ...] = Field(max_length=_STRUCTURE_LIMIT)
    next_cursor: CursorValue | None = None
    truncated: StrictBool = False


class SpanFound(StrictModel):
    span_id: str = Field(max_length=256)
    label: str = Field(max_length=600)
    input_excerpt: str | None = Field(default=None, max_length=_EXCERPT_LIMIT)
    output_excerpt: str | None = Field(default=None, max_length=_EXCERPT_LIMIT)


class SpanTypeSummary(StrictModel):
    span_type: ObservationType
    count: int = Field(ge=1, le=_STRUCTURE_LIMIT)
    sample_span_ids: tuple[str, ...] = Field(max_length=_TYPE_SAMPLE_LIMIT)


class TraceQueryObservation(StrictModel):
    status: QueryStatus
    summary: str = Field(max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(max_length=_STRUCTURE_LIMIT)
    trace_id: str = Field(max_length=256)
    ordering: QueryOrdering
    filters_applied: AppliedSpanFilters
    spans_found: tuple[SpanFound, ...] = Field(max_length=_STRUCTURE_LIMIT)
    span_types: tuple[SpanTypeSummary, ...] = Field(max_length=len(ObservationType))
    missing_fields: tuple[str, ...] = Field(default=(), max_length=1)
    next_cursor: CursorValue | None = None
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ObservationRead:
    trace_id: TraceId | None
    fields: tuple[ObservationFieldGroup, ...]
    limit: int
    window: TraceWindow | None = None
    observation_id: str | None = None
    name: str | None = None
    observation_type: ObservationType | None = None
    error: bool | None = None
    text_filter: SpanTextFilter | None = None
    parent_observation_id: str | None = None
    session_id: str | None = None
    environment: str | None = None
    release: str | None = None
    cursor: PageCursor | None = None


class ObservationReader(Protocol):
    def read_observations(self, query: ObservationRead) -> ObservationPage: ...


class TraceQueryService:
    def __init__(self, reader: ObservationReader) -> None:
        self._reader = reader

    def list_traces(self, query: ListTracesInput) -> TraceListObservation:
        page = self._reader.read_observations(_trace_list_read(query))
        traces = _trace_summaries(_ordered(page.records))
        next_cursor = _next_cursor(page.cursor)
        return TraceListObservation(
            status=QueryStatus.SUCCESS,
            summary=f"Found {len(traces)} traces for the session page.",
            next_actions=_trace_list_actions(next_cursor),
            artifacts=tuple(trace.trace_id for trace in traces),
            ordering=QueryOrdering.PAGE_START_TIME_DESC_ID_DESC,
            filters_applied=AppliedTraceFilters(
                session_id=query.session_id,
                environment=query.environment,
                release=query.release,
                time_range=query.time_range,
                limit=query.limit,
            ),
            traces_found=traces,
            next_cursor=next_cursor,
            truncated=next_cursor is not None,
        )

    def get_trace_schema(self, query: GetTraceSchemaInput) -> TraceQueryObservation:
        page = self._reader.read_observations(
            ObservationRead(
                trace_id=TraceId(query.trace_id),
                fields=(ObservationFieldGroup.CORE, ObservationFieldGroup.BASIC),
                limit=_STRUCTURE_LIMIT,
                cursor=_cursor(query.cursor),
            )
        )
        return _success(
            query.trace_id,
            AppliedSpanFilters(),
            _ordered(page.records),
            (),
            "Trace structure loaded without span input or output.",
            _next_cursor(page.cursor),
            QueryOrdering.PAGE_START_TIME_DESC_ID_DESC,
        )

    def query_spans(self, query: QuerySpansInput) -> TraceQueryObservation:
        missing_field = query.filters.missing_field()
        if missing_field is not None:
            return _needs_input(query.trace_id, query.filters.applied(), missing_field)
        page = self._reader.read_observations(_span_read(query))
        return _success(
            query.trace_id,
            query.filters.applied(),
            _ordered(page.records),
            (),
            f"Found {len(page.records)} spans using exact structural filters.",
            _next_cursor(page.cursor),
            QueryOrdering.PAGE_START_TIME_DESC_ID_DESC,
        )

    def get_span_context(self, query: GetSpanContextInput) -> TraceQueryObservation:
        anchor_page = self._reader.read_observations(_anchor_read(query))
        if not anchor_page.records:
            return _not_found(query)
        anchor = anchor_page.records[0]
        parent_page = self._parent_page(query, anchor)
        child_page = self._reader.read_observations(_children_read(query))
        children = _ordered(child_page.records)
        records = parent_page.records[:1] + (anchor,) + children
        contents = parent_page.contents + anchor_page.contents + child_page.contents
        return _success(
            query.trace_id,
            AppliedSpanFilters(span_id=query.span_id),
            records,
            contents,
            "Loaded the span, its parent, and direct children with bounded raw excerpts.",
            _next_cursor(child_page.cursor),
            QueryOrdering.PARENT_ANCHOR_CHILDREN_DESC,
        )

    def _parent_page(
        self,
        query: GetSpanContextInput,
        anchor: ObservationRecord,
    ) -> ObservationPage:
        if anchor.parent_observation_id is None:
            return ObservationPage((), None)
        return self._reader.read_observations(
            _context_read(query.trace_id, observation_id=anchor.parent_observation_id.value)
        )


def _trace_list_read(query: ListTracesInput) -> ObservationRead:
    return ObservationRead(
        trace_id=None,
        fields=(
            ObservationFieldGroup.CORE,
            ObservationFieldGroup.BASIC,
            ObservationFieldGroup.TRACE_CONTEXT,
        ),
        limit=query.limit,
        window=TraceWindow(query.time_range.start_time, query.time_range.end_time),
        session_id=query.session_id,
        environment=query.environment,
        release=query.release,
        cursor=_cursor(query.cursor),
    )


def _trace_summaries(records: tuple[ObservationRecord, ...]) -> tuple[TraceFound, ...]:
    traces: list[TraceFound] = []
    seen: set[str] = set()
    for record in records:
        if record.trace_id is None or record.trace_id.value in seen:
            continue
        seen.add(record.trace_id.value)
        traces.append(_trace_summary(record))
    return tuple(traces)


def _trace_summary(record: ObservationRecord) -> TraceFound:
    if record.trace_id is None:
        raise ValueError("trace summary requires trace_id")
    return TraceFound(
        trace_id=record.trace_id.value,
        sample_span_id=record.id.value,
        label=record.trace_name or record.name or record.trace_id.value,
        start_time=record.start_time,
        environment=record.environment,
        release=record.release,
        session_id=record.session_id,
    )


def _trace_list_actions(next_cursor: str | None) -> tuple[str, ...]:
    if next_cursor is not None:
        return ("Continue with next_cursor using the same session filters.",)
    return ("Select one trace_id for structural span search.",)


def _span_read(query: QuerySpansInput) -> ObservationRead:
    filters = query.filters
    return ObservationRead(
        trace_id=TraceId(query.trace_id),
        fields=(ObservationFieldGroup.CORE, ObservationFieldGroup.BASIC),
        limit=filters.max_results,
        window=_window(filters),
        observation_id=filters.observation_id,
        name=_tool_name(filters.tool_name),
        observation_type=_span_type(filters),
        error=filters.error,
        text_filter=filters.text,
        cursor=_cursor(query.cursor),
    )


def _window(filters: SpanFilters) -> TraceWindow | None:
    if filters.start_time is None or filters.end_time is None:
        return None
    return TraceWindow(filters.start_time, filters.end_time)


def _tool_name(tool_name: str | None) -> str | None:
    return None if tool_name is None else f"Tool: {tool_name}"


def _span_type(filters: SpanFilters) -> ObservationType | None:
    return ObservationType.TOOL if filters.tool_name is not None else filters.span_type


def _anchor_read(query: GetSpanContextInput) -> ObservationRead:
    return _context_read(query.trace_id, observation_id=query.span_id)


def _children_read(query: GetSpanContextInput) -> ObservationRead:
    return _context_read(
        query.trace_id,
        parent_observation_id=query.span_id,
        limit=_CONTEXT_CHILD_LIMIT,
        cursor=_cursor(query.cursor),
    )


def _context_read(
    trace_id: str,
    *,
    observation_id: str | None = None,
    parent_observation_id: str | None = None,
    limit: int = 2,
    cursor: PageCursor | None = None,
) -> ObservationRead:
    return ObservationRead(
        trace_id=TraceId(trace_id),
        fields=(
            ObservationFieldGroup.CORE,
            ObservationFieldGroup.BASIC,
            ObservationFieldGroup.IO,
        ),
        limit=limit,
        observation_id=observation_id,
        parent_observation_id=parent_observation_id,
        cursor=cursor,
    )


def _needs_input(
    trace_id: str,
    filters: AppliedSpanFilters,
    missing_field: str,
) -> TraceQueryObservation:
    return TraceQueryObservation(
        status=QueryStatus.NEEDS_INPUT,
        summary=f"One clarifying field is required: {missing_field}.",
        next_actions=(f"Provide {missing_field}.",),
        artifacts=(),
        trace_id=trace_id,
        ordering=QueryOrdering.NONE,
        filters_applied=filters,
        spans_found=(),
        span_types=(),
        missing_fields=(missing_field,),
    )


def _not_found(query: GetSpanContextInput) -> TraceQueryObservation:
    return TraceQueryObservation(
        status=QueryStatus.NOT_FOUND,
        summary="The requested span was not found in this trace.",
        next_actions=("Verify span_id with get_trace_schema or query_spans.",),
        artifacts=(),
        trace_id=query.trace_id,
        ordering=QueryOrdering.NONE,
        filters_applied=AppliedSpanFilters(span_id=query.span_id),
        spans_found=(),
        span_types=(),
        missing_fields=("span_id",),
    )


def _success(
    trace_id: str,
    filters: AppliedSpanFilters,
    records: tuple[ObservationRecord, ...],
    contents: tuple[ObservationContent, ...],
    summary: str,
    next_cursor: str | None,
    ordering: QueryOrdering,
) -> TraceQueryObservation:
    content_lookup = tuple((content.reference, content.text) for content in contents)
    spans = tuple(_span(record, content_lookup) for record in records)
    return TraceQueryObservation(
        status=QueryStatus.SUCCESS,
        summary=summary,
        next_actions=_next_actions(next_cursor),
        artifacts=tuple(span.span_id for span in spans),
        trace_id=trace_id,
        ordering=ordering,
        filters_applied=filters,
        spans_found=spans,
        span_types=_span_types(records),
        next_cursor=next_cursor,
        truncated=next_cursor is not None,
    )


def _next_actions(next_cursor: str | None) -> tuple[str, ...]:
    if next_cursor is not None:
        return ("Continue with next_cursor, or narrow the filters.",)
    return ("Stop, or expand one span with get_span_context if its raw context is needed.",)


def _ordered(records: tuple[ObservationRecord, ...]) -> tuple[ObservationRecord, ...]:
    return tuple(sorted(records, key=_ordering_key, reverse=True))


def _ordering_key(record: ObservationRecord) -> tuple[datetime, str]:
    return (record.start_time, record.id.value)


def _span_types(records: tuple[ObservationRecord, ...]) -> tuple[SpanTypeSummary, ...]:
    summaries: list[SpanTypeSummary] = []
    for span_type in ObservationType:
        summary = _span_type_summary(records, span_type)
        if summary is not None:
            summaries.append(summary)
    return tuple(summaries)


def _span_type_summary(
    records: tuple[ObservationRecord, ...],
    span_type: ObservationType,
) -> SpanTypeSummary | None:
    matching = tuple(record for record in records if record.type is span_type)
    if not matching:
        return None
    return SpanTypeSummary(
        span_type=span_type,
        count=len(matching),
        sample_span_ids=tuple(record.id.value for record in matching[:_TYPE_SAMPLE_LIMIT]),
    )


def _cursor(value: str | None) -> PageCursor | None:
    return None if value is None else PageCursor(value)


def _next_cursor(cursor: PageCursor | None) -> str | None:
    return None if cursor is None else cursor.value


def _span(
    record: ObservationRecord,
    contents: tuple[tuple[ObservationContentReference, str], ...],
) -> SpanFound:
    relationship = (
        "root"
        if record.parent_observation_id is None
        else f"parent={record.parent_observation_id.value}"
    )
    name = "unnamed" if record.name is None else record.name[:256]
    return SpanFound(
        span_id=record.id.value,
        label=f"{record.type.value} · {name} · {relationship}"[:600],
        input_excerpt=_content_excerpt(record.input_content, contents),
        output_excerpt=_content_excerpt(record.output_content, contents),
    )


def _content_excerpt(
    reference: ObservationContentReference | None,
    contents: tuple[tuple[ObservationContentReference, str], ...],
) -> str | None:
    if reference is None:
        return None
    for candidate, text in contents:
        if candidate == reference:
            return text[:_EXCERPT_LIMIT]
    return None


def _validate_utc(value: datetime | None) -> None:
    if value is not None and value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC")


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and start >= end:
        raise ValueError("start_time must precede end_time")


def _validate_metadata_key(field: SpanTextField, key: str | None) -> None:
    if field is SpanTextField.METADATA and key is None:
        raise ValueError("metadata text filters require metadata_key")
    if field is not SpanTextField.METADATA and key is not None:
        raise ValueError("metadata_key is only valid for metadata text filters")
