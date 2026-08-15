"""Boundary repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.boundary import BoundaryManifest, SystemBoundaryRecord
from openflywheel.contracts.ids import BoundaryId, WorkspaceId
from openflywheel.store.exceptions import StoreNotFoundError
from openflywheel.store.rows import BoundaryRow
from openflywheel.store.serde import model_from_json, model_to_json, tuple_from_json, tuple_to_json
from openflywheel.store.sqlite_access import (
    cell_int,
    cell_optional_str,
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class BoundaryRepository(Protocol):
    def upsert_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        name: str,
        slug: str,
        component_paths: tuple[str, ...],
        manifest: BoundaryManifest | None,
        created_at: datetime,
        boundary_id: BoundaryId | None = None,
    ) -> SystemBoundaryRecord: ...

    def list_boundaries(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[SystemBoundaryRecord, ...]: ...

    def get_by_slug(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId, slug: str
    ) -> SystemBoundaryRecord | None: ...

    def get_by_id(
        self, conn: sqlite3.Connection, boundary_id: BoundaryId
    ) -> SystemBoundaryRecord | None: ...

    def has_locked_boundary(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> bool: ...


def _row_to_boundary(row: BoundaryRow) -> SystemBoundaryRecord:
    manifest = model_from_json(BoundaryManifest, row.manifest_json) if row.manifest_json else None
    return SystemBoundaryRecord(
        id=BoundaryId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        name=row.name,
        slug=row.slug,
        component_paths=tuple_from_json(row.component_paths_json),
        manifest=manifest,
        created_at=datetime.fromisoformat(row.created_at),
    )


class SqliteBoundaryRepository:
    def upsert_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        name: str,
        slug: str,
        component_paths: tuple[str, ...],
        manifest: BoundaryManifest | None,
        created_at: datetime,
        boundary_id: BoundaryId | None = None,
    ) -> SystemBoundaryRecord:
        bid = boundary_id or BoundaryId(str(uuid4()))
        manifest_json = model_to_json(manifest) if manifest else None
        existing = conn.execute(
            "SELECT id FROM boundaries WHERE workspace_id = ? AND slug = ?",
            (str(workspace_id), slug),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE boundaries
                SET name = ?, component_paths_json = ?, manifest_json = ?
                WHERE workspace_id = ? AND slug = ?
                """,
                (
                    name,
                    tuple_to_json(component_paths),
                    manifest_json,
                    str(workspace_id),
                    slug,
                ),
            )
            bid = BoundaryId(str(existing["id"]))
        else:
            conn.execute(
                """
                INSERT INTO boundaries
                (id, workspace_id, name, slug, component_paths_json, manifest_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(bid),
                    str(workspace_id),
                    name,
                    slug,
                    tuple_to_json(component_paths),
                    manifest_json,
                    created_at.isoformat(),
                ),
            )
        row = fetch_one_row(conn, "SELECT * FROM boundaries WHERE id = ?", (str(bid),))
        if row is None:
            raise StoreNotFoundError(
                code="BOUNDARY_WRITE_VERIFY",
                message="Boundary row missing after upsert",
                root_cause_hint="Upsert succeeded but read-back failed",
                safe_retry=True,
                stop_condition="Inspect boundaries table integrity",
            )
        boundary_row = BoundaryRow(
            id=cell_str(row, "id"),
            workspace_id=cell_str(row, "workspace_id"),
            name=cell_str(row, "name"),
            slug=cell_str(row, "slug"),
            component_paths_json=cell_str(row, "component_paths_json"),
            manifest_json=cell_optional_str(row, "manifest_json"),
            created_at=cell_str(row, "created_at"),
        )
        return _row_to_boundary(boundary_row)

    def list_boundaries(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[SystemBoundaryRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM boundaries WHERE workspace_id = ? ORDER BY created_at",
            (str(workspace_id),),
        )
        result: list[SystemBoundaryRecord] = []
        for raw in rows:
            boundary_row = BoundaryRow(
                id=cell_str(raw, "id"),
                workspace_id=cell_str(raw, "workspace_id"),
                name=cell_str(raw, "name"),
                slug=cell_str(raw, "slug"),
                component_paths_json=cell_str(raw, "component_paths_json"),
                manifest_json=cell_optional_str(raw, "manifest_json"),
                created_at=cell_str(raw, "created_at"),
            )
            result.append(_row_to_boundary(boundary_row))
        return tuple(result)

    def get_by_slug(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId, slug: str
    ) -> SystemBoundaryRecord | None:
        raw = fetch_one_row(
            conn,
            "SELECT * FROM boundaries WHERE workspace_id = ? AND slug = ?",
            (str(workspace_id), slug),
        )
        if raw is None:
            return None
        boundary_row = BoundaryRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            name=cell_str(raw, "name"),
            slug=cell_str(raw, "slug"),
            component_paths_json=cell_str(raw, "component_paths_json"),
            manifest_json=cell_optional_str(raw, "manifest_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_boundary(boundary_row)

    def get_by_id(
        self, conn: sqlite3.Connection, boundary_id: BoundaryId
    ) -> SystemBoundaryRecord | None:
        raw = fetch_one_row(conn, "SELECT * FROM boundaries WHERE id = ?", (str(boundary_id),))
        if raw is None:
            return None
        boundary_row = BoundaryRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            name=cell_str(raw, "name"),
            slug=cell_str(raw, "slug"),
            component_paths_json=cell_str(raw, "component_paths_json"),
            manifest_json=cell_optional_str(raw, "manifest_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_boundary(boundary_row)

    def has_locked_boundary(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> bool:
        row = fetch_one_row(
            conn,
            """
            SELECT COUNT(*) AS cnt FROM boundaries
            WHERE workspace_id = ? AND manifest_json IS NOT NULL
            """,
            (str(workspace_id),),
        )
        if row is None:
            return False
        return cell_int(row, "cnt") > 0

    def locked_exclusions(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[str, ...]:
        boundaries = self.list_boundaries(conn, workspace_id)
        merged: set[str] = set()
        for boundary in boundaries:
            if boundary.manifest is not None:
                merged.update(boundary.manifest.exclusions)
        return tuple(sorted(merged))

    def locked_component_paths(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> frozenset[str]:
        boundaries = self.list_boundaries(conn, workspace_id)
        paths: set[str] = set()
        for boundary in boundaries:
            if boundary.manifest is not None:
                paths.update(boundary.component_paths)
        return frozenset(paths)
