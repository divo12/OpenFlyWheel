"""Exact read-only Langfuse trace mapping for candidate trials."""

from __future__ import annotations

from ofw.evolution.candidate import (
    CandidateBlockerCode,
    TraceMatch,
    TraceMatchRequest,
)
from ofw.observability.langfuse.contracts import TraceWindow
from ofw.observability.langfuse.domain import ObservationRecord, PageCursor, TraceId
from ofw.observability.langfuse.trace_query import (
    ObservationFieldGroup,
    ObservationRead,
    ObservationReader,
)


class LangfuseCandidateTraceLocator:
    """Accept one trace only when the complete bounded result is unambiguous."""

    def __init__(self, reader: ObservationReader) -> None:
        self._reader = reader

    def locate(self, request: TraceMatchRequest) -> TraceMatch:
        page = self._reader.read_observations(
            ObservationRead(
                trace_id=None,
                fields=(
                    ObservationFieldGroup.CORE,
                    ObservationFieldGroup.BASIC,
                    ObservationFieldGroup.TRACE_CONTEXT,
                ),
                limit=2,
                window=TraceWindow(request.started_at, request.finished_at),
                session_id=request.session_id,
                environment=request.environment,
                release=request.release,
                is_root_observation=True,
            )
        )
        records = tuple(record for record in page.records if record.trace_id is not None)
        trace_ids = _trace_ids(records)
        return _trace_match(trace_ids, page.cursor, records)


def _trace_match(
    trace_ids: tuple[str, ...],
    cursor: PageCursor | None,
    records: tuple[ObservationRecord, ...],
) -> TraceMatch:
    if not trace_ids:
        return TraceMatch(None, CandidateBlockerCode.TRACE_NOT_FOUND)
    if cursor is not None or len(set(trace_ids)) != 1:
        return TraceMatch(None, CandidateBlockerCode.TRACE_AMBIGUOUS)
    return TraceMatch(TraceId(trace_ids[0]).value, None, records[0].total_cost)


def _trace_ids(records: tuple[ObservationRecord, ...]) -> tuple[str, ...]:
    return tuple(record.trace_id.value for record in records if record.trace_id is not None)
