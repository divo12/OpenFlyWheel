"""Atomic verification transaction."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from openflywheel.book.coverage import CoverageService
from openflywheel.book.ontology import resolve_verified_slot
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.boundary import SystemBoundaryRecord
from openflywheel.contracts.claim import ClaimRecord
from openflywheel.contracts.enums import (
    ClaimState,
    EdgeKind,
    ProposalStatus,
    VerificationDecision,
    VisibilityLevel,
)
from openflywheel.contracts.ids import BoundaryId, ClaimId, EdgeId, IdentityId
from openflywheel.contracts.proposal import ClaimProposalRecord
from openflywheel.retrieval.acl import claim_visible_to_identity
from openflywheel.store.db import Database
from openflywheel.store.exceptions import DomainError
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.coverage_repo import SqliteCoverageRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository


@dataclass(frozen=True)
class VerifyTransactionResult:
    proposal: ClaimProposalRecord
    claim: ClaimRecord | None
    edge_ids: tuple[EdgeId, ...]


class BookVerifyUnitOfWork:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._proposals = SqliteProposalRepository()
        self._claims = SqliteClaimRepository()
        self._edges = SqliteEdgeRepository()
        self._coverage = SqliteCoverageRepository()
        self._coverage_service = CoverageService()

    def execute(
        self,
        *,
        proposal: ClaimProposalRecord,
        boundary: SystemBoundaryRecord,
        decision: VerificationDecision,
        verifier_id: IdentityId,
        verified_at: datetime,
        tension_with_claim_id: ClaimId | None = None,
        supersedes_claim_id: ClaimId | None = None,
        derived_from_claim_id: ClaimId | None = None,
        is_human_owner: bool,
        acl: AclLabel | None = None,
    ) -> VerifyTransactionResult:
        if not is_human_owner:
            raise DomainError(
                code="VERIFY_UNAUTHORIZED",
                message="Verifier lacks authority for this boundary",
                root_cause_hint="Only human boundary owners may verify proposals",
                safe_retry=False,
                stop_condition="Use a human owner identity from the manifest",
            )
        if proposal.status != ProposalStatus.PENDING:
            raise DomainError(
                code="VERIFY_NOT_PENDING",
                message="Proposal is not pending",
                root_cause_hint="Proposal already verified",
                safe_retry=False,
                stop_condition="Select a pending proposal",
            )
        if decision == VerificationDecision.LEAVE_IN_TENSION and tension_with_claim_id is None:
            raise DomainError(
                code="VERIFY_TENSION_REQUIRED",
                message="Leave-in-tension requires a visible same-boundary counterpart",
                root_cause_hint="Provide tension_with_claim_id for an active claim",
                safe_retry=False,
                stop_condition="Pick an active claim in the same boundary",
            )

        with self._database.write() as conn:
            fresh = self._proposals.get_proposal(conn, proposal.id)
            if fresh is None or fresh.status != ProposalStatus.PENDING:
                raise DomainError(
                    code="VERIFY_NOT_PENDING",
                    message="Proposal is not pending",
                    root_cause_hint="Proposal state changed concurrently",
                    safe_retry=True,
                    stop_condition="Refresh proposal list",
                )

            self._validate_edge_targets(
                conn,
                boundary_id=proposal.boundary_id,
                verifier_id=verifier_id,
                tension_with_claim_id=tension_with_claim_id,
                supersedes_claim_id=supersedes_claim_id,
                derived_from_claim_id=derived_from_claim_id,
                require_tension_visible=decision == VerificationDecision.LEAVE_IN_TENSION,
            )

            self._coverage_service.seed_for_boundary(conn, boundary)

            if decision == VerificationDecision.REJECT:
                updated = self._proposals.update_status(
                    conn, proposal_id=proposal.id, status=ProposalStatus.REJECTED
                )
                return VerifyTransactionResult(proposal=updated, claim=None, edge_ids=tuple())

            proposal_status = (
                ProposalStatus.IN_TENSION
                if decision == VerificationDecision.LEAVE_IN_TENSION
                else ProposalStatus.PROMOTED
            )
            claim_state = (
                ClaimState.PROPOSED
                if decision == VerificationDecision.LEAVE_IN_TENSION
                else ClaimState.ACTIVE
            )

            claim = self._claims.insert_claim(
                conn,
                workspace_id=proposal.workspace_id,
                boundary_id=proposal.boundary_id,
                what=proposal.what,
                how=proposal.how,
                section=proposal.section,
                state=claim_state,
                authority_identity_id=verifier_id,
                acl=acl or AclLabel(visibility=VisibilityLevel.INTERNAL),
                valid_from=verified_at,
                valid_to=None,
                source_proposal_id=proposal.id,
                claim_id=ClaimId(str(uuid4())),
            )
            updated = self._proposals.update_status(
                conn, proposal_id=proposal.id, status=proposal_status
            )

            edge_ids: list[EdgeId] = []
            if supersedes_claim_id is not None:
                self._claims.close_validity(
                    conn,
                    claim_id=supersedes_claim_id,
                    valid_to=verified_at,
                    state=ClaimState.SUPERSEDED,
                )
                edge = self._edges.insert_edge(
                    conn,
                    kind=EdgeKind.SUPERSEDES,
                    from_claim_id=claim.id,
                    to_claim_id=supersedes_claim_id,
                    note="Human verification supersession",
                )
                edge_ids.append(edge.id)

            if tension_with_claim_id is not None:
                edge = self._edges.insert_edge(
                    conn,
                    kind=EdgeKind.IN_TENSION_WITH,
                    from_claim_id=claim.id,
                    to_claim_id=tension_with_claim_id,
                    note="Human verification tension",
                )
                edge_ids.append(edge.id)

            if derived_from_claim_id is not None:
                edge = self._edges.insert_edge(
                    conn,
                    kind=EdgeKind.DERIVED_FROM,
                    from_claim_id=claim.id,
                    to_claim_id=derived_from_claim_id,
                    note="Human verification lineage",
                )
                edge_ids.append(edge.id)

            if decision == VerificationDecision.PROMOTE:
                slot = resolve_verified_slot(proposal.section, proposal.what, proposal.how)
                if slot is not None:
                    self._coverage.mark_verified(
                        conn, boundary_id=proposal.boundary_id, slot_key=slot
                    )

            return VerifyTransactionResult(
                proposal=updated,
                claim=claim,
                edge_ids=tuple(edge_ids),
            )

    def _validate_edge_targets(
        self,
        conn: sqlite3.Connection,
        *,
        boundary_id: BoundaryId,
        verifier_id: IdentityId,
        tension_with_claim_id: ClaimId | None,
        supersedes_claim_id: ClaimId | None,
        derived_from_claim_id: ClaimId | None,
        require_tension_visible: bool,
    ) -> None:
        for label, target_id, require_active, require_visible in (
            ("tension_with", tension_with_claim_id, True, require_tension_visible),
            ("supersedes", supersedes_claim_id, True, False),
            ("derived_from", derived_from_claim_id, False, False),
        ):
            if target_id is None:
                continue
            target = self._claims.get_claim(conn, target_id)
            if target is None:
                raise DomainError(
                    code="VERIFY_TARGET_NOT_FOUND",
                    message=f"{label} claim not found",
                    root_cause_hint="Target claim id is invalid",
                    safe_retry=False,
                    stop_condition="Use an existing claim in the same boundary",
                )
            if target.boundary_id != boundary_id:
                raise DomainError(
                    code="VERIFY_TARGET_BOUNDARY",
                    message=f"{label} claim must share proposal boundary",
                    root_cause_hint="Cross-boundary edges are refused",
                    safe_retry=False,
                    stop_condition="Pick a claim from the same boundary",
                )
            if require_active and (
                target.state != ClaimState.ACTIVE or target.valid_to is not None
            ):
                raise DomainError(
                    code="VERIFY_TARGET_NOT_ACTIVE",
                    message=f"{label} target must be an active claim",
                    root_cause_hint="Supersede/tension requires active counterpart",
                    safe_retry=False,
                    stop_condition="Promote or pick an active claim",
                )
            if require_visible and not claim_visible_to_identity(target, verifier_id):
                raise DomainError(
                    code="VERIFY_TENSION_NOT_VISIBLE",
                    message="Tension counterpart must be visible to verifier",
                    root_cause_hint="Pick an ACL-visible active claim in the same boundary",
                    safe_retry=False,
                    stop_condition="Use a claim the verifier can access",
                )
