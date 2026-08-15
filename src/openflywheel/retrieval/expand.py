"""Direct edge expansion for retrieval."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.enums import EdgeKind
from openflywheel.contracts.ids import ClaimId, IdentityId
from openflywheel.retrieval.acl import claim_visible_to_identity
from openflywheel.retrieval.edge_filter import filter_bidirectional_edges, visible_claim_ids
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository


def _claim_valid_from(claim: ClaimRecord) -> datetime:
    return claim.valid_from


class EdgeExpansionService:
    def __init__(self) -> None:
        self._edges = SqliteEdgeRepository()
        self._claims = SqliteClaimRepository()

    def expand(
        self,
        conn: sqlite3.Connection,
        *,
        seed_claim_ids: frozenset[ClaimId],
        identity_id: IdentityId,
        allowed_claim_ids: frozenset[ClaimId] | None = None,
    ) -> tuple[ClaimRecord, ...]:
        if not seed_claim_ids:
            return tuple()
        edges = self._edges.list_direct_neighbors(conn, seed_claim_ids)
        allowed_kinds = {
            EdgeKind.DERIVED_FROM,
            EdgeKind.IN_TENSION_WITH,
            EdgeKind.SUPERSEDES,
        }
        neighbor_ids: set[ClaimId] = set(seed_claim_ids)
        for edge in edges:
            if edge.kind not in allowed_kinds:
                continue
            if allowed_claim_ids is not None and (
                edge.from_claim_id not in allowed_claim_ids
                or edge.to_claim_id not in allowed_claim_ids
            ):
                continue
            neighbor_ids.add(edge.from_claim_id)
            neighbor_ids.add(edge.to_claim_id)

        claims: list[ClaimRecord] = []
        for claim_id in neighbor_ids:
            if allowed_claim_ids is not None and claim_id not in allowed_claim_ids:
                continue
            claim = self._claims.get_claim(conn, claim_id)
            if claim is None:
                continue
            if not claim_visible_to_identity(claim, identity_id):
                continue
            claims.append(claim)
        return tuple(claims)

    def visible_tension_edges(
        self,
        conn: sqlite3.Connection,
        claims: tuple[ClaimRecord, ...],
        identity_id: IdentityId,
    ) -> tuple[EdgeRecord, ...]:
        visible = visible_claim_ids(claims, identity_id)
        if not visible:
            return tuple()
        edges = self._edges.list_direct_neighbors(conn, visible)
        tensions = tuple(e for e in edges if e.kind == EdgeKind.IN_TENSION_WITH)
        return filter_bidirectional_edges(tensions, visible)

    def lineage_history(
        self,
        conn: sqlite3.Connection,
        *,
        claim_id: ClaimId,
        identity_id: IdentityId,
    ) -> tuple[ClaimRecord, ...]:
        edges = self._edges.list_edges_for_claim(conn, claim_id)
        related_ids: set[ClaimId] = set()
        for edge in edges:
            if edge.kind == EdgeKind.SUPERSEDES:
                related_ids.add(edge.from_claim_id)
                related_ids.add(edge.to_claim_id)
            if edge.kind == EdgeKind.DERIVED_FROM:
                related_ids.add(edge.from_claim_id)
                related_ids.add(edge.to_claim_id)
        related_ids.discard(claim_id)
        history: list[ClaimRecord] = []
        for related in related_ids:
            claim = self._claims.get_claim(conn, related)
            if claim is None:
                continue
            if not claim_visible_to_identity(claim, identity_id):
                continue
            history.append(claim)
        return tuple(sorted(history, key=_claim_valid_from))
