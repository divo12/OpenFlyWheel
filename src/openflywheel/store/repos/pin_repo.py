"""Pin snapshot repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.ids import BoundaryId, ClaimId, ManifestVersion, PinId, WorkspaceId
from openflywheel.contracts.pin import PinRecord
from openflywheel.store.rows import PinRow
from openflywheel.store.serde import tuple_from_json, tuple_to_json
from openflywheel.store.sqlite_access import (
    cell_int,
    cell_str,
    fetch_one_row,
)


class PinRepository(Protocol):
    def insert_pin(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        manifest_version: ManifestVersion,
        claim_ids: tuple[ClaimId, ...],
        created_at: datetime,
        pin_id: PinId | None = None,
    ) -> PinRecord: ...

    def get_pin(self, conn: sqlite3.Connection, pin_id: PinId) -> PinRecord | None: ...


def _row_to_pin(row: PinRow) -> PinRecord:
    return PinRecord(
        id=PinId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        boundary_id=BoundaryId(row.boundary_id),
        manifest_version=ManifestVersion(row.manifest_version),
        claim_ids=tuple(ClaimId(c) for c in tuple_from_json(row.claim_ids_json)),
        created_at=datetime.fromisoformat(row.created_at),
    )


class SqlitePinRepository:
    def insert_pin(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        manifest_version: ManifestVersion,
        claim_ids: tuple[ClaimId, ...],
        created_at: datetime,
        pin_id: PinId | None = None,
    ) -> PinRecord:
        pid = pin_id or PinId(str(uuid4()))
        row = PinRow(
            id=str(pid),
            workspace_id=str(workspace_id),
            boundary_id=str(boundary_id),
            manifest_version=int(manifest_version),
            claim_ids_json=tuple_to_json(tuple(str(c) for c in claim_ids)),
            created_at=created_at.isoformat(),
        )
        conn.execute(
            """
            INSERT INTO pins
            (id, workspace_id, boundary_id, manifest_version, claim_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.workspace_id,
                row.boundary_id,
                row.manifest_version,
                row.claim_ids_json,
                row.created_at,
            ),
        )
        return _row_to_pin(row)

    def get_pin(self, conn: sqlite3.Connection, pin_id: PinId) -> PinRecord | None:
        raw = fetch_one_row(conn, "SELECT * FROM pins WHERE id = ?", (str(pin_id),))
        if raw is None:
            return None
        row = PinRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            boundary_id=cell_str(raw, "boundary_id"),
            manifest_version=cell_int(raw, "manifest_version"),
            claim_ids_json=cell_str(raw, "claim_ids_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_pin(row)
