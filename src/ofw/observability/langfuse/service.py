"""Exhaustive in-memory Langfuse collection and direct querying."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from itertools import groupby

from pydantic import ValidationError

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapabilityReason,
    CollectionResult,
    ObservationContent,
    ObservationContentField,
    ObservationContentHit,
    ObservationContentMatch,
    ObservationContentQuery,
    ObservationContentReference,
    ObservationId,
    ObservationPage,
    ObservationRecord,
    PageCursor,
    ScoreId,
    ScoreRecord,
    ScoreSubjectKind,
    SessionId,
    SessionRecord,
    TraceGap,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.observability.langfuse.wire import RevisionMetadata


def collect(
    revision: HarnessRevision,
    *,
    window: TraceWindow,
) -> CollectionResult:
    if revision.observability is None:
        raise CollectionError(
            CollectionErrorCode.OBSERVABILITY_NOT_CONNECTED,
            str(revision.id),
        )
    project = LangfuseProject.from_manifest(revision.observability)
    client = LangfuseHttpClient(project)
    try:
        client.check_health()
        observation_page = _query_observations(client, window)
        scores = _query_scores(client, window)
    finally:
        client.close()
    observations = observation_page.records
    traces, orphan_count = _assemble_traces(observations, scores, revision.id)
    sessions = _assemble_sessions(observations, scores)
    return CollectionResult(
        revision_id=revision.id,
        connection=revision.observability,
        window=window,
        observations=observations,
        contents=observation_page.contents,
        scores=scores,
        traces=traces,
        sessions=sessions,
        gap_count=orphan_count + sum(len(trace.gaps) for trace in traces),
        snapshot_digest=_snapshot_digest(observations, scores, traces, sessions),
        capability=_capability(traces),
    )


def _query_observations(
    client: LangfuseHttpClient,
    window: TraceWindow,
    *,
    trace_id: TraceId | None = None,
    session_id: str | None = None,
    content_field: ObservationContentField | None = None,
    content_match: ObservationContentMatch | None = None,
    content_text: str | None = None,
    maximum_records: int | None = None,
) -> ObservationPage:
    records: list[ObservationRecord] = []
    contents: list[ObservationContent] = []
    content_references: set[ObservationContentReference] = set()
    seen: set[str] = set()
    cursor: PageCursor | None = None
    while True:
        limit = _page_limit(maximum_records, len(records))
        if limit == 0:
            return ObservationPage(tuple(records), None, tuple(contents))
        page = client.get_observations(
            window,
            cursor,
            trace_id=trace_id,
            session_id=session_id,
            content_field=content_field,
            content_match=content_match,
            content_text=content_text,
            limit=limit,
        )
        records.extend(page.records)
        _append_unique_contents(contents, content_references, page.contents)
        if page.cursor is None:
            return ObservationPage(tuple(records), None, tuple(contents))
        _reject_cursor_loop(page.cursor, seen)
        cursor = page.cursor


def _page_limit(maximum_records: int | None, collected: int) -> int:
    if maximum_records is None:
        return 1000
    return max(0, min(1000, maximum_records - collected))


def _append_unique_contents(
    target: list[ObservationContent],
    references: set[ObservationContentReference],
    incoming: tuple[ObservationContent, ...],
) -> None:
    for content in incoming:
        if content.reference in references:
            continue
        references.add(content.reference)
        target.append(content)


def _query_scores(
    client: LangfuseHttpClient,
    window: TraceWindow,
) -> tuple[ScoreRecord, ...]:
    records: list[ScoreRecord] = []
    seen: set[str] = set()
    cursor: PageCursor | None = None
    while True:
        page = client.get_scores(window, cursor)
        records.extend(page.records)
        if page.cursor is None:
            return tuple(records)
        _reject_cursor_loop(page.cursor, seen)
        cursor = page.cursor


def _reject_cursor_loop(cursor: PageCursor | None, seen: set[str]) -> None:
    if cursor is None:
        return
    if cursor.value in seen:
        raise CollectionError(CollectionErrorCode.CURSOR_LOOP, cursor.value)
    seen.add(cursor.value)


def _assemble_traces(
    observations: tuple[ObservationRecord, ...],
    scores: tuple[ScoreRecord, ...],
    revision_id: HarnessRevisionId,
) -> tuple[tuple[TraceRecord, ...], int]:
    attributed = tuple(record for record in observations if record.trace_id is not None)
    orphan_count = len(observations) - len(attributed)
    ordered = tuple(
        sorted(
            attributed,
            key=_observation_sort_key,
        )
    )
    traces: list[TraceRecord] = []
    for trace_id, grouped in groupby(ordered, key=_trace_key):
        records = tuple(grouped)
        traces.append(_assemble_trace(TraceId(trace_id), records, scores, revision_id))
    return tuple(traces), orphan_count


def _observation_sort_key(record: ObservationRecord) -> tuple[str, datetime, str]:
    return (
        "" if record.trace_id is None else record.trace_id.value,
        record.start_time,
        record.id.value,
    )


def _trace_key(record: ObservationRecord) -> str:
    return "" if record.trace_id is None else record.trace_id.value


def _assemble_trace(
    trace_id: TraceId,
    observations: tuple[ObservationRecord, ...],
    scores: tuple[ScoreRecord, ...],
    revision_id: HarnessRevisionId,
) -> TraceRecord:
    observation_ids = tuple(record.id for record in observations)
    root_ids = tuple(
        record.id
        for record in observations
        if record.is_root is True or record.parent_observation_id is None
    )
    score_ids = tuple(
        score.id
        for score in scores
        if _score_belongs(score, trace_id, observation_ids, observations)
    )
    gaps: list[TraceGap] = []
    if not root_ids:
        gaps.append(TraceGap.MISSING_ROOT)
    known_ids = {record.id.value for record in observations}
    if any(
        record.parent_observation_id is not None
        and record.is_root is not True
        and record.parent_observation_id.value not in known_ids
        for record in observations
    ):
        gaps.append(TraceGap.MISSING_PARENT)
    session_id = _single_value(tuple(record.session_id for record in observations))
    environment = _single_value(tuple(record.environment for record in observations))
    release = _single_value(tuple(record.release for record in observations))
    if _multiple_values(tuple(record.session_id for record in observations)):
        gaps.append(TraceGap.MULTIPLE_SESSIONS)
    if _multiple_values(tuple(record.environment for record in observations)):
        gaps.append(TraceGap.MULTIPLE_ENVIRONMENTS)
    if _multiple_values(tuple(record.release for record in observations)):
        gaps.append(TraceGap.MULTIPLE_RELEASES)
    attribution = _attribution(observations, revision_id)
    digest_source = "\0".join(
        (
            trace_id.value,
            attribution.value,
            *(str(record.digest) for record in observations),
            *(score_id.value for score_id in score_ids),
            *(gap.value for gap in gaps),
        )
    )
    return TraceRecord(
        id=trace_id,
        observation_ids=observation_ids,
        root_observation_ids=root_ids,
        score_ids=score_ids,
        session_id=session_id,
        environment=environment,
        release=release,
        attribution=attribution,
        gaps=tuple(gaps),
        digest=Sha256Digest(f"sha256:{hashlib.sha256(digest_source.encode()).hexdigest()}"),
    )


def _score_belongs(
    score: ScoreRecord,
    trace_id: TraceId,
    observation_ids: tuple[ObservationId, ...],
    observations: tuple[ObservationRecord, ...],
) -> bool:
    if score.subject is None:
        return False
    if score.subject.kind is ScoreSubjectKind.TRACE:
        return score.subject.id == trace_id.value
    if score.subject.kind is ScoreSubjectKind.OBSERVATION:
        return any(observation_id.value == score.subject.id for observation_id in observation_ids)
    if score.subject.kind is ScoreSubjectKind.SESSION:
        return any(record.session_id == score.subject.id for record in observations)
    return False


def _assemble_sessions(
    observations: tuple[ObservationRecord, ...],
    scores: tuple[ScoreRecord, ...],
) -> tuple[SessionRecord, ...]:
    attributed = tuple(record for record in observations if record.session_id)
    ordered = sorted(attributed, key=_session_sort_key)
    return tuple(
        _assemble_session(session_id, tuple(grouped), scores)
        for session_id, grouped in groupby(ordered, key=_session_key)
    )


def _assemble_session(
    session_id: str,
    records: tuple[ObservationRecord, ...],
    scores: tuple[ScoreRecord, ...],
) -> SessionRecord:
    observation_ids = tuple(record.id for record in records)
    trace_ids = _unique_trace_ids(records)
    score_ids = _session_score_ids(scores, session_id, trace_ids, observation_ids)
    digest = _session_digest(session_id, trace_ids, records, score_ids)
    return SessionRecord(
        id=SessionId(session_id),
        trace_ids=trace_ids,
        observation_ids=observation_ids,
        score_ids=score_ids,
        digest=digest,
    )


def _session_score_ids(
    scores: tuple[ScoreRecord, ...],
    session_id: str,
    trace_ids: tuple[TraceId, ...],
    observation_ids: tuple[ObservationId, ...],
) -> tuple[ScoreId, ...]:
    return tuple(
        score.id
        for score in scores
        if _score_belongs_session(score, session_id, trace_ids, observation_ids)
    )


def _session_digest(
    session_id: str,
    trace_ids: tuple[TraceId, ...],
    records: tuple[ObservationRecord, ...],
    score_ids: tuple[ScoreId, ...],
) -> Sha256Digest:
    payload = "\0".join(
        (
            session_id,
            *(trace_id.value for trace_id in trace_ids),
            *(str(record.digest) for record in records),
            *(score_id.value for score_id in score_ids),
        )
    )
    return Sha256Digest(f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}")


def _unique_trace_ids(records: tuple[ObservationRecord, ...]) -> tuple[TraceId, ...]:
    trace_ids: list[TraceId] = []
    for record in records:
        if record.trace_id is not None and record.trace_id not in trace_ids:
            trace_ids.append(record.trace_id)
    return tuple(trace_ids)


def _session_sort_key(record: ObservationRecord) -> tuple[str, datetime, str]:
    return (_session_key(record), record.start_time, record.id.value)


def _session_key(record: ObservationRecord) -> str:
    if not record.session_id:
        raise ValueError("session assembly requires a session id")
    return record.session_id


def _score_belongs_session(
    score: ScoreRecord,
    session_id: str,
    trace_ids: tuple[TraceId, ...],
    observation_ids: tuple[ObservationId, ...],
) -> bool:
    if score.subject is None:
        return False
    if score.subject.kind is ScoreSubjectKind.SESSION:
        return score.subject.id == session_id
    if score.subject.kind is ScoreSubjectKind.TRACE:
        return any(trace_id.value == score.subject.id for trace_id in trace_ids)
    if score.subject.kind is ScoreSubjectKind.OBSERVATION:
        return any(item.value == score.subject.id for item in observation_ids)
    return False


def _attribution(
    observations: tuple[ObservationRecord, ...],
    revision_id: HarnessRevisionId,
) -> AttributionLevel:
    revisions: list[str] = []
    for observation in observations:
        if observation.metadata is None:
            continue
        try:
            metadata = RevisionMetadata.model_validate_json(observation.metadata.canonical)
        except ValidationError:
            continue
        if metadata.revision_id is not None and metadata.revision_id not in revisions:
            revisions.append(metadata.revision_id)
    if not revisions:
        releases = {observation.release for observation in observations if observation.release}
        return (
            AttributionLevel.EXACT
            if releases == {str(revision_id)}
            else AttributionLevel.MISSING
        )
    if len(revisions) == 1 and revisions[0] == str(revision_id):
        return AttributionLevel.EXACT
    return AttributionLevel.AMBIGUOUS


def _single_value(values: tuple[str | None, ...]) -> str | None:
    unique = {value for value in values if value}
    return unique.pop() if len(unique) == 1 else None


def _multiple_values(values: tuple[str | None, ...]) -> bool:
    return len({value for value in values if value}) > 1


def _capability(traces: tuple[TraceRecord, ...]) -> CollectionCapabilityReason:
    if not traces:
        reason = CollectionCapabilityReason.NO_TRACES
    elif any(trace.attribution is AttributionLevel.AMBIGUOUS for trace in traces):
        reason = CollectionCapabilityReason.AMBIGUOUS_REVISION_ATTRIBUTION
    elif any(trace.attribution is AttributionLevel.MISSING for trace in traces):
        reason = CollectionCapabilityReason.MISSING_REVISION_ATTRIBUTION
    elif any(trace.gaps for trace in traces):
        reason = CollectionCapabilityReason.INCOMPLETE_TRACE
    else:
        reason = CollectionCapabilityReason.READY
    return reason


def _snapshot_digest(
    observations: tuple[ObservationRecord, ...],
    scores: tuple[ScoreRecord, ...],
    traces: tuple[TraceRecord, ...],
    sessions: tuple[SessionRecord, ...],
) -> Sha256Digest:
    payload = "\0".join(
        (
            *(str(record.digest) for record in observations),
            *(str(record.digest) for record in scores),
            *(str(record.digest) for record in traces),
            *(str(record.digest) for record in sessions),
        )
    )
    return Sha256Digest(f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}")


def search_observation_content(
    collection: CollectionResult,
    query: ObservationContentQuery,
) -> tuple[ObservationContentHit, ...]:
    client = LangfuseHttpClient(LangfuseProject.from_manifest(collection.connection))
    try:
        hits: list[ObservationContentHit] = []
        for field in _content_fields(query.field):
            page = _query_observations(
                client,
                collection.window,
                trace_id=query.trace_id,
                content_field=field,
                content_match=query.match,
                content_text=query.text,
                maximum_records=query.limit,
            )
            hits.extend(_content_hits(page, field, query, query.limit - len(hits)))
            if len(hits) == query.limit:
                return tuple(hits)
        return tuple(hits)
    finally:
        client.close()


def _content_fields(
    field: ObservationContentField,
) -> tuple[ObservationContentField, ...]:
    if field is ObservationContentField.ANY:
        return (ObservationContentField.INPUT, ObservationContentField.OUTPUT)
    return (field,)


def _content_hits(
    page: ObservationPage,
    field: ObservationContentField,
    query: ObservationContentQuery,
    limit: int,
) -> tuple[ObservationContentHit, ...]:
    hits: list[ObservationContentHit] = []
    for observation in page.records:
        content = _observation_content(observation, page.contents, field)
        if content is None or not _content_matches(content.text, query):
            continue
        hits.append(
            ObservationContentHit(
                observation.id,
                observation.trace_id,
                field,
                content.reference,
                _excerpt(content.text, query.text, query.maximum_excerpt_characters),
            )
        )
        if len(hits) == limit:
            return tuple(hits)
    return tuple(hits)


def _observation_content(
    observation: ObservationRecord,
    contents: tuple[ObservationContent, ...],
    field: ObservationContentField,
) -> ObservationContent | None:
    reference = (
        observation.input_content
        if field is ObservationContentField.INPUT
        else observation.output_content
    )
    if reference is None:
        return None
    return next((content for content in contents if content.reference == reference), None)


def read_trace_observations(
    collection: CollectionResult,
    trace_id: TraceId,
    limit: int,
) -> tuple[ObservationRecord, ...]:
    if not 1 <= limit <= 1000:
        raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, str(limit))
    client = LangfuseHttpClient(LangfuseProject.from_manifest(collection.connection))
    try:
        page = _query_observations(
            client,
            collection.window,
            trace_id=trace_id,
            maximum_records=limit,
        )
        return tuple(sorted(page.records, key=_observation_sort_key))
    finally:
        client.close()


def read_session_observations(
    collection: CollectionResult,
    session_id: str,
    limit: int,
) -> tuple[ObservationRecord, ...]:
    if not session_id or not 1 <= limit <= 1000:
        raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, session_id)
    client = LangfuseHttpClient(LangfuseProject.from_manifest(collection.connection))
    try:
        page = _query_observations(
            client,
            collection.window,
            session_id=session_id,
            maximum_records=limit,
        )
        return tuple(sorted(page.records, key=_observation_sort_key))
    finally:
        client.close()


def read_observation_content(
    collection: CollectionResult,
    reference: ObservationContentReference,
) -> ObservationContent:
    content = next(
        (content for content in collection.contents if content.reference == reference),
        None,
    )
    if content is None:
        raise CollectionError(CollectionErrorCode.CONTENT_NOT_CAPTURED, str(reference.digest))
    return content


def _content_matches(text: str, query: ObservationContentQuery) -> bool:
    if query.match is ObservationContentMatch.EXACT:
        return text == query.text
    return re.search(
        rf"(?<!\w){re.escape(query.text)}(?!\w)",
        text,
        flags=re.IGNORECASE,
    ) is not None


def _excerpt(text: str, query: str, limit: int) -> str:
    index = text.casefold().find(query.casefold())
    if index < 0 or len(text) <= limit:
        return text[:limit]
    start = max(0, index - limit // 2)
    return text[start : start + limit]
