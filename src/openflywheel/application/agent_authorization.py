"""Authorization checks for agent episode write-back."""

from __future__ import annotations

from openflywheel.application.identity_gate import IdentityGate
from openflywheel.contracts.agent_session import CorrectionRecordRequest, SessionEnvelope
from openflywheel.contracts.identity import IdentityRecord
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.retrieval.acl import claim_visible_to_identity
from openflywheel.store.db import Database
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository


class AgentAuthorizationService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._identity_gate = IdentityGate(database)
        self._sources = SqliteSourceRepository()
        self._boundaries = SqliteBoundaryRepository()
        self._claims = SqliteClaimRepository()

    def authorize_episode(self, envelope: SessionEnvelope) -> OperationResult[IdentityRecord]:
        identity = self._identity_gate.resolve(
            workspace_id=envelope.workspace_id,
            identity_id=envelope.identity_id,
        )
        if identity.error is not None:
            return OperationResult.failure(
                code="EPISODE_IDENTITY_UNKNOWN",
                message="Identity not found in workspace",
                root_cause_hint=identity.error.root_cause_hint,
                safe_retry=False,
                stop_condition="Provide workspace identity id",
            )
        with self._database.read() as conn:
            source = self._sources.get_by_id(conn, envelope.source_id)
            if source is None or source.workspace_id != envelope.workspace_id:
                return OperationResult.failure(
                    code="EPISODE_SOURCE_UNKNOWN",
                    message="Source not found for workspace",
                    root_cause_hint="Connect agent platform source first",
                    safe_retry=True,
                    stop_condition="Run onboard connect for platform",
                )
            if envelope.boundary_id is not None:
                boundary = self._boundaries.get_by_id(conn, envelope.boundary_id)
                if boundary is None or boundary.workspace_id != envelope.workspace_id:
                    return OperationResult.failure(
                        code="EPISODE_BOUNDARY_UNKNOWN",
                        message="Boundary not found for workspace",
                        root_cause_hint="Use locked boundary id from workspace",
                        safe_retry=False,
                        stop_condition="Provide valid boundary id",
                    )
        if identity.data is None:
            return OperationResult.failure(
                code="EPISODE_IDENTITY_INTERNAL",
                message="Identity resolution returned no data",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )
        return OperationResult.success(
            summary=f"Authorized identity {identity.data.display_name}",
            data=identity.data,
        )

    def authorize_correction(self, request: CorrectionRecordRequest) -> OperationResult[None]:
        identity = self._identity_gate.resolve(
            workspace_id=request.workspace_id,
            identity_id=request.authority_identity_id,
        )
        if identity.error is not None:
            return OperationResult.failure(
                code="CORRECTION_IDENTITY_UNKNOWN",
                message="Authority identity not found",
                root_cause_hint=identity.error.root_cause_hint,
                safe_retry=False,
                stop_condition="Provide workspace authority identity",
            )
        with self._database.read() as conn:
            source = self._sources.get_by_id(conn, request.source_id)
            if source is None or source.workspace_id != request.workspace_id:
                return OperationResult.failure(
                    code="CORRECTION_SOURCE_UNKNOWN",
                    message="Correction source not found",
                    root_cause_hint="Connect correction source",
                    safe_retry=True,
                    stop_condition="Provide valid source id",
                )
            boundary = self._boundaries.get_by_id(conn, request.boundary_id)
            if boundary is None or boundary.workspace_id != request.workspace_id:
                return OperationResult.failure(
                    code="CORRECTION_BOUNDARY_UNKNOWN",
                    message="Correction boundary not found",
                    root_cause_hint="Use locked boundary id",
                    safe_retry=False,
                    stop_condition="Provide valid boundary id",
                )
            claim = self._claims.get_claim(conn, request.claim_id)
            if claim is None or claim.workspace_id != request.workspace_id:
                return OperationResult.failure(
                    code="CORRECTION_CLAIM_UNKNOWN",
                    message="Claim not found for correction",
                    root_cause_hint="Target an existing claim id",
                    safe_retry=False,
                    stop_condition="Provide visible claim id",
                )
            if claim.boundary_id != request.boundary_id:
                return OperationResult.failure(
                    code="CORRECTION_BOUNDARY_MISMATCH",
                    message="Claim boundary does not match correction boundary",
                    root_cause_hint="Use the claim's boundary id",
                    safe_retry=False,
                    stop_condition="Align boundary with claim",
                )
            if not claim_visible_to_identity(claim, request.authority_identity_id):
                return OperationResult.failure(
                    code="CORRECTION_CLAIM_FORBIDDEN",
                    message="Claim not visible to authority identity",
                    root_cause_hint="Use identity authorized for claim ACL",
                    safe_retry=False,
                    stop_condition="Choose authorized identity",
                )
        return OperationResult.success(summary="Correction authorized", data=None)
