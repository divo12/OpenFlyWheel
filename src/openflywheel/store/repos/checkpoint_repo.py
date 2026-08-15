"""Checkpoint repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.ids import CheckpointId, SourceId
from openflywheel.store.internal_records import CheckpointRecord
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_one_row,
)


class CheckpointRepository(Protocol):
    def get_checkpoint(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> CheckpointRecord | None: ...

    def upsert_checkpoint(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: SourceId,
        cursor_value: str,
        updated_at: datetime,
    ) -> CheckpointRecord: ...


class SqliteCheckpointRepository:
    def get_checkpoint(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> CheckpointRecord | None:
        raw = fetch_one_row(
            conn, "SELECT * FROM checkpoints WHERE source_id = ?", (str(source_id),)
        )
        if raw is None:
            return None
        return CheckpointRecord(
            id=CheckpointId(cell_str(raw, "id")),
            source_id=SourceId(cell_str(raw, "source_id")),
            cursor_value=cell_str(raw, "cursor_value"),
            updated_at=datetime.fromisoformat(cell_str(raw, "updated_at")),
        )

    def upsert_checkpoint(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: SourceId,
        cursor_value: str,
        updated_at: datetime,
    ) -> CheckpointRecord:
        existing = self.get_checkpoint(conn, source_id)
        cid = existing.id if existing else CheckpointId(str(uuid4()))
        if existing is not None:
            conn.execute(
                "UPDATE checkpoints SET cursor_value = ?, updated_at = ? WHERE source_id = ?",
                (cursor_value, updated_at.isoformat(), str(source_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO checkpoints (id, source_id, cursor_value, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(cid), str(source_id), cursor_value, updated_at.isoformat()),
            )
        record = self.get_checkpoint(conn, source_id)
        if record is None:
            from openflywheel.store.exceptions import StoreNotFoundError

            raise StoreNotFoundError(
                code="CHECKPOINT_NOT_FOUND",
                message="Checkpoint missing after upsert",
                root_cause_hint="Checkpoint write did not persist",
                safe_retry=True,
                stop_condition="Inspect checkpoints table integrity",
            )
        return record
