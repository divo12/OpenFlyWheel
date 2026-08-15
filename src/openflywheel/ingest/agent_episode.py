"""Agent session episode admission and correction write-back."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from openflywheel.application.agent_authorization import AgentAuthorizationService
from openflywheel.application.recursion import recursion_disabled
from openflywheel.connectors.agents.path_guard import (
    resolve_trusted_transcript_roots,
    validate_session_ref,
)
from openflywheel.connectors.agents.transcript import load_canonical_session
from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.agent_session import (
    CanonicalAgentSession,
    CorrectionRecordRequest,
    CorrectionRecordSummary,
    EpisodeRecordRequest,
    EpisodeRecordSummary,
    SessionEnvelope,
)
from openflywheel.contracts.enums import (
    AdmissionDecision,
    BackgroundJobKind,
    LocatorKind,
    ProposalStatus,
    TruthSection,
)
from openflywheel.contracts.episode import EpisodeRecord, SourceReference
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.contracts.ids import AgentSessionId, IdentityId, ProposalId, SourceId
from openflywheel.contracts.jobs import TranscriptExtractPayload
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.admission import evaluate_admission
from openflywheel.store.db import Database
from openflywheel.store.exceptions import map_sqlite_error
from openflywheel.store.repos.agent_session_repo import SqliteAgentSessionRepository
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.job_repo import SqliteBackgroundJobRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.uow import IngestUnitOfWork

_PAYLOAD_ADAPTER: TypeAdapter[TranscriptExtractPayload] = TypeAdapter(TranscriptExtractPayload)


class AgentEpisodeService:
    def __init__(
        self,
        database: Database,
        *,
        uow: IngestUnitOfWork | None = None,
    ) -> None:
        self._database = database
        self._sessions = SqliteAgentSessionRepository()
        self._jobs = SqliteBackgroundJobRepository()
        self._boundaries = SqliteBoundaryRepository()
        self._episodes = SqliteEpisodeRepository()
        self._proposals = SqliteProposalRepository()
        self._uow = uow or IngestUnitOfWork(database)
        self._auth = AgentAuthorizationService(database)

    def record_episode(
        self, request: EpisodeRecordRequest
    ) -> OperationResult[EpisodeRecordSummary]:
        envelope = request.envelope
        authorized = self._auth.authorize_episode(envelope)
        if authorized.error is not None:
            return OperationResult.failure(
                code=authorized.error.code,
                message=authorized.error.message,
                root_cause_hint=authorized.error.root_cause_hint,
                safe_retry=authorized.error.safe_retry,
                stop_condition=authorized.error.stop_condition,
            )

        with self._database.read() as conn:
            if not self._boundaries.has_locked_boundary(conn, envelope.workspace_id):
                return OperationResult.failure(
                    code="EPISODE_PRECONDITION",
                    message="Episode record requires at least one locked boundary",
                    root_cause_hint="Complete onboard lock before agent write-back",
                    safe_retry=True,
                    stop_condition="Lock a boundary manifest",
                    next_actions=("Run ofw onboard lock",),
                )

        session_valid = validate_session_ref(envelope.session_ref)
        if session_valid.error is not None:
            return OperationResult.failure(
                code=session_valid.error.code,
                message=session_valid.error.message,
                root_cause_hint=session_valid.error.root_cause_hint,
                safe_retry=session_valid.error.safe_retry,
                stop_condition=session_valid.error.stop_condition,
            )

        roots_result = resolve_trusted_transcript_roots(
            agent_home=envelope.agent_home,
            project_root=envelope.project_root,
        )
        if roots_result.error is not None:
            return OperationResult.failure(
                code=roots_result.error.code,
                message=roots_result.error.message,
                root_cause_hint=roots_result.error.root_cause_hint,
                safe_retry=roots_result.error.safe_retry,
                stop_condition=roots_result.error.stop_condition,
            )
        if roots_result.data is None:
            return OperationResult.failure(
                code="TRANSCRIPT_ROOTS_INTERNAL",
                message="Trusted transcript roots missing",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )

        session_id = AgentSessionId(str(uuid4()))
        loaded = load_canonical_session(
            platform=envelope.platform,
            transcript_path=Path(envelope.transcript_path),
            session_ref=envelope.session_ref,
            session_id=session_id,
            project_root=envelope.project_root,
            allowed_roots=roots_result.data,
        )
        if loaded.error is not None:
            return OperationResult.failure(
                code=loaded.error.code,
                message=loaded.error.message,
                root_cause_hint=loaded.error.root_cause_hint,
                safe_retry=loaded.error.safe_retry,
                stop_condition=loaded.error.stop_condition,
            )
        if loaded.data is None:
            return OperationResult.failure(
                code="TRANSCRIPT_INTERNAL",
                message="Transcript load returned no data",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )
        canonical = loaded.data
        if not canonical.messages:
            return OperationResult.failure(
                code="TRANSCRIPT_EMPTY",
                message="No admissible human/assistant messages in transcript",
                root_cause_hint="Transcript may contain only tool or system events",
                safe_retry=False,
                stop_condition="Provide transcript with user/assistant text",
            )

        content_text = canonical.render_content_text()
        pointer = f"transcript://{envelope.platform.value}/{envelope.session_ref}"
        connector_envelope = ConnectorEnvelope(
            external_id=f"{envelope.platform.value}:{envelope.session_ref}",
            uri=pointer,
            content_text=content_text,
            content_type="text/plain",
            event_time=canonical.started_at or datetime.now(tz=UTC),
            acl=envelope.acl,
        )
        verdict = evaluate_admission(connector_envelope, excluded_paths=())
        if verdict.decision == AdmissionDecision.REJECT:
            return OperationResult.failure(
                code="EPISODE_REJECTED",
                message="Transcript rejected by admission policy",
                root_cause_hint=verdict.detail,
                safe_retry=False,
                stop_condition="Remove secrets or junk from transcript",
            )

        now = datetime.now(tz=UTC)
        existing = self._find_idempotent(
            envelope.source_id, connector_envelope.external_id, verdict.checksum
        )
        if existing is not None:
            with self._database.write() as conn:
                session = self._sessions.upsert_session(
                    conn,
                    workspace_id=envelope.workspace_id,
                    platform=envelope.platform,
                    session_ref=envelope.session_ref,
                    transcript_pointer=pointer,
                    created_at=now,
                    session_id=session_id,
                )
            return OperationResult.success(
                summary=f"Idempotent episode {existing.id}",
                data=EpisodeRecordSummary(
                    episode_id=existing.id,
                    session_id=session.id,
                    job_scheduled=False,
                    claims_created=0,
                ),
            )

        try:
            with self._database.write() as conn:
                bundle = self._uow.commit_episode_bundle(
                    workspace_id=envelope.workspace_id,
                    source_id=envelope.source_id,
                    source_ref=_source_ref(envelope, pointer),
                    content_text=content_text,
                    acl=envelope.acl,
                    event_time=connector_envelope.event_time,
                    ingest_time=now,
                    checksum=verdict.checksum,
                    content_type=connector_envelope.content_type,
                    anchors=_transcript_anchors(canonical),
                    checkpoint_cursor=connector_envelope.external_id,
                    conn=conn,
                )
                session = self._sessions.upsert_session(
                    conn,
                    workspace_id=envelope.workspace_id,
                    platform=envelope.platform,
                    session_ref=envelope.session_ref,
                    transcript_pointer=pointer,
                    created_at=now,
                    session_id=session_id,
                )
                job_scheduled = False
                if not recursion_disabled():
                    payload = TranscriptExtractPayload(
                        episode_id=str(bundle.episode.id),
                        session_id=str(session.id),
                        boundary_id=str(envelope.boundary_id) if envelope.boundary_id else None,
                        disable_recursion=True,
                    )
                    self._jobs.enqueue(
                        conn,
                        workspace_id=envelope.workspace_id,
                        kind=BackgroundJobKind.TRANSCRIPT_EXTRACT,
                        payload_json=payload.model_dump_json(),
                        created_at=now,
                    )
                    job_scheduled = True
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        return OperationResult.success(
            summary=f"Recorded episode {bundle.episode.id}",
            data=EpisodeRecordSummary(
                episode_id=bundle.episode.id,
                session_id=session.id,
                job_scheduled=job_scheduled,
                claims_created=0,
            ),
            next_actions=("Run background worker to extract proposals",),
        )

    def record_correction(
        self, request: CorrectionRecordRequest
    ) -> OperationResult[CorrectionRecordSummary]:
        authorized = self._auth.authorize_correction(request)
        if authorized.error is not None:
            return OperationResult.failure(
                code=authorized.error.code,
                message=authorized.error.message,
                root_cause_hint=authorized.error.root_cause_hint,
                safe_retry=authorized.error.safe_retry,
                stop_condition=authorized.error.stop_condition,
            )

        now = datetime.now(tz=UTC)
        external_id = f"correction:{request.claim_id}:{now.isoformat()}"
        content_text = f"Correction for claim {request.claim_id}:\n{request.correction_text}"
        envelope = ConnectorEnvelope(
            external_id=external_id,
            uri=f"correction://{request.claim_id}",
            content_text=content_text,
            content_type="text/plain",
            event_time=now,
            acl=_default_internal_acl(request.authority_identity_id),
        )
        verdict = evaluate_admission(envelope, excluded_paths=())
        if verdict.decision == AdmissionDecision.REJECT:
            return OperationResult.failure(
                code="CORRECTION_REJECTED",
                message="Correction rejected by admission policy",
                root_cause_hint=verdict.detail,
                safe_retry=False,
                stop_condition="Provide admissible correction text",
            )

        try:
            with self._database.write() as conn:
                bundle = self._uow.commit_episode_bundle(
                    workspace_id=request.workspace_id,
                    source_id=request.source_id,
                    source_ref=_correction_source_ref(request, external_id),
                    content_text=content_text,
                    acl=envelope.acl,
                    event_time=now,
                    ingest_time=now,
                    checksum=verdict.checksum,
                    content_type=envelope.content_type,
                    anchors=(
                        (
                            EvidenceLocator(
                                kind=LocatorKind.DOCUMENT_SPAN,
                                value=f"{external_id}:1",
                            ),
                            "correction",
                        ),
                    ),
                    checkpoint_cursor=external_id,
                    conn=conn,
                )
                proposal = self._proposals.insert_proposal(
                    conn,
                    workspace_id=request.workspace_id,
                    boundary_id=request.boundary_id,
                    what=request.correction_text,
                    how=f"High-authority correction against claim {request.claim_id}",
                    section=TruthSection.U5,
                    proposer_identity_id=request.authority_identity_id,
                    anchor_ids=request.anchor_ids,
                    status=ProposalStatus.PENDING,
                    idempotency_key=f"correction:{request.claim_id}:{verdict.checksum}",
                    created_at=now,
                    proposal_id=ProposalId(str(uuid4())),
                )
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        return OperationResult.success(
            summary=f"Correction episode {bundle.episode.id} with proposal {proposal.id}",
            data=CorrectionRecordSummary(
                episode_id=bundle.episode.id,
                proposal_id=proposal.id,
            ),
            next_actions=("Verify correction proposal before it becomes a claim",),
        )

    def _find_idempotent(
        self,
        source_id: SourceId,
        external_id: str,
        checksum: str,
    ) -> EpisodeRecord | None:
        with self._database.read() as conn:
            return self._episodes.find_idempotent(conn, source_id, external_id, checksum)


def _source_ref(envelope: SessionEnvelope, pointer: str) -> SourceReference:
    return SourceReference(
        source_id=envelope.source_id,
        external_id=f"{envelope.platform.value}:{envelope.session_ref}",
        uri=pointer,
    )


def _correction_source_ref(request: CorrectionRecordRequest, external_id: str) -> SourceReference:
    return SourceReference(
        source_id=request.source_id,
        external_id=external_id,
        uri=f"correction://{request.claim_id}",
    )


def _transcript_anchors(
    canonical: CanonicalAgentSession,
) -> tuple[tuple[EvidenceLocator, str], ...]:
    anchors: list[tuple[EvidenceLocator, str]] = []
    for message in canonical.messages:
        anchors.append(
            (
                EvidenceLocator(
                    kind=LocatorKind.TRANSCRIPT_SPAN,
                    value=f"{canonical.session_ref}:{message.message_index}",
                ),
                f"{message.role} message",
            )
        )
    return tuple(anchors)


def _default_internal_acl(identity_id: IdentityId) -> AclLabel:
    from openflywheel.contracts.enums import VisibilityLevel

    return AclLabel(
        visibility=VisibilityLevel.INTERNAL,
        allowed_identities=(identity_id,),
    )
