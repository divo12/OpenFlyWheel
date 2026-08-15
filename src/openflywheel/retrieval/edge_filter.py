"""ACL-safe edge filtering for retrieval packets."""

from __future__ import annotations

from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.edges import EdgeRecord
from openflywheel.contracts.ids import ClaimId, IdentityId
from openflywheel.retrieval.acl import claim_visible_to_identity


def visible_claim_ids(
    claims: tuple[ClaimRecord, ...],
    identity_id: IdentityId,
) -> frozenset[ClaimId]:
    return frozenset(c.id for c in claims if claim_visible_to_identity(c, identity_id))


def filter_bidirectional_edges(
    edges: tuple[EdgeRecord, ...],
    visible_ids: frozenset[ClaimId],
) -> tuple[EdgeRecord, ...]:
    return tuple(
        edge
        for edge in edges
        if edge.from_claim_id in visible_ids and edge.to_claim_id in visible_ids
    )
