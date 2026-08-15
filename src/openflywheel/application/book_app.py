"""Book application orchestration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from openflywheel.application.ingest_app import IngestApplication
from openflywheel.book.coverage import CoverageService
from openflywheel.book.pin import PinService
from openflywheel.book.propose import manual_proposal_idempotency_key
from openflywheel.book.verify import VerifyService
from openflywheel.contracts.agent_session import (
    CorrectionRecordRequest,
    CorrectionRecordSummary,
    EpisodeRecordRequest,
    EpisodeRecordSummary,
)
from openflywheel.contracts.book import (
    BookContextRequest,
    BookContextResult,
    ClaimDetail,
    CoverageGapsResult,
    ExtractSummary,
    PinSummary,
    ProposeManualRequest,
    VerifyRequest,
    VerifySummary,
)
from openflywheel.contracts.enums import ProposalStatus
from openflywheel.contracts.ids import (
    BoundaryId,
    ClaimId,
    IdentityId,
    PinId,
    ProposalId,
    WorkspaceId,
)
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.agent_episode import AgentEpisodeService
from openflywheel.ingest.episode_service import IngestSummary
from openflywheel.ingest.sao.service import SaOExtractService
from openflywheel.retrieval.service import RetrievalService
from openflywheel.store.db import Database
from openflywheel.store.exceptions import map_sqlite_error
from openflywheel.store.repos.coverage_repo import SqliteCoverageRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.sqlite_access import fetch_one_row


class BookApplication:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._sao = SaOExtractService(database)
        self._verify = VerifyService(database)
        self._coverage = CoverageService()
        self._pins = PinService(database)
        self._retrieval = RetrievalService(database)
        self._proposals = SqliteProposalRepository()
        self._coverage_repo = SqliteCoverageRepository()
        self._ingest = IngestApplication(database)
        self._agent_episodes = AgentEpisodeService(database)

    def extract(self, *, workspace_id: WorkspaceId) -> OperationResult[ExtractSummary]:
        return self._sao.extract_for_workspace(workspace_id=workspace_id)

    def claim_propose(self, request: ProposeManualRequest) -> OperationResult[ProposalId]:
        from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository

        boundaries = SqliteBoundaryRepository()
        with self._database.read() as conn:
            boundary = boundaries.get_by_id(conn, request.boundary_id)
            if boundary is None or boundary.workspace_id != request.workspace_id:
                return OperationResult.failure(
                    code="PROPOSE_BOUNDARY_NOT_FOUND",
                    message="Boundary not found",
                    root_cause_hint="Check boundary id",
                    safe_retry=False,
                    stop_condition="Use locked boundary id",
                )
            for anchor_id in request.anchor_ids:
                if (
                    fetch_one_row(
                        conn,
                        "SELECT id FROM evidence_anchors WHERE id = ?",
                        (str(anchor_id),),
                    )
                    is None
                ):
                    return OperationResult.failure(
                        code="PROPOSE_ANCHOR_NOT_FOUND",
                        message="Evidence anchor not found",
                        root_cause_hint="Anchor ids must reference ingested evidence",
                        safe_retry=False,
                        stop_condition="Use anchor ids from ingested episodes",
                    )

        now = datetime.now(tz=UTC)
        idempotency_key = manual_proposal_idempotency_key(request)
        try:
            with self._database.write() as conn:
                existing = self._proposals.find_by_idempotency_key(
                    conn,
                    workspace_id=request.workspace_id,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    return OperationResult.success(
                        summary=f"Idempotent proposal {existing.id}",
                        data=existing.id,
                    )
                proposal = self._proposals.insert_proposal(
                    conn,
                    workspace_id=request.workspace_id,
                    boundary_id=request.boundary_id,
                    what=request.what,
                    how=request.how,
                    section=request.section,
                    proposer_identity_id=request.proposer_identity_id,
                    anchor_ids=request.anchor_ids,
                    status=ProposalStatus.PENDING,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    proposal_id=ProposalId(str(uuid4())),
                )
        except sqlite3.IntegrityError:
            with self._database.read() as conn:
                existing = self._proposals.find_by_idempotency_key(
                    conn,
                    workspace_id=request.workspace_id,
                    idempotency_key=idempotency_key,
                )
            if existing is not None:
                return OperationResult.success(
                    summary=f"Idempotent proposal {existing.id}",
                    data=existing.id,
                )
            return map_sqlite_error(
                sqlite3.IntegrityError("unique constraint violated")
            ).to_operation_result()
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        return OperationResult.success(
            summary=f"Created proposal {proposal.id}",
            data=proposal.id,
        )

    def book_verify(
        self, *, workspace_id: WorkspaceId, request: VerifyRequest
    ) -> OperationResult[VerifySummary]:
        return self._verify.verify(workspace_id=workspace_id, request=request)

    def book_pin(
        self, *, workspace_id: WorkspaceId, boundary_id: BoundaryId
    ) -> OperationResult[PinSummary]:
        return self._pins.create_pin(workspace_id=workspace_id, boundary_id=boundary_id)

    def coverage_gaps(self, *, workspace_id: WorkspaceId) -> OperationResult[CoverageGapsResult]:
        with self._database.write() as conn:
            self._coverage.seed_workspace(conn, workspace_id)
        with self._database.read() as conn:
            report = self._coverage.org_report(conn, workspace_id)
            gaps = self._coverage.gaps_for_workspace(conn, workspace_id)
            unverified = self._coverage_repo.list_unverified(conn, workspace_id)
        result = CoverageGapsResult(gaps=gaps, report=report, unmet_requirements=unverified)
        return OperationResult.success(
            summary=f"Org coverage {report.overall_ratio:.0%}; {len(gaps)} gaps",
            data=result,
        )

    def book_context(self, request: BookContextRequest) -> OperationResult[BookContextResult]:
        return self._retrieval.book_context(request)

    def book_get(
        self,
        *,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
        claim_id: ClaimId,
        pin_id: PinId | None = None,
    ) -> OperationResult[ClaimDetail]:
        return self._retrieval.book_get(
            workspace_id=workspace_id,
            identity_id=identity_id,
            claim_id=claim_id,
            pin_id=pin_id,
        )

    def episode_record(
        self, request: EpisodeRecordRequest
    ) -> OperationResult[EpisodeRecordSummary]:
        return self._agent_episodes.record_episode(request)

    def correction_record(
        self, request: CorrectionRecordRequest
    ) -> OperationResult[CorrectionRecordSummary]:
        return self._agent_episodes.record_correction(request)

    def run_fixture_ingest(
        self,
        *,
        workspace_id: WorkspaceId,
        fixture_root: Path,
        cli_excluded_paths: tuple[str, ...] = (),
    ) -> OperationResult[IngestSummary]:
        return self._ingest.run_fixture_ingest(
            workspace_id=workspace_id,
            fixture_root=fixture_root,
            cli_excluded_paths=cli_excluded_paths,
        )
