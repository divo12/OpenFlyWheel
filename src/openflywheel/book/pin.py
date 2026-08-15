"""Immutable pin snapshots with frozen claim state."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from openflywheel.contracts.book import PinSummary
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.enums import LocatorKind
from openflywheel.contracts.evidence import EvidenceAnchorRecord, EvidenceLocator
from openflywheel.contracts.ids import (
    BoundaryId,
    EpisodeId,
    EvidenceAnchorId,
    ManifestVersion,
    PinId,
    WorkspaceId,
)
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.pin import PinRecord
from openflywheel.store.db import Database
from openflywheel.store.exceptions import map_sqlite_error
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.pin_repo import SqlitePinRepository
from openflywheel.store.repos.pin_snapshot_repo import SqlitePinSnapshotRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository


class PinService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._boundaries = SqliteBoundaryRepository()
        self._claims = SqliteClaimRepository()
        self._pins = SqlitePinRepository()
        self._snapshots = SqlitePinSnapshotRepository()
        self._edges = SqliteEdgeRepository()
        self._proposals = SqliteProposalRepository()
        self._episodes = SqliteEpisodeRepository()

    def create_pin(
        self,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
    ) -> OperationResult[PinSummary]:
        with self._database.read() as conn:
            boundary = self._boundaries.get_by_id(conn, boundary_id)
            if boundary is None or boundary.workspace_id != workspace_id:
                return OperationResult.failure(
                    code="PIN_BOUNDARY_NOT_FOUND",
                    message="Boundary not found in workspace",
                    root_cause_hint="Check boundary id",
                    safe_retry=False,
                    stop_condition="Use a locked boundary id",
                )
            if boundary.manifest is None:
                return OperationResult.failure(
                    code="PIN_BOUNDARY_UNLOCKED",
                    message="Cannot pin an unlocked boundary",
                    root_cause_hint="Lock boundary first",
                    safe_retry=True,
                    stop_condition="Complete onboard lock",
                )
            active = self._claims.list_active_for_boundary(conn, boundary_id=boundary_id)
            manifest_version = boundary.manifest.version
            claim_id_set = frozenset(c.id for c in active)
            edges = self._edges.list_direct_neighbors(conn, claim_id_set)

        now = datetime.now(tz=UTC)
        try:
            with self._database.write() as conn:
                pin = self._pins.insert_pin(
                    conn,
                    workspace_id=workspace_id,
                    boundary_id=boundary_id,
                    manifest_version=ManifestVersion(manifest_version),
                    claim_ids=tuple(c.id for c in active),
                    created_at=now,
                )
                for claim in active:
                    anchor_ids = self._anchor_ids_for_claim(conn, claim)
                    self._snapshots.insert_claim_snapshot(
                        conn,
                        pin_id=pin.id,
                        claim=claim,
                        anchor_ids=anchor_ids,
                    )
                    for anchor in self._load_anchors(conn, anchor_ids):
                        self._snapshots.insert_anchor_snapshot(
                            conn,
                            pin_id=pin.id,
                            anchor=anchor,
                            claim_id=claim.id,
                        )
                pinned_edges = tuple(
                    e
                    for e in edges
                    if e.from_claim_id in claim_id_set and e.to_claim_id in claim_id_set
                )
                for edge in pinned_edges:
                    self._snapshots.insert_edge_snapshot(conn, pin_id=pin.id, edge=edge)
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        summary = PinSummary(
            pin_id=pin.id,
            boundary_id=boundary_id,
            claim_count=len(active),
            manifest_version=int(pin.manifest_version),
            created_at=pin.created_at,
        )
        return OperationResult.success(
            summary=f"Pinned {summary.claim_count} claims at manifest v{summary.manifest_version}",
            data=summary,
            artifacts=(str(pin.id),),
        )

    def get_pin(self, pin_id: PinId) -> PinRecord | None:
        with self._database.read() as conn:
            return self._pins.get_pin(conn, pin_id)

    def _anchor_ids_for_claim(
        self, conn: sqlite3.Connection, claim: ClaimRecord
    ) -> tuple[EvidenceAnchorId, ...]:
        if claim.source_proposal_id is None:
            return tuple()
        proposal = self._proposals.get_proposal(conn, claim.source_proposal_id)
        if proposal is None:
            return tuple()
        return proposal.anchor_ids

    def _load_anchors(
        self, conn: sqlite3.Connection, anchor_ids: tuple[EvidenceAnchorId, ...]
    ) -> tuple[EvidenceAnchorRecord, ...]:
        from openflywheel.store.sqlite_access import cell_str, fetch_one_row

        anchors: list[EvidenceAnchorRecord] = []
        for anchor_id in anchor_ids:
            raw = fetch_one_row(
                conn,
                "SELECT * FROM evidence_anchors WHERE id = ?",
                (str(anchor_id),),
            )
            if raw is None:
                continue
            anchors.append(
                EvidenceAnchorRecord(
                    id=anchor_id,
                    episode_id=EpisodeId(cell_str(raw, "episode_id")),
                    locator=EvidenceLocator(
                        kind=LocatorKind(cell_str(raw, "locator_kind")),
                        value=cell_str(raw, "locator_value"),
                    ),
                    label=cell_str(raw, "label"),
                )
            )
        return tuple(anchors)
