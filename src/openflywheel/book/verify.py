"""Human verification orchestration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from openflywheel.book.verify_uow import BookVerifyUnitOfWork
from openflywheel.contracts.book import VerifyRequest, VerifySummary
from openflywheel.contracts.enums import IdentityKind, VerificationDecision
from openflywheel.contracts.identity import IdentityRecord
from openflywheel.contracts.ids import IdentityId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.store.db import Database
from openflywheel.store.exceptions import DomainError, map_sqlite_error
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.coverage_repo import SqliteCoverageRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository
from openflywheel.store.sqlite_access import cell_int, fetch_one_row


class VerifyService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._proposals = SqliteProposalRepository()
        self._boundaries = SqliteBoundaryRepository()
        self._workspaces = SqliteWorkspaceRepository()
        self._claims = SqliteClaimRepository()
        self._coverage = SqliteCoverageRepository()
        self._uow = BookVerifyUnitOfWork(database)

    def verify(
        self,
        *,
        workspace_id: WorkspaceId,
        request: VerifyRequest,
    ) -> OperationResult[VerifySummary]:
        with self._database.read() as conn:
            proposal = self._proposals.get_proposal(conn, request.proposal_id)
            if proposal is None or proposal.workspace_id != workspace_id:
                return OperationResult.failure(
                    code="VERIFY_NOT_FOUND",
                    message="Proposal not found in workspace",
                    root_cause_hint="Check proposal id",
                    safe_retry=False,
                    stop_condition="Use a valid proposal id",
                )
            boundary = self._boundaries.get_by_id(conn, proposal.boundary_id)
            if boundary is None or boundary.manifest is None:
                return OperationResult.failure(
                    code="VERIFY_BOUNDARY_UNLOCKED",
                    message="Boundary manifest required for verification",
                    root_cause_hint="Lock boundary before verify",
                    safe_retry=True,
                    stop_condition="Complete onboard lock",
                )
            verifier = self._find_identity(conn, workspace_id, request.verifier_identity_id)
            if verifier is None:
                return OperationResult.failure(
                    code="VERIFY_IDENTITY_UNKNOWN",
                    message="Verifier identity not found",
                    root_cause_hint="Unknown identity fails closed",
                    safe_retry=False,
                    stop_condition="Use a workspace identity id",
                )
            is_human_owner = (
                verifier.kind == IdentityKind.HUMAN
                and request.verifier_identity_id in boundary.manifest.owner_identity_ids
            )
            if not is_human_owner:
                return OperationResult.failure(
                    code="VERIFY_UNAUTHORIZED",
                    message="Verifier lacks authority for this boundary",
                    root_cause_hint="Only human boundary owners may verify proposals",
                    safe_retry=False,
                    stop_condition="Use a human owner identity from the manifest",
                )
            if (
                request.decision == VerificationDecision.LEAVE_IN_TENSION
                and request.tension_with_claim_id is None
            ):
                return OperationResult.failure(
                    code="VERIFY_TENSION_REQUIRED",
                    message="Leave-in-tension requires a visible same-boundary counterpart",
                    root_cause_hint="Provide tension_with_claim_id for an active claim",
                    safe_retry=False,
                    stop_condition="Pick an active claim in the same boundary",
                )

        now = datetime.now(tz=UTC)
        try:
            result = self._uow.execute(
                proposal=proposal,
                boundary=boundary,
                decision=request.decision,
                verifier_id=request.verifier_identity_id,
                verified_at=now,
                tension_with_claim_id=request.tension_with_claim_id,
                supersedes_claim_id=request.supersedes_claim_id,
                derived_from_claim_id=request.derived_from_claim_id,
                is_human_owner=is_human_owner,
                acl=request.acl,
            )
        except DomainError as exc:
            return exc.to_operation_result()
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        summary = VerifySummary(
            proposal_id=result.proposal.id,
            decision=request.decision,
            claim_id=result.claim.id if result.claim else None,
            edge_ids=result.edge_ids,
        )
        return OperationResult.success(
            summary=f"Verified proposal {proposal.id} as {request.decision.value}",
            data=summary,
            next_actions=("Run ofw coverage to inspect gaps",),
        )

    def _find_identity(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
    ) -> IdentityRecord | None:
        for identity in self._workspaces.list_identities(conn, workspace_id):
            if identity.id == identity_id:
                return identity
        return None

    def count_before_verify(self, workspace_id: WorkspaceId) -> tuple[int, int, int]:
        with self._database.read() as conn:
            claims = self._claims.count_claims(conn, workspace_id)
            proposals = self._proposals.count_proposals(conn, workspace_id)
            verified_row = fetch_one_row(
                conn,
                """
                SELECT COUNT(*) AS cnt FROM coverage_requirements
                WHERE workspace_id = ? AND verified = 1
                """,
                (str(workspace_id),),
            )
            verified_count = cell_int(verified_row, "cnt") if verified_row is not None else 0
        return claims, proposals, verified_count
