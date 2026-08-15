"""Episode and evidence anchor repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import LocatorKind
from openflywheel.contracts.episode import EpisodeRecord, SourceReference
from openflywheel.contracts.evidence import EvidenceAnchorRecord, EvidenceLocator
from openflywheel.contracts.ids import EpisodeId, EvidenceAnchorId, SourceId, WorkspaceId
from openflywheel.store.rows import EpisodeRow, EvidenceAnchorRow
from openflywheel.store.serde import model_from_json, model_to_json
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class EpisodeRepository(Protocol):
    def find_idempotent(
        self,
        conn: sqlite3.Connection,
        source_id: SourceId,
        external_id: str,
        checksum: str,
    ) -> EpisodeRecord | None: ...

    def get_episode(
        self, conn: sqlite3.Connection, episode_id: EpisodeId
    ) -> EpisodeRecord | None: ...

    def find_latest_for_external(
        self, conn: sqlite3.Connection, source_id: SourceId, external_id: str
    ) -> EpisodeRecord | None: ...

    def insert_episode(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        source_ref: SourceReference,
        content_text: str,
        acl: AclLabel,
        event_time: datetime,
        ingest_time: datetime,
        checksum: str,
        content_type: str,
        episode_id: EpisodeId | None = None,
    ) -> EpisodeRecord: ...

    def insert_anchor(
        self,
        conn: sqlite3.Connection,
        *,
        episode_id: EpisodeId,
        locator: EvidenceLocator,
        label: str,
        anchor_id: EvidenceAnchorId | None = None,
    ) -> EvidenceAnchorRecord: ...

    def list_episodes_for_source(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> tuple[EpisodeRecord, ...]: ...


def _row_to_episode(row: EpisodeRow) -> EpisodeRecord:
    return EpisodeRecord(
        id=EpisodeId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        source_ref=SourceReference(
            source_id=SourceId(row.source_id),
            external_id=row.external_id,
            uri=row.uri,
        ),
        content_text=row.content_text,
        acl=model_from_json(AclLabel, row.acl_json),
        event_time=datetime.fromisoformat(row.event_time),
        ingest_time=datetime.fromisoformat(row.ingest_time),
        checksum=row.checksum,
        content_type=row.content_type,
    )


def _row_to_anchor(row: EvidenceAnchorRow) -> EvidenceAnchorRecord:
    return EvidenceAnchorRecord(
        id=EvidenceAnchorId(row.id),
        episode_id=EpisodeId(row.episode_id),
        locator=EvidenceLocator(kind=LocatorKind(row.locator_kind), value=row.locator_value),
        label=row.label,
    )


class SqliteEpisodeRepository:
    def find_idempotent(
        self,
        conn: sqlite3.Connection,
        source_id: SourceId,
        external_id: str,
        checksum: str,
    ) -> EpisodeRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM episodes
            WHERE source_id = ? AND external_id = ? AND checksum = ?
            """,
            (str(source_id), external_id, checksum),
        )
        if raw is None:
            return None
        return self._raw_to_episode(raw)

    def get_episode(self, conn: sqlite3.Connection, episode_id: EpisodeId) -> EpisodeRecord | None:
        raw = fetch_one_row(
            conn,
            "SELECT * FROM episodes WHERE id = ?",
            (str(episode_id),),
        )
        if raw is None:
            return None
        return self._raw_to_episode(raw)

    def find_latest_for_external(
        self, conn: sqlite3.Connection, source_id: SourceId, external_id: str
    ) -> EpisodeRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM episodes
            WHERE source_id = ? AND external_id = ?
            ORDER BY ingest_time DESC
            LIMIT 1
            """,
            (str(source_id), external_id),
        )
        if raw is None:
            return None
        return self._raw_to_episode(raw)

    def _raw_to_episode(self, raw: sqlite3.Row) -> EpisodeRecord:
        row = EpisodeRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            source_id=cell_str(raw, "source_id"),
            external_id=cell_str(raw, "external_id"),
            uri=cell_str(raw, "uri"),
            content_text=cell_str(raw, "content_text"),
            acl_json=cell_str(raw, "acl_json"),
            event_time=cell_str(raw, "event_time"),
            ingest_time=cell_str(raw, "ingest_time"),
            checksum=cell_str(raw, "checksum"),
            content_type=cell_str(raw, "content_type"),
        )
        return _row_to_episode(row)

    def find_by_checksum(
        self, conn: sqlite3.Connection, source_id: SourceId, checksum: str
    ) -> EpisodeRecord | None:
        raw = fetch_one_row(
            conn,
            "SELECT * FROM episodes WHERE source_id = ? AND checksum = ?",
            (str(source_id), checksum),
        )
        if raw is None:
            return None
        return self._raw_to_episode(raw)

    def insert_episode(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        source_ref: SourceReference,
        content_text: str,
        acl: AclLabel,
        event_time: datetime,
        ingest_time: datetime,
        checksum: str,
        content_type: str,
        episode_id: EpisodeId | None = None,
    ) -> EpisodeRecord:
        eid = episode_id or EpisodeId(str(uuid4()))
        row = EpisodeRow(
            id=str(eid),
            workspace_id=str(workspace_id),
            source_id=str(source_ref.source_id),
            external_id=source_ref.external_id,
            uri=source_ref.uri,
            content_text=content_text,
            acl_json=model_to_json(acl),
            event_time=event_time.isoformat(),
            ingest_time=ingest_time.isoformat(),
            checksum=checksum,
            content_type=content_type,
        )
        conn.execute(
            """
            INSERT INTO episodes
            (id, workspace_id, source_id, external_id, uri, content_text, acl_json,
             event_time, ingest_time, checksum, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.workspace_id,
                row.source_id,
                row.external_id,
                row.uri,
                row.content_text,
                row.acl_json,
                row.event_time,
                row.ingest_time,
                row.checksum,
                row.content_type,
            ),
        )
        return _row_to_episode(row)

    def insert_anchor(
        self,
        conn: sqlite3.Connection,
        *,
        episode_id: EpisodeId,
        locator: EvidenceLocator,
        label: str,
        anchor_id: EvidenceAnchorId | None = None,
    ) -> EvidenceAnchorRecord:
        aid = anchor_id or EvidenceAnchorId(str(uuid4()))
        row = EvidenceAnchorRow(
            id=str(aid),
            episode_id=str(episode_id),
            locator_kind=locator.kind.value,
            locator_value=locator.value,
            label=label,
        )
        conn.execute(
            """
            INSERT INTO evidence_anchors (id, episode_id, locator_kind, locator_value, label)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row.id, row.episode_id, row.locator_kind, row.locator_value, row.label),
        )
        return _row_to_anchor(row)

    def list_episodes_for_source(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> tuple[EpisodeRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM episodes WHERE source_id = ? ORDER BY ingest_time",
            (str(source_id),),
        )
        result: list[EpisodeRecord] = []
        for raw in rows:
            row = EpisodeRow(
                id=cell_str(raw, "id"),
                workspace_id=cell_str(raw, "workspace_id"),
                source_id=cell_str(raw, "source_id"),
                external_id=cell_str(raw, "external_id"),
                uri=cell_str(raw, "uri"),
                content_text=cell_str(raw, "content_text"),
                acl_json=cell_str(raw, "acl_json"),
                event_time=cell_str(raw, "event_time"),
                ingest_time=cell_str(raw, "ingest_time"),
                checksum=cell_str(raw, "checksum"),
                content_type=cell_str(raw, "content_type"),
            )
            result.append(_row_to_episode(row))
        return tuple(result)

    def list_anchors_for_episode(
        self, conn: sqlite3.Connection, episode_id: EpisodeId
    ) -> tuple[EvidenceAnchorRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM evidence_anchors WHERE episode_id = ?",
            (str(episode_id),),
        )
        result: list[EvidenceAnchorRecord] = []
        for raw in rows:
            row = EvidenceAnchorRow(
                id=cell_str(raw, "id"),
                episode_id=cell_str(raw, "episode_id"),
                locator_kind=cell_str(raw, "locator_kind"),
                locator_value=cell_str(raw, "locator_value"),
                label=cell_str(raw, "label"),
            )
            result.append(_row_to_anchor(row))
        return tuple(result)
