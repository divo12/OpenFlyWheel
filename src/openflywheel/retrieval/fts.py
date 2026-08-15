"""SQLite FTS5 retrieval with ACL-before-ranking."""

from __future__ import annotations

import sqlite3

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.ids import BoundaryId, ClaimId, IdentityId, WorkspaceId
from openflywheel.retrieval.acl import claim_visible_to_identity
from openflywheel.retrieval.fts_query import escape_fts_query
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.sqlite_access import cell_str, fetch_all_rows


class ClaimFtsRepository:
    def __init__(self) -> None:
        self._claims = SqliteClaimRepository()

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        query: str,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId | None,
        identity_id: IdentityId,
        limit: int = 20,
    ) -> tuple[ClaimId, ...]:
        fts_query = escape_fts_query(query)
        if not fts_query:
            return tuple()

        visible = self._visible_active_claims(
            conn,
            workspace_id=workspace_id,
            boundary_id=boundary_id,
            identity_id=identity_id,
        )
        if not visible:
            return tuple()

        visible_ids = [str(c.id) for c in visible]
        placeholders = ",".join("?" for _ in visible_ids)
        rows = fetch_all_rows(
            conn,
            f"""
            SELECT claim_id FROM claim_fts
            WHERE claim_fts MATCH ? AND claim_id IN ({placeholders})
            LIMIT ?
            """,
            (fts_query, *visible_ids, limit),
        )

        result: list[ClaimId] = []
        for raw in rows:
            result.append(ClaimId(cell_str(raw, "claim_id")))
        return tuple(result)

    def _visible_active_claims(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId | None,
        identity_id: IdentityId,
    ) -> tuple[ClaimRecord, ...]:
        if boundary_id is not None:
            candidates = self._claims.list_active_for_boundary(conn, boundary_id=boundary_id)
        else:
            candidates = self._claims.list_active_for_workspace(conn, workspace_id)
        return tuple(c for c in candidates if claim_visible_to_identity(c, identity_id))
