"""SQLite collection catalog and checkpoint behavior."""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ofw import Sha256Digest
from ofw.observability.langfuse.domain import (
    CollectionSyncId,
    JsonDocument,
    ObservationContent,
    ObservationContentField,
    ObservationContentHit,
    ObservationContentMatch,
    ObservationContentQuery,
    ObservationContentReference,
    ObservationId,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    PageCursor,
    ProjectId,
    ScoreDataType,
    ScoreId,
    ScorePage,
    ScoreRecord,
    ScoreSource,
    ScoreSubject,
    ScoreSubjectKind,
    SyncStream,
    TraceId,
)
from ofw.observability.langfuse.store import CollectionStore


def _observation(observation_id: str, digest_suffix: str = "one") -> ObservationRecord:
    return ObservationRecord(
        id=ObservationId(observation_id),
        trace_id=TraceId("trace-1"),
        start_time=datetime(2026, 8, 22, 0, 0, tzinfo=UTC),
        end_time=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        project_id=ProjectId("project-1"),
        parent_observation_id=None,
        type=ObservationType.AGENT,
        is_root=True,
        name="agent-run",
        level=None,
        version="v1",
        environment="production",
        user_id="user-1",
        session_id="session-1",
        created_at=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, 0, 2, tzinfo=UTC),
        metadata=JsonDocument('{"source":"fixture"}'),
        usage=JsonDocument('{"input":1}'),
        costs=JsonDocument('{"total":0.1}'),
        total_cost=0.1,
        tags=("production",),
        release="release-1",
        trace_name="employee-run",
        raw=JsonDocument('{"id":"' + observation_id + '"}'),
        digest=Sha256Digest(f"sha256:{digest_suffix}"),
    )


def _score() -> ScoreRecord:
    return ScoreRecord(
        id=ScoreId("score-1"),
        project_id=ProjectId("project-1"),
        name="correctness",
        value=True,
        data_type=ScoreDataType.BOOLEAN,
        source=ScoreSource.ANNOTATION,
        timestamp=datetime(2026, 8, 22, 0, 3, tzinfo=UTC),
        environment="production",
        created_at=datetime(2026, 8, 22, 0, 3, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, 0, 4, tzinfo=UTC),
        comment="reviewed",
        metadata=None,
        subject=ScoreSubject(ScoreSubjectKind.TRACE, "trace-1", None),
        raw=JsonDocument('{"id":"score-1"}'),
        digest=Sha256Digest("sha256:score"),
    )


def test_migrates_catalog_and_round_trips_atomic_pages(tmp_path: Path) -> None:
    path = tmp_path / "collection.sqlite"
    sync_id = CollectionSyncId("sync-1")
    store = CollectionStore(path)
    try:
        assert store.schema_version() == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        store.commit_observation_page(
            connection_id="connection-1",
            sync_id=sync_id,
            page=ObservationPage((_observation("obs-1"),), PageCursor("next-page")),
        )
        checkpoint = store.checkpoint(sync_id, SyncStream.OBSERVATIONS)
        assert checkpoint is not None
        assert checkpoint.cursor == PageCursor("next-page")
        assert not checkpoint.complete
        assert store.observations(sync_id) == (_observation("obs-1"),)
    finally:
        store.close()

    reopened = CollectionStore(path)
    try:
        checkpoint = reopened.checkpoint(sync_id, SyncStream.OBSERVATIONS)
        assert checkpoint is not None
        assert checkpoint.cursor == PageCursor("next-page")
        reopened.commit_observation_page(
            connection_id="connection-1",
            sync_id=sync_id,
            page=ObservationPage((_observation("obs-2"),), None),
        )
        completed = reopened.checkpoint(sync_id, SyncStream.OBSERVATIONS)
        assert completed is not None
        assert completed.complete
        assert completed.cursor is None
        assert tuple(record.id.value for record in reopened.observations(sync_id)) == (
            "obs-1",
            "obs-2",
        )
    finally:
        reopened.close()


def test_repeated_observation_and_score_pages_are_idempotent(tmp_path: Path) -> None:
    store = CollectionStore(tmp_path / "collection.sqlite")
    observation_sync = CollectionSyncId("sync-observations")
    score_sync = CollectionSyncId("sync-scores")
    observation_page = ObservationPage((_observation("obs-1"),), None)
    score_page = ScorePage((_score(),), None)
    try:
        store.commit_observation_page("connection-1", observation_sync, observation_page)
        store.commit_observation_page("connection-1", observation_sync, observation_page)
        store.commit_score_page("connection-1", score_sync, score_page)
        store.commit_score_page("connection-1", score_sync, score_page)

        assert store.observations(observation_sync) == (_observation("obs-1"),)
        assert store.scores(score_sync) == (_score(),)
        observation_checkpoint = store.checkpoint(
            observation_sync,
            SyncStream.OBSERVATIONS,
        )
        score_checkpoint = store.checkpoint(score_sync, SyncStream.SCORES)
        assert observation_checkpoint is not None
        assert score_checkpoint is not None
    finally:
        store.close()


def test_source_update_replaces_record_without_duplicate_membership(tmp_path: Path) -> None:
    store = CollectionStore(tmp_path / "collection.sqlite")
    sync_id = CollectionSyncId("sync-1")
    try:
        store.commit_observation_page(
            "connection-1",
            sync_id,
            ObservationPage((_observation("obs-1", "old"),), None),
        )
        store.commit_observation_page(
            "connection-1",
            sync_id,
            ObservationPage((_observation("obs-1", "new"),), None),
        )

        records = store.observations(sync_id)
        assert len(records) == 1
        assert records[0].digest == Sha256Digest("sha256:new")
    finally:
        store.close()


def test_wal_sidecars_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "collection.sqlite"
    store = CollectionStore(path)
    try:
        store.commit_observation_page(
            "connection-1",
            CollectionSyncId("sync-sidecars"),
            ObservationPage((_observation("obs-1"),), None),
        )
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(f"{path.name}{suffix}")
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        store.close()


def test_source_update_does_not_mutate_prior_sync_snapshot(tmp_path: Path) -> None:
    store = CollectionStore(tmp_path / "collection.sqlite")
    old_sync = CollectionSyncId("sync-old")
    new_sync = CollectionSyncId("sync-new")
    try:
        store.commit_observation_page(
            "connection-1",
            old_sync,
            ObservationPage((_observation("obs-1", "old"),), None),
        )
        store.commit_observation_page(
            "connection-1",
            new_sync,
            ObservationPage((_observation("obs-1", "new"),), None),
        )

        assert store.observations(old_sync)[0].digest == Sha256Digest("sha256:old")
        assert store.observations(new_sync)[0].digest == Sha256Digest("sha256:new")
    finally:
        store.close()


def test_content_is_addressed_once_and_supports_bounded_exact_and_phrase_search(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collection.sqlite"
    store = CollectionStore(path)
    sync_id = CollectionSyncId("sync-content")
    input_text = "Refund failed for dev@example.com"
    output_text = "Escalation opened"
    input_reference = ObservationContentReference.for_text(input_text)
    output_reference = ObservationContentReference.for_text(output_text)
    input_content = ObservationContent(input_reference, input_text)
    output_content = ObservationContent(output_reference, output_text)
    observation = replace(
        _observation("obs-content"),
        input_content=input_reference,
        output_content=output_reference,
    )
    page = ObservationPage(
        (observation,),
        None,
        (input_content, output_content),
    )
    try:
        store.commit_observation_page("connection-1", sync_id, page)
        store.commit_observation_page("connection-1", sync_id, page)

        assert store.read_content(sync_id, input_reference) == input_content
        exact = store.search_content(
            sync_id,
            ObservationContentQuery(
                "Escalation opened",
                ObservationContentMatch.EXACT,
                ObservationContentField.OUTPUT,
                None,
                10,
                100,
            ),
        )
        phrase = store.search_content(
            sync_id,
            ObservationContentQuery(
                "refund failed",
                ObservationContentMatch.TOKEN_PHRASE,
                ObservationContentField.ANY,
                TraceId("trace-1"),
                10,
                100,
            ),
        )
        trajectory = store.trace_observations(sync_id, TraceId("trace-1"), 10)
    finally:
        store.close()

    assert exact == (
        ObservationContentHit(
            ObservationId("obs-content"),
            TraceId("trace-1"),
            ObservationContentField.OUTPUT,
            output_reference,
            "Escalation opened",
        ),
    )
    assert phrase[0].field is ObservationContentField.INPUT
    assert phrase[0].reference == input_reference
    assert trajectory == (observation,)
    connection = sqlite3.connect(path)
    try:
        record_json = cast(
            tuple[str] | None,
            connection.execute("SELECT record_json FROM langfuse_observations").fetchone(),
        )
        content_count = cast(
            tuple[int] | None,
            connection.execute("SELECT COUNT(*) FROM observation_content").fetchone(),
        )
        search_count = cast(
            tuple[int] | None,
            connection.execute("SELECT COUNT(*) FROM observation_content_fts").fetchone(),
        )
    finally:
        connection.close()
    assert record_json is not None
    assert content_count is not None
    assert search_count is not None
    assert input_text not in record_json[0]
    assert content_count == (2,)
    assert search_count == (2,)
