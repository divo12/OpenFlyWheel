"""Verified claim repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.enums import ClaimState, TruthSection
from openflywheel.contracts.ids import BoundaryId, ClaimId, IdentityId, ProposalId, WorkspaceId
from openflywheel.store.rows import ClaimRow
from openflywheel.store.serde import model_from_json, model_to_json
from openflywheel.store.sqlite_access import cell_int, cell_str, fetch_all_rows, fetch_one_row


class ClaimRepository(Protocol):
    def insert_claim(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        what: str,
        how: str,
        section: TruthSection,
        state: ClaimState,
        authority_identity_id: IdentityId,
        acl: AclLabel,
        valid_from: datetime,
        valid_to: datetime | None,
        source_proposal_id: ProposalId | None,
        claim_id: ClaimId | None = None,
    ) -> ClaimRecord: ...

    def get_claim(self, conn: sqlite3.Connection, claim_id: ClaimId) -> ClaimRecord | None: ...

    def close_validity(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: ClaimId,
        valid_to: datetime,
        state: ClaimState,
    ) -> ClaimRecord: ...

    def count_claims(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> int: ...

    def list_active_for_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        as_of: datetime | None = None,
        claim_ids: frozenset[ClaimId] | None = None,
    ) -> tuple[ClaimRecord, ...]: ...

    def list_superseded_history(
        self, conn: sqlite3.Connection, *, boundary_id: BoundaryId, what: str
    ) -> tuple[ClaimRecord, ...]: ...


def _row_to_claim(row: ClaimRow) -> ClaimRecord:
    return ClaimRecord(
        id=ClaimId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        boundary_id=BoundaryId(row.boundary_id),
        what=row.what,
        how=row.how,
        section=TruthSection(row.section),
        state=ClaimState(row.state),
        authority_identity_id=IdentityId(row.authority_identity_id),
        acl=model_from_json(AclLabel, row.acl_json),
        valid_from=datetime.fromisoformat(row.valid_from),
        valid_to=datetime.fromisoformat(row.valid_to) if row.valid_to else None,
        source_proposal_id=ProposalId(row.source_proposal_id) if row.source_proposal_id else None,
    )


class SqliteClaimRepository:
    def insert_claim(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        what: str,
        how: str,
        section: TruthSection,
        state: ClaimState,
        authority_identity_id: IdentityId,
        acl: AclLabel,
        valid_from: datetime,
        valid_to: datetime | None,
        source_proposal_id: ProposalId | None,
        claim_id: ClaimId | None = None,
    ) -> ClaimRecord:
        cid = claim_id or ClaimId(str(uuid4()))
        row = ClaimRow(
            id=str(cid),
            workspace_id=str(workspace_id),
            boundary_id=str(boundary_id),
            what=what,
            how=how,
            section=section.value,
            state=state.value,
            authority_identity_id=str(authority_identity_id),
            acl_json=model_to_json(acl),
            valid_from=valid_from.isoformat(),
            valid_to=valid_to.isoformat() if valid_to else None,
            source_proposal_id=str(source_proposal_id) if source_proposal_id else None,
        )
        conn.execute(
            """
            INSERT INTO claims
            (id, workspace_id, boundary_id, what, how, section, state,
             authority_identity_id, acl_json, valid_from, valid_to, source_proposal_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.workspace_id,
                row.boundary_id,
                row.what,
                row.how,
                row.section,
                row.state,
                row.authority_identity_id,
                row.acl_json,
                row.valid_from,
                row.valid_to,
                row.source_proposal_id,
            ),
        )
        return _row_to_claim(row)

    def get_claim(self, conn: sqlite3.Connection, claim_id: ClaimId) -> ClaimRecord | None:
        raw = fetch_one_row(conn, "SELECT * FROM claims WHERE id = ?", (str(claim_id),))
        if raw is None:
            return None
        return self._raw_to_claim(raw)

    def close_validity(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: ClaimId,
        valid_to: datetime,
        state: ClaimState,
    ) -> ClaimRecord:
        conn.execute(
            """
            UPDATE claims SET valid_to = ?, state = ?
            WHERE id = ?
            """,
            (valid_to.isoformat(), state.value, str(claim_id)),
        )
        claim = self.get_claim(conn, claim_id)
        if claim is None:
            from openflywheel.store.exceptions import StoreNotFoundError

            raise StoreNotFoundError(
                code="CLAIM_NOT_FOUND",
                message=f"Claim {claim_id} missing after validity close",
                root_cause_hint="Concurrent delete or schema mismatch",
                safe_retry=False,
                stop_condition="Inspect claims table",
            )
        return claim

    def count_claims(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> int:
        row = fetch_one_row(
            conn,
            "SELECT COUNT(*) AS cnt FROM claims WHERE workspace_id = ?",
            (str(workspace_id),),
        )
        if row is None:
            return 0
        return cell_int(row, "cnt")

    def list_active_for_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        as_of: datetime | None = None,
        claim_ids: frozenset[ClaimId] | None = None,
    ) -> tuple[ClaimRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM claims
            WHERE boundary_id = ? AND state = ?
            ORDER BY valid_from DESC
            """,
            (str(boundary_id), ClaimState.ACTIVE.value),
        )
        result: list[ClaimRecord] = []
        for raw in rows:
            claim = self._raw_to_claim(raw)
            if claim_ids is not None and claim.id not in claim_ids:
                continue
            if as_of is not None:
                if claim.valid_from > as_of:
                    continue
                if claim.valid_to is not None and claim.valid_to <= as_of:
                    continue
            result.append(claim)
        return tuple(result)

    def list_active_for_workspace(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[ClaimRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM claims
            WHERE workspace_id = ? AND state = ? AND valid_to IS NULL
            ORDER BY valid_from DESC
            """,
            (str(workspace_id), ClaimState.ACTIVE.value),
        )
        return tuple(self._raw_to_claim(raw) for raw in rows)

    def list_superseded_history(
        self, conn: sqlite3.Connection, *, boundary_id: BoundaryId, what: str
    ) -> tuple[ClaimRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM claims
            WHERE boundary_id = ? AND what = ?
            ORDER BY valid_from
            """,
            (str(boundary_id), what),
        )
        return tuple(self._raw_to_claim(raw) for raw in rows)

    def _raw_to_claim(self, raw: sqlite3.Row) -> ClaimRecord:
        row = ClaimRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            boundary_id=cell_str(raw, "boundary_id"),
            what=cell_str(raw, "what"),
            how=cell_str(raw, "how"),
            section=cell_str(raw, "section"),
            state=cell_str(raw, "state"),
            authority_identity_id=cell_str(raw, "authority_identity_id"),
            acl_json=cell_str(raw, "acl_json"),
            valid_from=cell_str(raw, "valid_from"),
            valid_to=cell_str(raw, "valid_to") if raw["valid_to"] is not None else None,
            source_proposal_id=(
                cell_str(raw, "source_proposal_id")
                if raw["source_proposal_id"] is not None
                else None
            ),
        )
        return _row_to_claim(row)
