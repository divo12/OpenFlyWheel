"""Deterministic context retrieval."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from openflywheel.book.coverage import CoverageService
from openflywheel.contracts.book import BookContextRequest, BookContextResult, ClaimDetail
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import ClaimState, EdgeKind, LocatorKind
from openflywheel.contracts.evidence import EvidenceAnchorRecord, EvidenceLocator
from openflywheel.contracts.ids import (
    ClaimId,
    EpisodeId,
    EvidenceAnchorId,
    IdentityId,
    PinId,
    WorkspaceId,
)
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.retrieval.acl import claim_visible_to_identity, filter_claims_by_acl
from openflywheel.retrieval.edge_filter import filter_bidirectional_edges, visible_claim_ids
from openflywheel.retrieval.expand import EdgeExpansionService
from openflywheel.retrieval.fts import ClaimFtsRepository
from openflywheel.retrieval.packet import build_packet
from openflywheel.store.db import Database
from openflywheel.store.exceptions import map_sqlite_error
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository
from openflywheel.store.repos.pin_repo import SqlitePinRepository
from openflywheel.store.repos.pin_snapshot_repo import SqlitePinSnapshotRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository
from openflywheel.store.sqlite_access import cell_str, fetch_one_row


class RetrievalService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._claims = SqliteClaimRepository()
        self._fts = ClaimFtsRepository()
        self._expand = EdgeExpansionService()
        self._coverage = CoverageService()
        self._pins = SqlitePinRepository()
        self._snapshots = SqlitePinSnapshotRepository()
        self._workspaces = SqliteWorkspaceRepository()
        self._proposals = SqliteProposalRepository()
        self._edges = SqliteEdgeRepository()

    def book_context(
        self,
        request: BookContextRequest,
    ) -> OperationResult[BookContextResult]:
        if not self._identity_known(request.workspace_id, request.identity_id):
            return OperationResult.failure(
                code="CONTEXT_IDENTITY_UNKNOWN",
                message="Unknown identity; retrieval fails closed",
                root_cause_hint="Provide a workspace identity id",
                safe_retry=False,
                stop_condition="Use a valid identity from workspace init/onboard",
            )

        try:
            with self._database.read() as conn:
                pin_id = request.pin_id
                if pin_id is not None:
                    claims, anchors, tensions = self._context_from_pin(conn, request)
                else:
                    claims, anchors, tensions = self._context_live(conn, request)
                gaps = self._coverage.gaps_for_workspace(conn, request.workspace_id)
                packet = build_packet(
                    pin_id=pin_id,
                    claims=claims,
                    anchors=anchors,
                    tensions=tensions,
                    gaps=gaps,
                )
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        result = BookContextResult(packet=packet, markdown=packet.markdown_body)
        return OperationResult.success(
            summary=f"Context packet with {len(packet.claims)} claims",
            data=result,
            artifacts=(packet.markdown_body[:120] + "...",),
        )

    def book_get(
        self,
        *,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
        claim_id: ClaimId,
        pin_id: PinId | None = None,
    ) -> OperationResult[ClaimDetail]:
        if not self._identity_known(workspace_id, identity_id):
            return OperationResult.failure(
                code="GET_IDENTITY_UNKNOWN",
                message="Unknown identity; retrieval fails closed",
                root_cause_hint="Provide a workspace identity id",
                safe_retry=False,
                stop_condition="Use a valid identity",
            )
        with self._database.read() as conn:
            if pin_id is not None:
                claim = self._claim_from_pin(conn, pin_id, claim_id)
                if claim is None:
                    return OperationResult.failure(
                        code="GET_PIN_MISMATCH",
                        message="Claim not in pin snapshot",
                        root_cause_hint="Historical pin reads use frozen snapshots",
                        safe_retry=False,
                        stop_condition="Use claim id from pin manifest",
                    )
            else:
                claim = self._claims.get_claim(conn, claim_id)

            if claim is None or claim.workspace_id != workspace_id:
                return OperationResult.failure(
                    code="GET_NOT_FOUND",
                    message="Claim not found",
                    root_cause_hint="Check claim id",
                    safe_retry=False,
                    stop_condition="Use a valid claim id",
                )
            if not claim_visible_to_identity(claim, identity_id):
                return OperationResult.failure(
                    code="GET_ACL_DENIED",
                    message="Identity cannot access this claim",
                    root_cause_hint="ACL filtering fails closed",
                    safe_retry=False,
                    stop_condition="Use an authorized identity",
                )

            if pin_id is not None:
                anchors = self._snapshots.list_anchor_snapshots_for_claim(conn, pin_id, claim.id)
                pin_edges = self._snapshots.list_edge_snapshots(conn, pin_id)
                visible = frozenset({claim.id})
                edges = filter_bidirectional_edges(pin_edges, visible)
                history = self._history_from_pin_edges(conn, pin_id, claim.id, identity_id)
            else:
                anchors = self._anchors_for_proposals(conn, (claim,))
                raw_edges = self._edges.list_edges_for_claim(conn, claim_id)
                visible = visible_claim_ids((claim,), identity_id)
                edges = filter_bidirectional_edges(raw_edges, visible)
                history = self._expand.lineage_history(
                    conn, claim_id=claim_id, identity_id=identity_id
                )

        detail = ClaimDetail(
            claim=claim,
            anchors=anchors,
            edges=edges,
            history=history,
        )
        return OperationResult.success(
            summary=f"Claim {claim_id} with {len(anchors)} anchors",
            data=detail,
        )

    def _context_from_pin(
        self, conn: sqlite3.Connection, request: BookContextRequest
    ) -> tuple[tuple[ClaimRecord, ...], tuple[EvidenceAnchorRecord, ...], tuple[EdgeRecord, ...]]:
        pin_id = request.pin_id
        assert pin_id is not None
        pin = self._pins.get_pin(conn, pin_id)
        if pin is None:
            return tuple(), tuple(), tuple()

        snapshots = self._snapshots.list_claim_snapshots(conn, pin_id)
        claims = filter_claims_by_acl(snapshots, request.identity_id)
        if request.boundary_id is not None:
            claims = tuple(c for c in claims if c.boundary_id == request.boundary_id)

        visible_ids = frozenset(c.id for c in claims)
        pinned_ids = frozenset(pin.claim_ids)

        anchor_list: list[EvidenceAnchorRecord] = []
        for claim in claims:
            anchor_list.extend(
                self._snapshots.list_anchor_snapshots_for_claim(conn, pin_id, claim.id)
            )
        anchors = tuple(anchor_list)

        pin_edges = self._snapshots.list_edge_snapshots(conn, pin_id)
        scoped_edges = tuple(
            e for e in pin_edges if e.from_claim_id in pinned_ids and e.to_claim_id in pinned_ids
        )
        tensions = filter_bidirectional_edges(
            tuple(e for e in scoped_edges if e.kind == EdgeKind.IN_TENSION_WITH),
            visible_ids,
        )
        return claims, anchors, tensions

    def _context_live(
        self, conn: sqlite3.Connection, request: BookContextRequest
    ) -> tuple[tuple[ClaimRecord, ...], tuple[EvidenceAnchorRecord, ...], tuple[EdgeRecord, ...]]:
        fts_ids = self._fts.search(
            conn,
            query=request.query,
            workspace_id=request.workspace_id,
            boundary_id=request.boundary_id,
            identity_id=request.identity_id,
        )
        matched = self._load_claims(conn, fts_ids, request.identity_id)
        expanded = self._expand.expand(
            conn,
            seed_claim_ids=frozenset(c.id for c in matched),
            identity_id=request.identity_id,
        )
        if not expanded and matched:
            expanded = matched
        elif expanded:
            seen: set[ClaimId] = {c.id for c in matched}
            merged: list[ClaimRecord] = list(matched)
            for claim in expanded:
                if claim.id not in seen:
                    merged.append(claim)
            expanded = tuple(merged)

        tensions = self._expand.visible_tension_edges(conn, expanded, request.identity_id)
        anchors = self._anchors_for_proposals(conn, expanded)
        return expanded, anchors, tensions

    def _claim_from_pin(
        self, conn: sqlite3.Connection, pin_id: PinId, claim_id: ClaimId
    ) -> ClaimRecord | None:
        for claim in self._snapshots.list_claim_snapshots(conn, pin_id):
            if claim.id == claim_id:
                return claim
        return None

    def _history_from_pin_edges(
        self,
        conn: sqlite3.Connection,
        pin_id: PinId,
        claim_id: ClaimId,
        identity_id: IdentityId,
    ) -> tuple[ClaimRecord, ...]:
        edges = self._snapshots.list_edge_snapshots(conn, pin_id)
        related: set[ClaimId] = set()
        for edge in edges:
            if edge.kind in (EdgeKind.SUPERSEDES, EdgeKind.DERIVED_FROM):
                if edge.from_claim_id == claim_id:
                    related.add(edge.to_claim_id)
                if edge.to_claim_id == claim_id:
                    related.add(edge.from_claim_id)
        related.discard(claim_id)
        snapshots = self._snapshots.list_claim_snapshots(conn, pin_id)
        by_id = {c.id: c for c in snapshots}
        history: list[ClaimRecord] = []
        for rid in related:
            claim = by_id.get(rid)
            if claim is None:
                continue
            if not claim_visible_to_identity(claim, identity_id):
                continue
            history.append(claim)
        return tuple(history)

    def _identity_known(self, workspace_id: WorkspaceId, identity_id: IdentityId) -> bool:
        with self._database.read() as conn:
            for identity in self._workspaces.list_identities(conn, workspace_id):
                if identity.id == identity_id:
                    return True
        return False

    def _load_claims(
        self,
        conn: sqlite3.Connection,
        claim_ids: tuple[ClaimId, ...],
        identity_id: IdentityId,
    ) -> tuple[ClaimRecord, ...]:
        claims: list[ClaimRecord] = []
        for claim_id in claim_ids:
            claim = self._claims.get_claim(conn, claim_id)
            if claim is None or claim.state != ClaimState.ACTIVE:
                continue
            if claim.valid_to is not None and claim.valid_to <= datetime.now(tz=UTC):
                continue
            if not claim_visible_to_identity(claim, identity_id):
                continue
            claims.append(claim)
        return tuple(claims)

    def _anchors_for_proposals(
        self, conn: sqlite3.Connection, claims: tuple[ClaimRecord, ...]
    ) -> tuple[EvidenceAnchorRecord, ...]:
        anchors: list[EvidenceAnchorRecord] = []
        seen: set[EvidenceAnchorId] = set()
        for claim in claims:
            if claim.source_proposal_id is None:
                continue
            proposal = self._proposals.get_proposal(conn, claim.source_proposal_id)
            if proposal is None:
                continue
            for anchor_id in proposal.anchor_ids:
                if anchor_id in seen:
                    continue
                raw = fetch_one_row(
                    conn,
                    "SELECT * FROM evidence_anchors WHERE id = ?",
                    (str(anchor_id),),
                )
                if raw is None:
                    continue
                anchor = EvidenceAnchorRecord(
                    id=anchor_id,
                    episode_id=EpisodeId(cell_str(raw, "episode_id")),
                    locator=EvidenceLocator(
                        kind=LocatorKind(cell_str(raw, "locator_kind")),
                        value=cell_str(raw, "locator_value"),
                    ),
                    label=cell_str(raw, "label"),
                )
                anchors.append(anchor)
                seen.add(anchor_id)
        return tuple(anchors)
