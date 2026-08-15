"""Coverage requirement repository."""

from __future__ import annotations

import sqlite3
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.coverage import CoverageRequirementRecord
from openflywheel.contracts.enums import SystemShape, TruthSection
from openflywheel.contracts.ids import BoundaryId, CoverageRequirementId, WorkspaceId
from openflywheel.store.rows import CoverageRequirementRow
from openflywheel.store.sqlite_access import (
    cell_int,
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class CoverageRepository(Protocol):
    def insert_if_missing(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        section: TruthSection,
        slot_key: str,
        description: str,
        required_for_shape: SystemShape,
        requirement_id: CoverageRequirementId | None = None,
    ) -> CoverageRequirementRecord: ...

    def mark_verified(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        slot_key: str,
    ) -> CoverageRequirementRecord | None: ...

    def list_for_boundary(
        self, conn: sqlite3.Connection, boundary_id: BoundaryId
    ) -> tuple[CoverageRequirementRecord, ...]: ...

    def list_unverified(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[CoverageRequirementRecord, ...]: ...


def _row_to_requirement(row: CoverageRequirementRow) -> CoverageRequirementRecord:
    return CoverageRequirementRecord(
        id=CoverageRequirementId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        boundary_id=BoundaryId(row.boundary_id),
        section=TruthSection(row.section),
        slot_key=row.slot_key,
        description=row.description,
        required_for_shape=SystemShape(row.required_for_shape),
        verified=bool(row.verified),
    )


class SqliteCoverageRepository:
    def insert_if_missing(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        section: TruthSection,
        slot_key: str,
        description: str,
        required_for_shape: SystemShape,
        requirement_id: CoverageRequirementId | None = None,
    ) -> CoverageRequirementRecord:
        existing = conn.execute(
            """
            SELECT * FROM coverage_requirements
            WHERE boundary_id = ? AND slot_key = ?
            """,
            (str(boundary_id), slot_key),
        ).fetchone()
        if existing is not None:
            return self._raw_to_requirement(existing)
        rid = requirement_id or CoverageRequirementId(str(uuid4()))
        row = CoverageRequirementRow(
            id=str(rid),
            workspace_id=str(workspace_id),
            boundary_id=str(boundary_id),
            section=section.value,
            slot_key=slot_key,
            description=description,
            required_for_shape=required_for_shape.value,
            verified=0,
        )
        conn.execute(
            """
            INSERT INTO coverage_requirements
            (id, workspace_id, boundary_id, section, slot_key, description,
             required_for_shape, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.workspace_id,
                row.boundary_id,
                row.section,
                row.slot_key,
                row.description,
                row.required_for_shape,
                row.verified,
            ),
        )
        return _row_to_requirement(row)

    def mark_verified(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        slot_key: str,
    ) -> CoverageRequirementRecord | None:
        conn.execute(
            """
            UPDATE coverage_requirements SET verified = 1
            WHERE boundary_id = ? AND slot_key = ?
            """,
            (str(boundary_id), slot_key),
        )
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM coverage_requirements
            WHERE boundary_id = ? AND slot_key = ?
            """,
            (str(boundary_id), slot_key),
        )
        if raw is None:
            return None
        return self._raw_to_requirement(raw)

    def list_for_boundary(
        self, conn: sqlite3.Connection, boundary_id: BoundaryId
    ) -> tuple[CoverageRequirementRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM coverage_requirements
            WHERE boundary_id = ?
            ORDER BY section, slot_key
            """,
            (str(boundary_id),),
        )
        return tuple(self._raw_to_requirement(raw) for raw in rows)

    def list_unverified(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[CoverageRequirementRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM coverage_requirements
            WHERE workspace_id = ? AND verified = 0
            ORDER BY boundary_id, section, slot_key
            """,
            (str(workspace_id),),
        )
        return tuple(self._raw_to_requirement(raw) for raw in rows)

    def _raw_to_requirement(self, raw: sqlite3.Row) -> CoverageRequirementRecord:
        row = CoverageRequirementRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            boundary_id=cell_str(raw, "boundary_id"),
            section=cell_str(raw, "section"),
            slot_key=cell_str(raw, "slot_key"),
            description=cell_str(raw, "description"),
            required_for_shape=cell_str(raw, "required_for_shape"),
            verified=cell_int(raw, "verified"),
        )
        return _row_to_requirement(row)
