"""Source repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import SourceKind
from openflywheel.contracts.ids import SourceId, WorkspaceId
from openflywheel.contracts.source import ConnectorCapabilityReport, SourceRecord
from openflywheel.store.rows import SourceRow
from openflywheel.store.serde import model_from_json, model_to_json
from openflywheel.store.sqlite_access import (
    cell_optional_str,
    cell_str,
    fetch_one_row,
)


class SourceRepository(Protocol):
    def upsert_source(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: SourceKind,
        slug: str,
        display_name: str,
        capability: ConnectorCapabilityReport,
        root_path: str | None,
        created_at: datetime,
    ) -> SourceRecord: ...

    def get_by_slug(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId, slug: str
    ) -> SourceRecord | None: ...

    def get_by_id(self, conn: sqlite3.Connection, source_id: SourceId) -> SourceRecord | None: ...


def _row_to_source(row: SourceRow) -> SourceRecord:
    return SourceRecord(
        id=SourceId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        kind=SourceKind(row.kind),
        slug=row.slug,
        display_name=row.display_name,
        capability=model_from_json(ConnectorCapabilityReport, row.capability_json),
        root_path=row.root_path,
        created_at=datetime.fromisoformat(row.created_at),
    )


class SqliteSourceRepository:
    def upsert_source(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: SourceKind,
        slug: str,
        display_name: str,
        capability: ConnectorCapabilityReport,
        root_path: str | None,
        created_at: datetime,
    ) -> SourceRecord:
        existing = conn.execute(
            "SELECT id FROM sources WHERE workspace_id = ? AND slug = ?",
            (str(workspace_id), slug),
        ).fetchone()
        source_id = SourceId(str(existing["id"])) if existing else SourceId(str(uuid4()))
        row = SourceRow(
            id=str(source_id),
            workspace_id=str(workspace_id),
            kind=kind.value,
            slug=slug,
            display_name=display_name,
            capability_json=model_to_json(capability),
            root_path=root_path,
            created_at=created_at.isoformat(),
        )
        if existing is not None:
            conn.execute(
                """
                UPDATE sources
                SET kind = ?, display_name = ?, capability_json = ?, root_path = ?
                WHERE id = ?
                """,
                (row.kind, row.display_name, row.capability_json, row.root_path, row.id),
            )
        else:
            conn.execute(
                """
                INSERT INTO sources
                (id, workspace_id, kind, slug, display_name, capability_json, root_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.id,
                    row.workspace_id,
                    row.kind,
                    row.slug,
                    row.display_name,
                    row.capability_json,
                    row.root_path,
                    row.created_at,
                ),
            )
        return _row_to_source(row)

    def get_by_slug(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId, slug: str
    ) -> SourceRecord | None:
        raw = fetch_one_row(
            conn,
            "SELECT * FROM sources WHERE workspace_id = ? AND slug = ?",
            (str(workspace_id), slug),
        )
        if raw is None:
            return None
        row = SourceRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            kind=cell_str(raw, "kind"),
            slug=cell_str(raw, "slug"),
            display_name=cell_str(raw, "display_name"),
            capability_json=cell_str(raw, "capability_json"),
            root_path=cell_optional_str(raw, "root_path"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_source(row)

    def get_by_id(self, conn: sqlite3.Connection, source_id: SourceId) -> SourceRecord | None:
        raw = fetch_one_row(
            conn,
            "SELECT * FROM sources WHERE id = ?",
            (str(source_id),),
        )
        if raw is None:
            return None
        row = SourceRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            kind=cell_str(raw, "kind"),
            slug=cell_str(raw, "slug"),
            display_name=cell_str(raw, "display_name"),
            capability_json=cell_str(raw, "capability_json"),
            root_path=cell_optional_str(raw, "root_path"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_source(row)
