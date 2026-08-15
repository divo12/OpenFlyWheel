"""Episode ingest orchestration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.connectors.github.protocol import GitHubClient
from openflywheel.connectors.github.scan import ScanItemKind
from openflywheel.contracts.enums import AdmissionDecision, LocatorKind, RejectReason
from openflywheel.contracts.episode import EpisodeRecord, SourceReference
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.contracts.ids import EpisodeId, SourceId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.admission import evaluate_admission
from openflywheel.ingest.scope import is_within_component_paths, merge_exclusions
from openflywheel.store.checkpoint_hook import CheckpointCommitHook
from openflywheel.store.db import Database
from openflywheel.store.exceptions import DomainError, IngestTransactionError, map_sqlite_error
from openflywheel.store.repos.audit_repo import SqliteAuditRejectRepository
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.checkpoint_repo import SqliteCheckpointRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.uow import IngestUnitOfWork


class IngestSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted_count: int
    rejected_count: int
    skipped_out_of_scope_count: int
    episode_ids: tuple[EpisodeId, ...] = Field(default_factory=tuple)


class EpisodeIngestService:
    def __init__(
        self,
        database: Database,
        *,
        checkpoint_hook: CheckpointCommitHook | None = None,
    ) -> None:
        self._database = database
        self._episodes = SqliteEpisodeRepository()
        self._audit = SqliteAuditRejectRepository()
        self._checkpoints = SqliteCheckpointRepository()
        self._boundaries = SqliteBoundaryRepository()
        self._uow = IngestUnitOfWork(database, checkpoint_hook=checkpoint_hook)

    def ingest_github_fixture(
        self,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        client: GitHubClient,
        cli_excluded_paths: tuple[str, ...] = (),
    ) -> OperationResult[IngestSummary]:
        with self._database.read() as conn:
            if not self._boundaries.has_locked_boundary(conn, workspace_id):
                return OperationResult.failure(
                    code="INGEST_PRECONDITION",
                    message="Ingest requires at least one locked boundary",
                    root_cause_hint="Complete onboard lock before ingest",
                    safe_retry=True,
                    stop_condition="Lock a boundary manifest",
                    next_actions=("Run ofw onboard lock",),
                )
            locked_exclusions = self._boundaries.locked_exclusions(conn, workspace_id)
            component_paths = self._boundaries.locked_component_paths(conn, workspace_id)

        excluded_paths = merge_exclusions(locked_exclusions, cli_excluded_paths)
        accepted_ids: list[EpisodeId] = []
        rejected_count = 0
        skipped_out_of_scope = 0
        ingest_time = datetime.now(tz=UTC)

        for item in client.list_scan_items():
            try:
                if item.kind == ScanItemKind.UNSUPPORTED and item.unsupported is not None:
                    unsupported = item.unsupported
                    if not is_within_component_paths(unsupported.external_id, component_paths):
                        skipped_out_of_scope += 1
                        continue
                    if self._record_reject_and_cursor(
                        workspace_id=workspace_id,
                        source_id=source_id,
                        external_id=unsupported.external_id,
                        reason=RejectReason.UNSUPPORTED_CONTENT,
                        detail=unsupported.detail,
                        ingest_time=ingest_time,
                        checkpoint_cursor=unsupported.external_id,
                    ):
                        rejected_count += 1
                    continue

                if item.envelope is None:
                    continue

                envelope = item.envelope
                if not is_within_component_paths(envelope.external_id, component_paths):
                    skipped_out_of_scope += 1
                    continue

                verdict = evaluate_admission(envelope, excluded_paths=excluded_paths)
                if verdict.decision == AdmissionDecision.REJECT:
                    if self._record_reject_and_cursor(
                        workspace_id=workspace_id,
                        source_id=source_id,
                        external_id=envelope.external_id,
                        reason=verdict.reason or RejectReason.JUNK,
                        detail=verdict.detail,
                        ingest_time=ingest_time,
                        checkpoint_cursor=envelope.external_id,
                    ):
                        rejected_count += 1
                    continue

                existing = self._find_idempotent(source_id, envelope.external_id, verdict.checksum)
                if existing is not None:
                    accepted_ids.append(existing.id)
                    self._advance_cursor_only(
                        source_id=source_id,
                        checkpoint_cursor=envelope.external_id,
                        ingest_time=ingest_time,
                    )
                    continue

                bundle = self._uow.commit_episode_bundle(
                    workspace_id=workspace_id,
                    source_id=source_id,
                    source_ref=SourceReference(
                        source_id=source_id,
                        external_id=envelope.external_id,
                        uri=envelope.uri,
                    ),
                    content_text=envelope.content_text,
                    acl=envelope.acl,
                    event_time=envelope.event_time,
                    ingest_time=ingest_time,
                    checksum=verdict.checksum,
                    content_type=envelope.content_type,
                    anchors=self._default_anchors(envelope),
                    checkpoint_cursor=envelope.external_id,
                )
                accepted_ids.append(bundle.episode.id)
            except IngestTransactionError as exc:
                return exc.to_operation_result()
            except DomainError as exc:
                return exc.to_operation_result()
            except sqlite3.Error as exc:
                return map_sqlite_error(exc).to_operation_result()

        summary = IngestSummary(
            accepted_count=len(accepted_ids),
            rejected_count=rejected_count,
            skipped_out_of_scope_count=skipped_out_of_scope,
            episode_ids=tuple(accepted_ids),
        )
        return OperationResult.success(
            summary=(
                f"Ingested {summary.accepted_count} episodes; "
                f"rejected {summary.rejected_count}; "
                f"skipped {summary.skipped_out_of_scope_count} out-of-scope"
            ),
            data=summary,
            next_actions=("Run SaO extraction after boundary lock",),
        )

    def _default_anchors(
        self, envelope: ConnectorEnvelope
    ) -> tuple[tuple[EvidenceLocator, str], ...]:
        return (
            (
                EvidenceLocator(
                    kind=LocatorKind.FILE_LINE,
                    value=f"{envelope.external_id}:1",
                ),
                envelope.external_id,
            ),
        )

    def _find_idempotent(
        self, source_id: SourceId, external_id: str, checksum: str
    ) -> EpisodeRecord | None:
        with self._database.read() as conn:
            return self._episodes.find_idempotent(conn, source_id, external_id, checksum)

    def _record_reject_and_cursor(
        self,
        *,
        workspace_id: WorkspaceId,
        source_id: SourceId,
        external_id: str,
        reason: RejectReason,
        detail: str,
        ingest_time: datetime,
        checkpoint_cursor: str,
    ) -> bool:
        hook = self._uow.checkpoint_hook
        with self._database.write() as conn:
            inserted = self._audit.record_reject_idempotent(
                conn,
                workspace_id=workspace_id,
                source_id=source_id,
                external_id=external_id,
                reason=reason,
                detail=detail,
                rejected_at=ingest_time,
            )
            hook.before_checkpoint_commit(source_id=source_id, cursor_value=checkpoint_cursor)
            self._checkpoints.upsert_checkpoint(
                conn,
                source_id=source_id,
                cursor_value=checkpoint_cursor,
                updated_at=ingest_time,
            )
        return inserted is not None

    def _advance_cursor_only(
        self,
        *,
        source_id: SourceId,
        checkpoint_cursor: str,
        ingest_time: datetime,
    ) -> None:
        self._uow.commit_reject_cursor(
            source_id=source_id,
            checkpoint_cursor=checkpoint_cursor,
            updated_at=ingest_time,
        )
