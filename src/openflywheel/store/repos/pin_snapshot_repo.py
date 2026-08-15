"""Pin snapshot persistence."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import LocatorKind
from openflywheel.contracts.evidence import EvidenceAnchorRecord, EvidenceLocator
from openflywheel.contracts.ids import ClaimId, EpisodeId, EvidenceAnchorId, PinId
from openflywheel.store.serde import model_to_json, tuple_to_json
from openflywheel.store.sqlite_access import (
    cell_int,
    cell_optional_str,
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class PinSnapshotRepository(Protocol):
    def insert_claim_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        claim: ClaimRecord,
        anchor_ids: tuple[EvidenceAnchorId, ...],
    ) -> None: ...

    def insert_anchor_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        anchor: EvidenceAnchorRecord,
        claim_id: ClaimId,
    ) -> None: ...

    def insert_edge_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        edge: EdgeRecord,
    ) -> None: ...

    def list_claim_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[ClaimRecord, ...]: ...

    def list_anchor_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[EvidenceAnchorRecord, ...]: ...

    def list_anchor_snapshots_for_claim(
        self,
        conn: sqlite3.Connection,
        pin_id: PinId,
        claim_id: ClaimId,
    ) -> tuple[EvidenceAnchorRecord, ...]: ...

    def list_edge_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[EdgeRecord, ...]: ...


class SqlitePinSnapshotRepository:
    def insert_claim_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        claim: ClaimRecord,
        anchor_ids: tuple[EvidenceAnchorId, ...],
    ) -> None:
        conn.execute(
            """
            INSERT INTO pin_claim_snapshots
            (pin_id, claim_id, workspace_id, boundary_id, what, how, section, state,
             authority_identity_id, acl_json, valid_from, valid_to, source_proposal_id,
             anchor_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pin_id),
                str(claim.id),
                str(claim.workspace_id),
                str(claim.boundary_id),
                claim.what,
                claim.how,
                claim.section.value,
                claim.state.value,
                str(claim.authority_identity_id),
                model_to_json(claim.acl),
                claim.valid_from.isoformat(),
                claim.valid_to.isoformat() if claim.valid_to else None,
                str(claim.source_proposal_id) if claim.source_proposal_id else None,
                tuple_to_json(tuple(str(a) for a in anchor_ids)),
            ),
        )

    def insert_anchor_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        anchor: EvidenceAnchorRecord,
        claim_id: ClaimId,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pin_anchor_snapshots
            (pin_id, anchor_id, claim_id, episode_id, locator_kind, locator_value, label)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pin_id),
                str(anchor.id),
                str(claim_id),
                str(anchor.episode_id),
                anchor.locator.kind.value,
                anchor.locator.value,
                anchor.label,
            ),
        )

    def insert_edge_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        pin_id: PinId,
        edge: EdgeRecord,
    ) -> None:
        conn.execute(
            """
            INSERT INTO pin_edge_snapshots
            (pin_id, edge_id, kind, from_claim_id, to_claim_id, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(pin_id),
                str(edge.id),
                edge.kind.value,
                str(edge.from_claim_id),
                str(edge.to_claim_id),
                edge.note,
            ),
        )

    def list_claim_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[ClaimRecord, ...]:
        from datetime import datetime

        from openflywheel.contracts.acl import AclLabel
        from openflywheel.contracts.enums import ClaimState, TruthSection
        from openflywheel.contracts.ids import BoundaryId, IdentityId, ProposalId, WorkspaceId
        from openflywheel.store.serde import model_from_json

        rows = fetch_all_rows(
            conn,
            "SELECT * FROM pin_claim_snapshots WHERE pin_id = ? ORDER BY valid_from",
            (str(pin_id),),
        )
        result: list[ClaimRecord] = []
        for raw in rows:
            valid_to_raw = cell_optional_str(raw, "valid_to")
            proposal_raw = cell_optional_str(raw, "source_proposal_id")
            result.append(
                ClaimRecord(
                    id=ClaimId(cell_str(raw, "claim_id")),
                    workspace_id=WorkspaceId(cell_str(raw, "workspace_id")),
                    boundary_id=BoundaryId(cell_str(raw, "boundary_id")),
                    what=cell_str(raw, "what"),
                    how=cell_str(raw, "how"),
                    section=TruthSection(cell_str(raw, "section")),
                    state=ClaimState(cell_str(raw, "state")),
                    authority_identity_id=IdentityId(cell_str(raw, "authority_identity_id")),
                    acl=model_from_json(AclLabel, cell_str(raw, "acl_json")),
                    valid_from=datetime.fromisoformat(cell_str(raw, "valid_from")),
                    valid_to=datetime.fromisoformat(valid_to_raw) if valid_to_raw else None,
                    source_proposal_id=ProposalId(proposal_raw) if proposal_raw else None,
                )
            )
        return tuple(result)

    def list_anchor_snapshots_for_claim(
        self,
        conn: sqlite3.Connection,
        pin_id: PinId,
        claim_id: ClaimId,
    ) -> tuple[EvidenceAnchorRecord, ...]:
        rows = fetch_all_rows(
            conn,
            """
            SELECT * FROM pin_anchor_snapshots
            WHERE pin_id = ? AND claim_id = ?
            ORDER BY anchor_id
            """,
            (str(pin_id), str(claim_id)),
        )
        return self._rows_to_anchors(rows)

    def list_anchor_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[EvidenceAnchorRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM pin_anchor_snapshots WHERE pin_id = ? ORDER BY anchor_id",
            (str(pin_id),),
        )
        return self._rows_to_anchors(rows)

    def _rows_to_anchors(self, rows: tuple[sqlite3.Row, ...]) -> tuple[EvidenceAnchorRecord, ...]:
        result: list[EvidenceAnchorRecord] = []
        for raw in rows:
            result.append(
                EvidenceAnchorRecord(
                    id=EvidenceAnchorId(cell_str(raw, "anchor_id")),
                    episode_id=EpisodeId(cell_str(raw, "episode_id")),
                    locator=EvidenceLocator(
                        kind=LocatorKind(cell_str(raw, "locator_kind")),
                        value=cell_str(raw, "locator_value"),
                    ),
                    label=cell_str(raw, "label"),
                )
            )
        return tuple(result)

    def list_edge_snapshots(
        self, conn: sqlite3.Connection, pin_id: PinId
    ) -> tuple[EdgeRecord, ...]:
        from openflywheel.contracts.enums import EdgeKind
        from openflywheel.contracts.ids import EdgeId

        rows = fetch_all_rows(
            conn,
            "SELECT * FROM pin_edge_snapshots WHERE pin_id = ? ORDER BY edge_id",
            (str(pin_id),),
        )
        result: list[EdgeRecord] = []
        for raw in rows:
            result.append(
                EdgeRecord(
                    id=EdgeId(cell_str(raw, "edge_id")),
                    kind=EdgeKind(cell_str(raw, "kind")),
                    from_claim_id=ClaimId(cell_str(raw, "from_claim_id")),
                    to_claim_id=ClaimId(cell_str(raw, "to_claim_id")),
                    note=cell_str(raw, "note"),
                )
            )
        return tuple(result)

    def claim_count(self, conn: sqlite3.Connection, pin_id: PinId) -> int:
        row = fetch_one_row(
            conn,
            "SELECT COUNT(*) AS cnt FROM pin_claim_snapshots WHERE pin_id = ?",
            (str(pin_id),),
        )
        if row is None:
            return 0
        return cell_int(row, "cnt")
