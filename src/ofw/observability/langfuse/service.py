"""Restart-safe Langfuse collection and trace assembly."""

from __future__ import annotations

import hashlib
from datetime import datetime
from itertools import groupby
from pathlib import Path

from pydantic import ValidationError

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    ContentCaptureMode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapabilityReason,
    CollectionResult,
    CollectionSyncId,
    ObservationContent,
    ObservationContentHit,
    ObservationContentQuery,
    ObservationContentReference,
    ObservationId,
    ObservationRecord,
    PageCursor,
    ScoreRecord,
    ScoreSubjectKind,
    SyncStream,
    TraceGap,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.store import CollectionStore
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.observability.langfuse.wire import RevisionMetadata


def collect(
    revision: HarnessRevision,
    *,
    window: TraceWindow,
    store_path: Path | None = None,
) -> CollectionResult:
    if revision.observability is None:
        raise CollectionError(
            CollectionErrorCode.OBSERVABILITY_NOT_CONNECTED,
            str(revision.id),
        )
    selected_store_path = store_path or revision.root / ".ofw" / "collection.sqlite"
    observation_sync_id = CollectionSyncId.for_collection(
        revision,
        window,
        SyncStream.OBSERVATIONS,
    )
    score_sync_id = CollectionSyncId.for_collection(revision, window, SyncStream.SCORES)
    store = CollectionStore(selected_store_path)
    try:
        observation_checkpoint = store.checkpoint(
            observation_sync_id,
            SyncStream.OBSERVATIONS,
        )
        score_checkpoint = store.checkpoint(score_sync_id, SyncStream.SCORES)
        project = LangfuseProject.from_manifest(revision.observability)
        client = LangfuseHttpClient(project)
        try:
            client.check_health()
            _sync_observations(
                client,
                store,
                str(revision.observability.id),
                observation_sync_id,
                window,
                (
                    observation_checkpoint.cursor
                    if observation_checkpoint is not None and not observation_checkpoint.complete
                    else None
                ),
            )
            _sync_scores(
                client,
                store,
                str(revision.observability.id),
                score_sync_id,
                window,
                (
                    score_checkpoint.cursor
                    if score_checkpoint is not None and not score_checkpoint.complete
                    else None
                ),
            )
        finally:
            client.close()
        observations = store.observations(observation_sync_id)
        scores = store.scores(score_sync_id)
        traces, orphan_count = _assemble_traces(observations, scores, revision.id)
        capability = _capability(traces)
        snapshot_digest = _snapshot_digest(observations, scores, traces)
        return CollectionResult(
            revision_id=revision.id,
            connection_id=revision.observability.id,
            window=window,
            observation_sync_id=observation_sync_id,
            score_sync_id=score_sync_id,
            traces=traces,
            observation_count=len(observations),
            score_count=len(scores),
            gap_count=orphan_count + sum(len(trace.gaps) for trace in traces),
            snapshot_digest=snapshot_digest,
            capability=capability,
            store_path=selected_store_path,
            content_policy=revision.observability.content_policy,
        )
    finally:
        store.close()


def _sync_observations(
    client: LangfuseHttpClient,
    store: CollectionStore,
    connection_id: str,
    sync_id: CollectionSyncId,
    window: TraceWindow,
    cursor: PageCursor | None,
) -> None:
    seen = set() if cursor is None else {cursor.value}
    current = cursor
    while True:
        page = client.get_observations(window, current)
        store.commit_observation_page(connection_id, sync_id, page)
        if page.cursor is None:
            return
        current = page.cursor
        _reject_cursor_loop(current, seen)


def _sync_scores(
    client: LangfuseHttpClient,
    store: CollectionStore,
    connection_id: str,
    sync_id: CollectionSyncId,
    window: TraceWindow,
    cursor: PageCursor | None,
) -> None:
    seen = set() if cursor is None else {cursor.value}
    current = cursor
    while True:
        page = client.get_scores(window, current)
        store.commit_score_page(connection_id, sync_id, page)
        if page.cursor is None:
            return
        current = page.cursor
        _reject_cursor_loop(current, seen)


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
        return AttributionLevel.MISSING
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
) -> Sha256Digest:
    payload = "\0".join(
        (
            *(str(record.digest) for record in observations),
            *(str(record.digest) for record in scores),
            *(str(record.digest) for record in traces),
        )
    )
    return Sha256Digest(f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}")


def search_observation_content(
    collection: CollectionResult,
    query: ObservationContentQuery,
) -> tuple[ObservationContentHit, ...]:
    _require_captured_content(collection)
    store = CollectionStore(collection.store_path)
    try:
        return store.search_content(collection.observation_sync_id, query)
    finally:
        store.close()


def read_trace_observations(
    collection: CollectionResult,
    trace_id: TraceId,
    limit: int,
) -> tuple[ObservationRecord, ...]:
    if not 1 <= limit <= 1000:
        raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, str(limit))
    store = CollectionStore(collection.store_path)
    try:
        return store.trace_observations(collection.observation_sync_id, trace_id, limit)
    finally:
        store.close()


def read_observation_content(
    collection: CollectionResult,
    reference: ObservationContentReference,
) -> ObservationContent:
    _require_captured_content(collection)
    store = CollectionStore(collection.store_path)
    try:
        return store.read_content(collection.observation_sync_id, reference)
    finally:
        store.close()


def _require_captured_content(collection: CollectionResult) -> None:
    if collection.content_policy.mode is ContentCaptureMode.METADATA_ONLY:
        raise CollectionError(
            CollectionErrorCode.CONTENT_NOT_CAPTURED,
            str(collection.observation_sync_id.value),
        )
