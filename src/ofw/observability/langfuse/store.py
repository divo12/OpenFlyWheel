"""SQLite catalog for restart-safe Langfuse collection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from ofw.observability.langfuse.contracts import CollectionError, CollectionErrorCode
from ofw.observability.langfuse.domain import (
    CollectionSyncId,
    ObservationPage,
    ObservationRecord,
    PageCursor,
    ScorePage,
    ScoreRecord,
    SyncCheckpoint,
    SyncStream,
)

_OBSERVATION_ADAPTER: TypeAdapter[ObservationRecord] = TypeAdapter(ObservationRecord)
_SCORE_ADAPTER: TypeAdapter[ScoreRecord] = TypeAdapter(ScoreRecord)

_OBSERVATION_UPSERT = """
INSERT INTO langfuse_observations (
    connection_id, observation_id, trace_id, start_time, content_digest, record_json
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (connection_id, observation_id, content_digest) DO NOTHING
"""

_SCORE_UPSERT = """
INSERT INTO langfuse_scores (
    connection_id, score_id, timestamp, content_digest, record_json
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT (connection_id, score_id, content_digest) DO NOTHING
"""

_CHECKPOINT_UPSERT = """
INSERT INTO collection_checkpoints (
    sync_id, stream, cursor, complete
) VALUES (?, ?, ?, ?)
ON CONFLICT (sync_id, stream) DO UPDATE SET
    cursor = excluded.cursor,
    complete = excluded.complete,
    updated_at = CURRENT_TIMESTAMP
"""


class CollectionStore:
    def __init__(self, path: Path) -> None:
        connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            connection = sqlite3.connect(path)
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, str(path)) from error

    def close(self) -> None:
        self._connection.close()

    def schema_version(self) -> int:
        row = cast(tuple[int] | None, self._connection.execute("PRAGMA user_version").fetchone())
        if row is None:
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, "missing user_version")
        return row[0]

    def commit_observation_page(
        self,
        connection_id: str,
        sync_id: CollectionSyncId,
        page: ObservationPage,
    ) -> None:
        try:
            with self._connection:
                for record in page.records:
                    self._connection.execute(
                        _OBSERVATION_UPSERT,
                        (
                            connection_id,
                            record.id.value,
                            None if record.trace_id is None else record.trace_id.value,
                            record.start_time.isoformat(),
                            str(record.digest),
                            _OBSERVATION_ADAPTER.dump_json(record).decode(),
                        ),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO collection_observations (
                            sync_id, connection_id, observation_id, content_digest
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT (sync_id, observation_id) DO UPDATE SET
                            connection_id = excluded.connection_id,
                            content_digest = excluded.content_digest
                        """,
                        (sync_id.value, connection_id, record.id.value, str(record.digest)),
                    )
                self._advance_checkpoint(
                    sync_id,
                    SyncStream.OBSERVATIONS,
                    page.cursor,
                )
        except sqlite3.Error as error:
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, sync_id.value) from error

    def commit_score_page(
        self,
        connection_id: str,
        sync_id: CollectionSyncId,
        page: ScorePage,
    ) -> None:
        try:
            with self._connection:
                for record in page.records:
                    self._connection.execute(
                        _SCORE_UPSERT,
                        (
                            connection_id,
                            record.id.value,
                            record.timestamp.isoformat(),
                            str(record.digest),
                            _SCORE_ADAPTER.dump_json(record).decode(),
                        ),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO collection_scores (
                            sync_id, connection_id, score_id, content_digest
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT (sync_id, score_id) DO UPDATE SET
                            connection_id = excluded.connection_id,
                            content_digest = excluded.content_digest
                        """,
                        (sync_id.value, connection_id, record.id.value, str(record.digest)),
                    )
                self._advance_checkpoint(sync_id, SyncStream.SCORES, page.cursor)
        except sqlite3.Error as error:
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, sync_id.value) from error

    def checkpoint(
        self,
        sync_id: CollectionSyncId,
        stream: SyncStream,
    ) -> SyncCheckpoint | None:
        row = cast(
            tuple[str | None, int] | None,
            self._connection.execute(
                """
                SELECT cursor, complete
                FROM collection_checkpoints
                WHERE sync_id = ? AND stream = ?
                """,
                (sync_id.value, stream.value),
            ).fetchone(),
        )
        if row is None:
            return None
        cursor, complete = row
        return SyncCheckpoint(
            sync_id=sync_id,
            stream=stream,
            cursor=None if cursor is None else PageCursor(cursor),
            complete=bool(complete),
        )

    def observations(self, sync_id: CollectionSyncId) -> tuple[ObservationRecord, ...]:
        cursor = self._connection.execute(
            """
            SELECT observation.record_json
            FROM collection_observations AS membership
            JOIN langfuse_observations AS observation
             ON observation.connection_id = membership.connection_id
             AND observation.observation_id = membership.observation_id
             AND observation.content_digest = membership.content_digest
            WHERE membership.sync_id = ?
            ORDER BY observation.trace_id, observation.start_time, observation.observation_id
            """,
            (sync_id.value,),
        )
        rows = cast(Iterable[tuple[str]], cursor)
        return tuple(_OBSERVATION_ADAPTER.validate_json(row[0]) for row in rows)

    def scores(self, sync_id: CollectionSyncId) -> tuple[ScoreRecord, ...]:
        cursor = self._connection.execute(
            """
            SELECT score.record_json
            FROM collection_scores AS membership
            JOIN langfuse_scores AS score
             ON score.connection_id = membership.connection_id
             AND score.score_id = membership.score_id
             AND score.content_digest = membership.content_digest
            WHERE membership.sync_id = ?
            ORDER BY score.timestamp, score.score_id
            """,
            (sync_id.value,),
        )
        rows = cast(Iterable[tuple[str]], cursor)
        return tuple(_SCORE_ADAPTER.validate_json(row[0]) for row in rows)

    def _advance_checkpoint(
        self,
        sync_id: CollectionSyncId,
        stream: SyncStream,
        cursor: PageCursor | None,
    ) -> None:
        self._connection.execute(
            _CHECKPOINT_UPSERT,
            (
                sync_id.value,
                stream.value,
                None if cursor is None else cursor.value,
                int(cursor is None),
            ),
        )

    def _migrate(self) -> None:
        version = self.schema_version()
        if version == 1:
            return
        if version != 0:
            raise CollectionError(CollectionErrorCode.UNSUPPORTED_SCHEMA, str(version))
        migration = (
            files("ofw.observability.langfuse.migrations")
            .joinpath("001_collection.up.sql")
            .read_text(encoding="utf-8")
        )
        self._connection.executescript(migration)
