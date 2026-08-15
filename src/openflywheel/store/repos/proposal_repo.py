"""Claim proposal repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import ProposalStatus, TruthSection
from openflywheel.contracts.ids import (
    BoundaryId,
    EvidenceAnchorId,
    IdentityId,
    ProposalId,
    WorkspaceId,
)
from openflywheel.contracts.proposal import ClaimProposalRecord
from openflywheel.store.rows import ProposalRow
from openflywheel.store.serde import tuple_from_json, tuple_to_json
from openflywheel.store.sqlite_access import cell_int, cell_str, fetch_all_rows, fetch_one_row


class ProposalRepository(Protocol):
    def insert_proposal(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        what: str,
        how: str,
        section: TruthSection,
        proposer_identity_id: IdentityId,
        anchor_ids: tuple[EvidenceAnchorId, ...],
        status: ProposalStatus,
        idempotency_key: str,
        created_at: datetime,
        proposal_id: ProposalId | None = None,
    ) -> ClaimProposalRecord: ...

    def find_by_idempotency_key(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        idempotency_key: str,
    ) -> ClaimProposalRecord | None: ...

    def get_proposal(
        self, conn: sqlite3.Connection, proposal_id: ProposalId
    ) -> ClaimProposalRecord | None: ...

    def update_status(
        self,
        conn: sqlite3.Connection,
        *,
        proposal_id: ProposalId,
        status: ProposalStatus,
    ) -> ClaimProposalRecord: ...

    def count_proposals(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> int: ...

    def list_for_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        status: ProposalStatus | None = None,
    ) -> tuple[ClaimProposalRecord, ...]: ...


def _row_to_proposal(row: ProposalRow) -> ClaimProposalRecord:
    return ClaimProposalRecord(
        id=ProposalId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        boundary_id=BoundaryId(row.boundary_id),
        what=row.what,
        how=row.how,
        section=TruthSection(row.section),
        proposer_identity_id=IdentityId(row.proposer_identity_id),
        anchor_ids=tuple(EvidenceAnchorId(a) for a in tuple_from_json(row.anchor_ids_json)),
        status=ProposalStatus(row.status),
        idempotency_key=row.idempotency_key,
        created_at=datetime.fromisoformat(row.created_at),
    )


class SqliteProposalRepository:
    def insert_proposal(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        what: str,
        how: str,
        section: TruthSection,
        proposer_identity_id: IdentityId,
        anchor_ids: tuple[EvidenceAnchorId, ...],
        status: ProposalStatus,
        idempotency_key: str,
        created_at: datetime,
        proposal_id: ProposalId | None = None,
    ) -> ClaimProposalRecord:
        pid = proposal_id or ProposalId(str(uuid4()))
        row = ProposalRow(
            id=str(pid),
            workspace_id=str(workspace_id),
            boundary_id=str(boundary_id),
            what=what,
            how=how,
            section=section.value,
            proposer_identity_id=str(proposer_identity_id),
            anchor_ids_json=tuple_to_json(tuple(str(a) for a in anchor_ids)),
            status=status.value,
            idempotency_key=idempotency_key,
            created_at=created_at.isoformat(),
        )
        conn.execute(
            """
            INSERT INTO proposals
            (id, workspace_id, boundary_id, what, how, section, proposer_identity_id,
             anchor_ids_json, status, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.workspace_id,
                row.boundary_id,
                row.what,
                row.how,
                row.section,
                row.proposer_identity_id,
                row.anchor_ids_json,
                row.status,
                row.idempotency_key,
                row.created_at,
            ),
        )
        return _row_to_proposal(row)

    def find_by_idempotency_key(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        idempotency_key: str,
    ) -> ClaimProposalRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM proposals
            WHERE workspace_id = ? AND idempotency_key = ?
            """,
            (str(workspace_id), idempotency_key),
        )
        if raw is None:
            return None
        return self._raw_to_proposal(raw)

    def get_proposal(
        self, conn: sqlite3.Connection, proposal_id: ProposalId
    ) -> ClaimProposalRecord | None:
        raw = fetch_one_row(conn, "SELECT * FROM proposals WHERE id = ?", (str(proposal_id),))
        if raw is None:
            return None
        return self._raw_to_proposal(raw)

    def update_status(
        self,
        conn: sqlite3.Connection,
        *,
        proposal_id: ProposalId,
        status: ProposalStatus,
    ) -> ClaimProposalRecord:
        conn.execute(
            "UPDATE proposals SET status = ? WHERE id = ?",
            (status.value, str(proposal_id)),
        )
        proposal = self.get_proposal(conn, proposal_id)
        if proposal is None:
            from openflywheel.store.exceptions import StoreNotFoundError

            raise StoreNotFoundError(
                code="PROPOSAL_NOT_FOUND",
                message=f"Proposal {proposal_id} missing after status update",
                root_cause_hint="Concurrent delete or schema mismatch",
                safe_retry=False,
                stop_condition="Inspect proposals table",
            )
        return proposal

    def count_proposals(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> int:
        row = fetch_one_row(
            conn,
            "SELECT COUNT(*) AS cnt FROM proposals WHERE workspace_id = ?",
            (str(workspace_id),),
        )
        if row is None:
            return 0
        return cell_int(row, "cnt")

    def list_for_boundary(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        status: ProposalStatus | None = None,
    ) -> tuple[ClaimProposalRecord, ...]:
        if status is None:
            rows = fetch_all_rows(
                conn,
                "SELECT * FROM proposals WHERE boundary_id = ? ORDER BY created_at",
                (str(boundary_id),),
            )
        else:
            rows = fetch_all_rows(
                conn,
                """
                SELECT * FROM proposals
                WHERE boundary_id = ? AND status = ?
                ORDER BY created_at
                """,
                (str(boundary_id), status.value),
            )
        return tuple(self._raw_to_proposal(raw) for raw in rows)

    def _raw_to_proposal(self, raw: sqlite3.Row) -> ClaimProposalRecord:
        row = ProposalRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            boundary_id=cell_str(raw, "boundary_id"),
            what=cell_str(raw, "what"),
            how=cell_str(raw, "how"),
            section=cell_str(raw, "section"),
            proposer_identity_id=cell_str(raw, "proposer_identity_id"),
            anchor_ids_json=cell_str(raw, "anchor_ids_json"),
            status=cell_str(raw, "status"),
            idempotency_key=cell_str(raw, "idempotency_key"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_proposal(row)
