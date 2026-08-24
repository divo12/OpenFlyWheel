"""SQLite catalog for restart-safe Langfuse collection."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.contracts import CollectionError, CollectionErrorCode
from ofw.observability.langfuse.domain import (
    CollectionSyncId,
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
    ScorePage,
    ScoreRecord,
    SyncCheckpoint,
    SyncStream,
    TraceId,
)

_OBSERVATION_ADAPTER: TypeAdapter[ObservationRecord] = TypeAdapter(ObservationRecord)
_SCORE_ADAPTER: TypeAdapter[ScoreRecord] = TypeAdapter(ScoreRecord)

_OBSERVATION_UPSERT = """
INSERT INTO langfuse_observations (
    connection_id, observation_id, trace_id, start_time,
    input_content_digest, output_content_digest, content_digest, record_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            self._path = path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            connection = sqlite3.connect(path)
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
            self._restrict_sidecar_permissions()
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, str(path)) from error

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._restrict_sidecar_permissions()
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
                for content in page.contents:
                    self._commit_content(content)
                for record in page.records:
                    self._connection.execute(
                        _OBSERVATION_UPSERT,
                        (
                            connection_id,
                            record.id.value,
                            None if record.trace_id is None else record.trace_id.value,
                            record.start_time.isoformat(),
                            (
                                None
                                if record.input_content is None
                                else str(record.input_content.digest)
                            ),
                            (
                                None
                                if record.output_content is None
                                else str(record.output_content.digest)
                            ),
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

    def replace_observation_membership(
        self,
        sync_id: CollectionSyncId,
        refresh_sync_id: CollectionSyncId,
    ) -> None:
        self._replace_membership(
            "collection_observations",
            SyncStream.OBSERVATIONS,
            sync_id,
            refresh_sync_id,
        )

    def replace_score_membership(
        self,
        sync_id: CollectionSyncId,
        refresh_sync_id: CollectionSyncId,
    ) -> None:
        self._replace_membership(
            "collection_scores",
            SyncStream.SCORES,
            sync_id,
            refresh_sync_id,
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

    def trace_observations(
        self,
        sync_id: CollectionSyncId,
        trace_id: TraceId,
        limit: int,
    ) -> tuple[ObservationRecord, ...]:
        if not 1 <= limit <= 1000:
            raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, str(limit))
        cursor = self._connection.execute(
            """
            SELECT observation.record_json
            FROM collection_observations AS membership
            JOIN langfuse_observations AS observation
             ON observation.connection_id = membership.connection_id
             AND observation.observation_id = membership.observation_id
             AND observation.content_digest = membership.content_digest
            WHERE membership.sync_id = ? AND observation.trace_id = ?
            ORDER BY observation.start_time, observation.observation_id
            LIMIT ?
            """,
            (sync_id.value, trace_id.value, limit),
        )
        rows = cast(Iterable[tuple[str]], cursor)
        return tuple(_OBSERVATION_ADAPTER.validate_json(row[0]) for row in rows)

    def read_content(
        self,
        sync_id: CollectionSyncId,
        reference: ObservationContentReference,
    ) -> ObservationContent:
        row = cast(
            tuple[str, int] | None,
            self._connection.execute(
                """
                SELECT content.content_text, content.byte_count
                FROM observation_content AS content
                WHERE content.content_digest = ?
                  AND EXISTS (
                    SELECT 1
                    FROM collection_observations AS membership
                    JOIN langfuse_observations AS observation
                     ON observation.connection_id = membership.connection_id
                     AND observation.observation_id = membership.observation_id
                     AND observation.content_digest = membership.content_digest
                    WHERE membership.sync_id = ?
                      AND (
                        observation.input_content_digest = content.content_digest
                        OR observation.output_content_digest = content.content_digest
                      )
                  )
                """,
                (str(reference.digest), sync_id.value),
            ).fetchone(),
        )
        if row is None:
            raise CollectionError(
                CollectionErrorCode.CONTENT_NOT_CAPTURED,
                str(reference.digest),
            )
        text, byte_count = row
        stored_reference = ObservationContentReference(
            reference.digest,
            byte_count,
        )
        if stored_reference != reference:
            raise CollectionError(
                CollectionErrorCode.DATABASE_ERROR,
                str(reference.digest),
            )
        return ObservationContent(stored_reference, text)

    def search_content(
        self,
        sync_id: CollectionSyncId,
        query: ObservationContentQuery,
    ) -> tuple[ObservationContentHit, ...]:
        match_clause = (
            "content.content_text = ?"
            if query.match is ObservationContentMatch.EXACT
            else (
                "content.content_digest IN ("
                "SELECT content_digest FROM observation_content_fts "
                "WHERE content_text MATCH ?"
                ")"
            )
        )
        match_value = query.text if query.match is ObservationContentMatch.EXACT else _fts_phrase(
            query.text
        )
        cursor = self._connection.execute(
            f"""
            WITH reference AS (
                SELECT observation.connection_id, observation.observation_id,
                       observation.trace_id, 'input' AS field,
                       observation.input_content_digest AS content_digest
                FROM collection_observations AS membership
                JOIN langfuse_observations AS observation
                 ON observation.connection_id = membership.connection_id
                 AND observation.observation_id = membership.observation_id
                 AND observation.content_digest = membership.content_digest
                WHERE membership.sync_id = ?
                UNION ALL
                SELECT observation.connection_id, observation.observation_id,
                       observation.trace_id, 'output' AS field,
                       observation.output_content_digest AS content_digest
                FROM collection_observations AS membership
                JOIN langfuse_observations AS observation
                 ON observation.connection_id = membership.connection_id
                 AND observation.observation_id = membership.observation_id
                 AND observation.content_digest = membership.content_digest
                WHERE membership.sync_id = ?
            )
            SELECT reference.observation_id, reference.trace_id, reference.field,
                   content.content_digest, content.byte_count,
                   substr(content.content_text, 1, ?)
            FROM reference
            JOIN observation_content AS content
              ON content.content_digest = reference.content_digest
            WHERE reference.content_digest IS NOT NULL
              AND (? = 'any' OR reference.field = ?)
              AND (? IS NULL OR reference.trace_id = ?)
              AND {match_clause}
            ORDER BY reference.observation_id, reference.field
            LIMIT ?
            """,  # nosec B608
            (
                sync_id.value,
                sync_id.value,
                query.maximum_excerpt_characters,
                query.field.value,
                query.field.value,
                None if query.trace_id is None else query.trace_id.value,
                None if query.trace_id is None else query.trace_id.value,
                match_value,
                query.limit,
            ),
        )
        rows = cast(Iterable[tuple[str, str | None, str, str, int, str]], cursor)
        return tuple(_content_hit(row) for row in rows)

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

    def _replace_membership(
        self,
        table: str,
        stream: SyncStream,
        sync_id: CollectionSyncId,
        refresh_sync_id: CollectionSyncId,
    ) -> None:
        try:
            with self._connection:
                self._connection.execute(
                    f"DELETE FROM {table} WHERE sync_id = ?",  # nosec B608
                    (sync_id.value,),
                )
                self._connection.execute(
                    f"UPDATE {table} SET sync_id = ? WHERE sync_id = ?",  # nosec B608
                    (sync_id.value, refresh_sync_id.value),
                )
                self._connection.execute(
                    "DELETE FROM collection_checkpoints WHERE sync_id = ? AND stream = ?",
                    (refresh_sync_id.value, stream.value),
                )
        except sqlite3.Error as error:
            raise CollectionError(CollectionErrorCode.DATABASE_ERROR, sync_id.value) from error

    def _commit_content(self, content: ObservationContent) -> None:
        inserted = self._connection.execute(
            """
            INSERT INTO observation_content (
                content_digest, content_text, byte_count
            ) VALUES (?, ?, ?)
            ON CONFLICT (content_digest) DO NOTHING
            """,
            (
                str(content.reference.digest),
                content.text,
                content.reference.byte_count,
            ),
        )
        if inserted.rowcount == 1:
            self._connection.execute(
                """
                INSERT INTO observation_content_fts (content_digest, content_text)
                VALUES (?, ?)
                """,
                (str(content.reference.digest), content.text),
            )

    def _restrict_sidecar_permissions(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = self._path.with_name(f"{self._path.name}{suffix}")
            try:
                sidecar.chmod(0o600)
            except FileNotFoundError:
                continue

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


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _content_hit(
    row: tuple[str, str | None, str, str, int, str],
) -> ObservationContentHit:
    observation_id, trace_id, field, digest, byte_count, excerpt = row
    return ObservationContentHit(
        ObservationId(observation_id),
        None if trace_id is None else TraceId(trace_id),
        ObservationContentField(field),
        ObservationContentReference(
            Sha256Digest(digest),
            byte_count,
        ),
        excerpt,
    )
