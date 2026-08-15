"""Audit reject repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import RejectReason
from openflywheel.contracts.ids import AuditRejectId, SourceId, WorkspaceId
from openflywheel.store.internal_records import AuditRejectRecord
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class AuditRejectRepository(Protocol):
    def record_reject_idempotent(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        external_id: str,
        reason: RejectReason,
        detail: str,
        rejected_at: datetime,
    ) -> AuditRejectRecord | None: ...

    def list_rejects_for_source(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> tuple[AuditRejectRecord, ...]: ...


class SqliteAuditRejectRepository:
    def find_reject(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: SourceId,
        external_id: str,
        reason: RejectReason,
    ) -> AuditRejectRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM audit_rejects
            WHERE source_id = ? AND external_id = ? AND reason = ?
            """,
            (str(source_id), external_id, reason.value),
        )
        if raw is None:
            return None
        return AuditRejectRecord(
            id=AuditRejectId(cell_str(raw, "id")),
            workspace_id=WorkspaceId(cell_str(raw, "workspace_id")),
            source_id=SourceId(cell_str(raw, "source_id")),
            external_id=cell_str(raw, "external_id"),
            reason=RejectReason(cell_str(raw, "reason")),
            detail=cell_str(raw, "detail"),
            rejected_at=datetime.fromisoformat(cell_str(raw, "rejected_at")),
        )

    def record_reject_idempotent(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        external_id: str,
        reason: RejectReason,
        detail: str,
        rejected_at: datetime,
    ) -> AuditRejectRecord | None:
        existing = self.find_reject(
            conn, source_id=source_id, external_id=external_id, reason=reason
        )
        if existing is not None:
            return None
        reject_id = AuditRejectId(str(uuid4()))
        conn.execute(
            """
            INSERT INTO audit_rejects
            (id, workspace_id, source_id, external_id, reason, detail, rejected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(reject_id),
                str(workspace_id),
                str(source_id),
                external_id,
                reason.value,
                detail,
                rejected_at.isoformat(),
            ),
        )
        created = self.find_reject(
            conn, source_id=source_id, external_id=external_id, reason=reason
        )
        return created

    def list_rejects_for_source(
        self, conn: sqlite3.Connection, source_id: SourceId
    ) -> tuple[AuditRejectRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM audit_rejects WHERE source_id = ? ORDER BY rejected_at",
            (str(source_id),),
        )
        result: list[AuditRejectRecord] = []
        for raw in rows:
            result.append(
                AuditRejectRecord(
                    id=AuditRejectId(cell_str(raw, "id")),
                    workspace_id=WorkspaceId(cell_str(raw, "workspace_id")),
                    source_id=SourceId(cell_str(raw, "source_id")),
                    external_id=cell_str(raw, "external_id"),
                    reason=RejectReason(cell_str(raw, "reason")),
                    detail=cell_str(raw, "detail"),
                    rejected_at=datetime.fromisoformat(cell_str(raw, "rejected_at")),
                )
            )
        return tuple(result)
