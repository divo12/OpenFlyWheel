"""Unit of Work for atomic episode + anchor + checkpoint writes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.episode import EpisodeRecord, SourceReference
from openflywheel.contracts.evidence import EvidenceAnchorRecord, EvidenceLocator
from openflywheel.contracts.ids import EpisodeId, SourceId, WorkspaceId
from openflywheel.store.checkpoint_hook import CheckpointCommitHook, NoOpCheckpointCommitHook
from openflywheel.store.db import Database
from openflywheel.store.repos.checkpoint_repo import SqliteCheckpointRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.sqlite_access import cell_int, fetch_one_row


@dataclass(frozen=True)
class EpisodeWriteBundle:
    episode: EpisodeRecord
    anchors: tuple[EvidenceAnchorRecord, ...]
    checkpoint_cursor: str


class IngestUnitOfWork:
    def __init__(
        self,
        database: Database,
        *,
        checkpoint_hook: CheckpointCommitHook | None = None,
    ) -> None:
        self._database = database
        self._episodes = SqliteEpisodeRepository()
        self._checkpoints = SqliteCheckpointRepository()
        self._checkpoint_hook = checkpoint_hook or NoOpCheckpointCommitHook()

    @property
    def checkpoint_hook(self) -> CheckpointCommitHook:
        return self._checkpoint_hook

    def commit_episode_bundle(
        self,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        source_ref: SourceReference,
        content_text: str,
        acl: AclLabel,
        event_time: datetime,
        ingest_time: datetime,
        checksum: str,
        content_type: str,
        anchors: tuple[tuple[EvidenceLocator, str], ...],
        checkpoint_cursor: str,
        episode_id: EpisodeId | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> EpisodeWriteBundle:
        if conn is not None:
            return self._commit_episode_bundle_on_conn(
                conn,
                workspace_id=workspace_id,
                source_id=source_id,
                source_ref=source_ref,
                content_text=content_text,
                acl=acl,
                event_time=event_time,
                ingest_time=ingest_time,
                checksum=checksum,
                content_type=content_type,
                anchors=anchors,
                checkpoint_cursor=checkpoint_cursor,
                episode_id=episode_id,
            )
        with self._database.write() as owned:
            return self._commit_episode_bundle_on_conn(
                owned,
                workspace_id=workspace_id,
                source_id=source_id,
                source_ref=source_ref,
                content_text=content_text,
                acl=acl,
                event_time=event_time,
                ingest_time=ingest_time,
                checksum=checksum,
                content_type=content_type,
                anchors=anchors,
                checkpoint_cursor=checkpoint_cursor,
                episode_id=episode_id,
            )

    def _commit_episode_bundle_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        source_ref: SourceReference,
        content_text: str,
        acl: AclLabel,
        event_time: datetime,
        ingest_time: datetime,
        checksum: str,
        content_type: str,
        anchors: tuple[tuple[EvidenceLocator, str], ...],
        checkpoint_cursor: str,
        episode_id: EpisodeId | None = None,
    ) -> EpisodeWriteBundle:
        episode = self._episodes.insert_episode(
            conn,
            workspace_id=workspace_id,
            source_ref=source_ref,
            content_text=content_text,
            acl=acl,
            event_time=event_time,
            ingest_time=ingest_time,
            checksum=checksum,
            content_type=content_type,
            episode_id=episode_id,
        )
        written_anchors: list[EvidenceAnchorRecord] = []
        for locator, label in anchors:
            anchor = self._episodes.insert_anchor(
                conn,
                episode_id=episode.id,
                locator=locator,
                label=label,
            )
            written_anchors.append(anchor)
        self._checkpoint_hook.before_checkpoint_commit(
            source_id=source_id,
            cursor_value=checkpoint_cursor,
        )
        self._checkpoints.upsert_checkpoint(
            conn,
            source_id=source_id,
            cursor_value=checkpoint_cursor,
            updated_at=ingest_time,
        )
        return EpisodeWriteBundle(
            episode=episode,
            anchors=tuple(written_anchors),
            checkpoint_cursor=checkpoint_cursor,
        )

    def commit_reject_cursor(
        self,
        *,
        source_id: SourceId,
        checkpoint_cursor: str,
        updated_at: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is not None:
            self._commit_reject_cursor_on_conn(
                conn,
                source_id=source_id,
                checkpoint_cursor=checkpoint_cursor,
                updated_at=updated_at,
            )
            return
        with self._database.write() as owned:
            self._commit_reject_cursor_on_conn(
                owned,
                source_id=source_id,
                checkpoint_cursor=checkpoint_cursor,
                updated_at=updated_at,
            )

    def _commit_reject_cursor_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: SourceId,
        checkpoint_cursor: str,
        updated_at: datetime,
    ) -> None:
        self._checkpoint_hook.before_checkpoint_commit(
            source_id=source_id,
            cursor_value=checkpoint_cursor,
        )
        self._checkpoints.upsert_checkpoint(
            conn,
            source_id=source_id,
            cursor_value=checkpoint_cursor,
            updated_at=updated_at,
        )

    def read_checkpoint(self, source_id: SourceId) -> str | None:
        with self._database.read() as conn:
            checkpoint = self._checkpoints.get_checkpoint(conn, source_id)
            return checkpoint.cursor_value if checkpoint else None

    def count_episodes(self, source_id: SourceId) -> int:
        with self._database.read() as conn:
            row = fetch_one_row(
                conn,
                "SELECT COUNT(*) AS cnt FROM episodes WHERE source_id = ?",
                (str(source_id),),
            )
            if row is None:
                return 0
            return cell_int(row, "cnt")

    def count_anchors_for_source(self, source_id: SourceId) -> int:
        with self._database.read() as conn:
            row = fetch_one_row(
                conn,
                """
                SELECT COUNT(*) AS cnt FROM evidence_anchors ea
                JOIN episodes e ON e.id = ea.episode_id
                WHERE e.source_id = ?
                """,
                (str(source_id),),
            )
            if row is None:
                return 0
            return cell_int(row, "cnt")
