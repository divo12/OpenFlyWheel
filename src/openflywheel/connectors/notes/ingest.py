"""Expert notes markdown connector."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from openflywheel.application.identity_gate import IdentityGate
from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import (
    AdmissionDecision,
    LocatorKind,
    ProposalStatus,
    TruthSection,
    VisibilityLevel,
)
from openflywheel.contracts.episode import SourceReference
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.contracts.ids import (
    BoundaryId,
    EpisodeId,
    IdentityId,
    ProposalId,
    SourceId,
    WorkspaceId,
)
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.admission import compute_checksum, evaluate_admission
from openflywheel.store.db import Database
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.uow import IngestUnitOfWork

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class NoteFrontmatter(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    authority: str
    boundary: str | None = None


class NoteIngestSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: EpisodeId
    proposal_id: ProposalId
    idempotent: bool


class ExpertNotesService:
    def __init__(
        self,
        database: Database,
        *,
        uow: IngestUnitOfWork | None = None,
    ) -> None:
        self._database = database
        self._uow = uow or IngestUnitOfWork(database)
        self._episodes = SqliteEpisodeRepository()
        self._proposals = SqliteProposalRepository()
        self._identity_gate = IdentityGate(database)
        self._sources = SqliteSourceRepository()
        self._boundaries = SqliteBoundaryRepository()

    def ingest_note(
        self,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        boundary_id: BoundaryId,
        authority_identity_id: IdentityId,
        note_path: Path,
    ) -> OperationResult[NoteIngestSummary]:
        identity = self._identity_gate.resolve(
            workspace_id=workspace_id,
            identity_id=authority_identity_id,
        )
        if identity.error is not None:
            return OperationResult.failure(
                code="NOTE_IDENTITY_UNKNOWN",
                message="Authority identity not found",
                root_cause_hint=identity.error.root_cause_hint,
                safe_retry=False,
                stop_condition="Provide workspace authority identity",
            )
        with self._database.read() as conn:
            source = self._sources.get_by_id(conn, source_id)
            if source is None or source.workspace_id != workspace_id:
                return OperationResult.failure(
                    code="NOTE_SOURCE_UNKNOWN",
                    message="Note source not found for workspace",
                    root_cause_hint="Connect expert notes source first",
                    safe_retry=True,
                    stop_condition="Run onboard connect for expert notes",
                )
            boundary = self._boundaries.get_by_id(conn, boundary_id)
            if boundary is None or boundary.workspace_id != workspace_id:
                return OperationResult.failure(
                    code="NOTE_BOUNDARY_UNKNOWN",
                    message="Note boundary not found for workspace",
                    root_cause_hint="Use locked boundary id from workspace",
                    safe_retry=False,
                    stop_condition="Provide valid boundary id",
                )

        raw_text = note_path.read_text(encoding="utf-8")
        frontmatter, body = _parse_note(raw_text)
        external_id = f"note:{note_path.name}:{compute_checksum(raw_text)}"
        envelope = ConnectorEnvelope(
            external_id=external_id,
            uri=f"note://{note_path.name}",
            content_text=body,
            content_type="text/markdown",
            event_time=datetime.now(tz=UTC),
            acl=AclLabel(
                visibility=VisibilityLevel.INTERNAL,
                allowed_identities=(authority_identity_id,),
            ),
        )
        verdict = evaluate_admission(envelope, excluded_paths=())
        if verdict.decision == AdmissionDecision.REJECT:
            return OperationResult.failure(
                code="NOTE_REJECTED",
                message="Note rejected by admission policy",
                root_cause_hint=verdict.detail,
                safe_retry=False,
                stop_condition="Provide admissible note content",
            )

        with self._database.read() as conn:
            existing = self._episodes.find_idempotent(
                conn, source_id, external_id, verdict.checksum
            )
        if existing is not None:
            with self._database.read() as conn:
                proposal = self._proposals.find_by_idempotency_key(
                    conn,
                    workspace_id=workspace_id,
                    idempotency_key=f"note:{external_id}",
                )
            if proposal is None:
                return OperationResult.failure(
                    code="NOTE_INCONSISTENT",
                    message="Idempotent episode without proposal",
                    root_cause_hint="Partial prior ingest",
                    safe_retry=False,
                    stop_condition="Inspect note ingest state",
                )
            return OperationResult.success(
                summary=f"Idempotent note episode {existing.id}",
                data=NoteIngestSummary(
                    episode_id=existing.id,
                    proposal_id=proposal.id,
                    idempotent=True,
                ),
            )

        now = datetime.now(tz=UTC)
        with self._database.write() as conn:
            bundle = self._uow.commit_episode_bundle(
                workspace_id=workspace_id,
                source_id=source_id,
                source_ref=SourceReference(
                    source_id=source_id,
                    external_id=external_id,
                    uri=envelope.uri,
                ),
                content_text=body,
                acl=envelope.acl,
                event_time=envelope.event_time,
                ingest_time=now,
                checksum=verdict.checksum,
                content_type=envelope.content_type,
                anchors=(
                    (
                        EvidenceLocator(
                            kind=LocatorKind.DOCUMENT_SPAN,
                            value=f"{note_path.name}:1",
                        ),
                        frontmatter.title,
                    ),
                ),
                checkpoint_cursor=external_id,
                conn=conn,
            )
            anchor_id = bundle.anchors[0].id
            proposal = self._proposals.insert_proposal(
                conn,
                workspace_id=workspace_id,
                boundary_id=boundary_id,
                what=frontmatter.title,
                how=body[:500],
                section=TruthSection.U5,
                proposer_identity_id=authority_identity_id,
                anchor_ids=(anchor_id,),
                status=ProposalStatus.PENDING,
                idempotency_key=f"note:{external_id}",
                created_at=now,
                proposal_id=ProposalId(str(uuid4())),
            )

        return OperationResult.success(
            summary=f"Ingested note episode {bundle.episode.id}",
            data=NoteIngestSummary(
                episode_id=bundle.episode.id,
                proposal_id=proposal.id,
                idempotent=False,
            ),
            next_actions=("Verify note proposal before it becomes a claim",),
        )


def _parse_note(text: str) -> tuple[NoteFrontmatter, str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        return NoteFrontmatter(title="Untitled note", authority="expert"), text.strip()
    body = text[match.end() :].strip()
    block_raw = match.group(1)
    if not isinstance(block_raw, str):
        return NoteFrontmatter(title="Untitled note", authority="expert"), body
    block = block_raw
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", maxsplit=1)
        fields[key.strip()] = value.strip()
    return NoteFrontmatter(
        title=fields.get("title", "Untitled note"),
        authority=fields.get("authority", "expert"),
        boundary=fields.get("boundary"),
    ), body
