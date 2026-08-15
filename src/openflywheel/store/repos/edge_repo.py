"""Claim graph edge repository."""

from __future__ import annotations

import sqlite3
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import EdgeKind
from openflywheel.contracts.ids import ClaimId, EdgeId
from openflywheel.store.rows import EdgeRow
from openflywheel.store.sqlite_access import cell_str, fetch_all_rows


class EdgeRepository(Protocol):
    def insert_edge(
        self,
        conn: sqlite3.Connection,
        *,
        kind: EdgeKind,
        from_claim_id: ClaimId,
        to_claim_id: ClaimId,
        note: str,
        edge_id: EdgeId | None = None,
    ) -> EdgeRecord: ...

    def list_edges_for_claim(
        self, conn: sqlite3.Connection, claim_id: ClaimId
    ) -> tuple[EdgeRecord, ...]: ...

    def list_direct_neighbors(
        self, conn: sqlite3.Connection, claim_ids: frozenset[ClaimId]
    ) -> tuple[EdgeRecord, ...]: ...


def _row_to_edge(row: EdgeRow) -> EdgeRecord:
    return EdgeRecord(
        id=EdgeId(row.id),
        kind=EdgeKind(row.kind),
        from_claim_id=ClaimId(row.from_claim_id),
        to_claim_id=ClaimId(row.to_claim_id),
        note=row.note,
    )


class SqliteEdgeRepository:
    def insert_edge(
        self,
        conn: sqlite3.Connection,
        *,
        kind: EdgeKind,
        from_claim_id: ClaimId,
        to_claim_id: ClaimId,
        note: str,
        edge_id: EdgeId | None = None,
    ) -> EdgeRecord:
        eid = edge_id or EdgeId(str(uuid4()))
        row = EdgeRow(
            id=str(eid),
            kind=kind.value,
            from_claim_id=str(from_claim_id),
            to_claim_id=str(to_claim_id),
            note=note,
        )
        conn.execute(
            """
            INSERT INTO edges (id, kind, from_claim_id, to_claim_id, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row.id, row.kind, row.from_claim_id, row.to_claim_id, row.note),
        )
        return _row_to_edge(row)

    def list_edges_for_claim(
        self, conn: sqlite3.Connection, claim_id: ClaimId
    ) -> tuple[EdgeRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM edges
            WHERE from_claim_id = ? OR to_claim_id = ?
            ORDER BY id
            """,
            (str(claim_id), str(claim_id)),
        )
        return tuple(self._raw_to_edge(raw) for raw in rows)

    def list_direct_neighbors(
        self, conn: sqlite3.Connection, claim_ids: frozenset[ClaimId]
    ) -> tuple[EdgeRecord, ...]:
        if not claim_ids:
            return tuple()
        placeholders = ",".join("?" for _ in claim_ids)
        params = [str(cid) for cid in claim_ids]
        rows = fetch_all_rows(
            conn,
            f"""
            SELECT * FROM edges
            WHERE from_claim_id IN ({placeholders}) OR to_claim_id IN ({placeholders})
            ORDER BY id
            """,
            (*params, *params),
        )
        return tuple(self._raw_to_edge(raw) for raw in rows)

    def _raw_to_edge(self, raw: sqlite3.Row) -> EdgeRecord:
        row = EdgeRow(
            id=cell_str(raw, "id"),
            kind=cell_str(raw, "kind"),
            from_claim_id=cell_str(raw, "from_claim_id"),
            to_claim_id=cell_str(raw, "to_claim_id"),
            note=cell_str(raw, "note"),
        )
        return _row_to_edge(row)
