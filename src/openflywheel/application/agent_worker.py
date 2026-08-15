"""Background worker for transcript proposal extraction."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter

from openflywheel.application.recursion import background_scope
from openflywheel.contracts.enums import (
    BackgroundJobKind,
    IdentityKind,
    LocatorKind,
    ProposalStatus,
    TruthSection,
)
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.contracts.ids import BoundaryId, EpisodeId, ProposalId
from openflywheel.contracts.jobs import TranscriptExtractPayload
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.store.db import Database
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.job_repo import SqliteBackgroundJobRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

_PAYLOAD_ADAPTER: TypeAdapter[TranscriptExtractPayload] = TypeAdapter(TranscriptExtractPayload)
_PROPOSAL_PATTERN = re.compile(r"\b(should|recommend|propose|must)\b", re.IGNORECASE)


class BackgroundWorkerService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._jobs = SqliteBackgroundJobRepository()
        self._proposals = SqliteProposalRepository()
        self._episodes = SqliteEpisodeRepository()
        self._boundaries = SqliteBoundaryRepository()
        self._workspaces = SqliteWorkspaceRepository()

    def process_next(self, *, owner: str = "ofw-worker") -> OperationResult[int]:
        with background_scope():
            return self._process_next_leased(owner=owner)

    def _process_next_leased(self, *, owner: str) -> OperationResult[int]:
        now = datetime.now(tz=UTC)
        with self._database.write() as conn:
            lease = self._jobs.acquire_lease(conn, owner=owner, lease_seconds=30, now=now)
            if lease is None:
                return OperationResult.success(summary="No pending jobs", data=0)
            job = self._jobs.get_job(conn, lease.job_id)
            if job is None or job.kind != BackgroundJobKind.TRANSCRIPT_EXTRACT:
                self._jobs.fail_job(conn, job_id=lease.job_id, now=now, retryable=False)
                return OperationResult.warning(summary="Unknown job kind", data=0)

        payload = _PAYLOAD_ADAPTER.validate_json(job.payload_json)
        extract_result = self._extract_proposals(payload)
        with self._database.write() as conn:
            if extract_result.error is not None:
                self._jobs.fail_job(
                    conn,
                    job_id=lease.job_id,
                    now=datetime.now(tz=UTC),
                    retryable=extract_result.error.safe_retry,
                )
                return OperationResult.failure(
                    code=extract_result.error.code,
                    message=extract_result.error.message,
                    root_cause_hint=extract_result.error.root_cause_hint,
                    safe_retry=extract_result.error.safe_retry,
                    stop_condition=extract_result.error.stop_condition,
                )
            self._jobs.complete_job(conn, job_id=lease.job_id, now=datetime.now(tz=UTC))
        return OperationResult.success(
            summary=f"Processed job; created {extract_result.data or 0} proposals",
            data=extract_result.data or 0,
        )

    def _extract_proposals(self, payload: TranscriptExtractPayload) -> OperationResult[int]:
        episode_id = EpisodeId(payload.episode_id)
        with self._database.read() as conn:
            episode = self._episodes.get_episode(conn, episode_id)
            if episode is None:
                return OperationResult.failure(
                    code="WORKER_EPISODE_MISSING",
                    message="Episode not found for background extract",
                    root_cause_hint="Episode must be admitted before scheduling extract",
                    safe_retry=False,
                    stop_condition="Re-record episode before worker run",
                )
            workspace_id = episode.workspace_id
            boundaries = self._boundaries.list_boundaries(conn, workspace_id)
            if not boundaries:
                return OperationResult.failure(
                    code="WORKER_NO_BOUNDARY",
                    message="No boundary available for extract",
                    root_cause_hint="Lock a boundary before worker extract",
                    safe_retry=True,
                    stop_condition="Complete onboarding lock",
                )
            boundary_id = (
                BoundaryId(payload.boundary_id) if payload.boundary_id else boundaries[0].id
            )
            anchors = self._episodes.list_anchors_for_episode(conn, episode_id)
            proposer = self._workspaces.find_identity_by_display_name(
                conn, workspace_id, "transcript-extractor"
            )

        if proposer is None:
            with self._database.write() as conn:
                proposer_id = self._workspaces.create_identity(
                    conn,
                    workspace_id=workspace_id,
                    kind=IdentityKind.AGENT,
                    display_name="transcript-extractor",
                    created_at=datetime.now(tz=UTC),
                ).id
        else:
            proposer_id = proposer.id

        created = 0
        now = datetime.now(tz=UTC)
        lines = episode.content_text.splitlines()
        message_index = 0
        for line in lines:
            if not line.startswith("[assistant] "):
                continue
            text = line.removeprefix("[assistant] ")
            if not _PROPOSAL_PATTERN.search(text):
                message_index += 1
                continue
            idempotency_key = f"transcript:{episode_id}:{message_index}"
            with self._database.read() as conn:
                existing = self._proposals.find_by_idempotency_key(
                    conn,
                    workspace_id=workspace_id,
                    idempotency_key=idempotency_key,
                )
            if existing is not None:
                message_index += 1
                continue
            anchor_id = anchors[message_index].id if message_index < len(anchors) else None
            anchor_ids = (anchor_id,) if anchor_id is not None else tuple()
            with self._database.write() as conn:
                if anchor_id is None:
                    anchor = self._episodes.insert_anchor(
                        conn,
                        episode_id=episode_id,
                        locator=EvidenceLocator(
                            kind=LocatorKind.TRANSCRIPT_SPAN,
                            value=f"{episode.source_ref.external_id}:{message_index}",
                        ),
                        label="assistant proposal span",
                    )
                    anchor_ids = (anchor.id,)
                self._proposals.insert_proposal(
                    conn,
                    workspace_id=workspace_id,
                    boundary_id=boundary_id,
                    what=text[:200],
                    how="Extracted deterministically from admitted episode content",
                    section=TruthSection.U3,
                    proposer_identity_id=proposer_id,
                    anchor_ids=anchor_ids,
                    status=ProposalStatus.PENDING,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    proposal_id=ProposalId(str(uuid4())),
                )
                created += 1
            message_index += 1

        return OperationResult.success(summary="Extract complete", data=created)
