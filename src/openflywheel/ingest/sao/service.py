"""SaO extraction orchestration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from openflywheel.contracts.book import ExtractSummary
from openflywheel.contracts.enums import IdentityKind, ProposalStatus, SourceKind
from openflywheel.contracts.ids import BoundaryId, EpisodeId, IdentityId, ProposalId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.sao.extractors import build_idempotency_key, extract_all
from openflywheel.ingest.sao.models import SaOProposalDraft
from openflywheel.ingest.scope import is_within_component_paths
from openflywheel.store.db import Database
from openflywheel.store.exceptions import DomainError, map_sqlite_error
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


class SaOExtractService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._boundaries = SqliteBoundaryRepository()
        self._episodes = SqliteEpisodeRepository()
        self._proposals = SqliteProposalRepository()
        self._sources = SqliteSourceRepository()
        self._workspaces = SqliteWorkspaceRepository()

    def extract_for_workspace(
        self,
        *,
        workspace_id: WorkspaceId,
    ) -> OperationResult[ExtractSummary]:
        with self._database.read() as conn:
            if not self._boundaries.has_locked_boundary(conn, workspace_id):
                return OperationResult.failure(
                    code="EXTRACT_BEFORE_LOCK",
                    message="SaO extraction requires at least one locked boundary",
                    root_cause_hint="Complete onboard lock before extraction",
                    safe_retry=True,
                    stop_condition="Lock a boundary manifest",
                    next_actions=("Run ofw onboard lock",),
                )
            boundaries = self._boundaries.list_boundaries(conn, workspace_id)
            source = self._sources.get_by_slug(conn, workspace_id, SourceKind.GITHUB.value)
            if source is None:
                return OperationResult.failure(
                    code="EXTRACT_NO_SOURCE",
                    message="GitHub source not configured",
                    root_cause_hint="Run onboard connect first",
                    safe_retry=True,
                    stop_condition="Configure github source via onboard connect",
                )
            proposer_record = self._workspaces.find_identity_by_display_name(
                conn, workspace_id, "sao-extractor"
            )
            episodes = self._episodes.list_episodes_for_source(conn, source.id)

        if proposer_record is None:
            with self._database.write() as conn:
                proposer_id = self._workspaces.create_identity(
                    conn,
                    workspace_id=workspace_id,
                    kind=IdentityKind.AGENT,
                    display_name="sao-extractor",
                    created_at=datetime.now(tz=UTC),
                ).id
        else:
            proposer_id = proposer_record.id

        created_ids: list[ProposalId] = []
        skipped = 0
        now = datetime.now(tz=UTC)

        for boundary in boundaries:
            if boundary.manifest is None:
                continue
            prefixes = boundary.component_paths
            prefix_set = frozenset(prefixes)
            for episode in episodes:
                if not is_within_component_paths(episode.source_ref.external_id, prefix_set):
                    continue
                drafts = extract_all(
                    external_id=episode.source_ref.external_id,
                    content=episode.content_text,
                )
                for draft in drafts:
                    if draft.section.value not in ("U3", "U4"):
                        continue
                    try:
                        proposal_id = self._persist_draft(
                            workspace_id=workspace_id,
                            boundary_id=boundary.id,
                            episode_id=episode.id,
                            proposer_id=proposer_id,
                            draft=draft,
                            created_at=now,
                        )
                        if proposal_id is None:
                            skipped += 1
                        else:
                            created_ids.append(proposal_id)
                    except DomainError as exc:
                        return exc.to_operation_result()
                    except sqlite3.Error as exc:
                        return map_sqlite_error(exc).to_operation_result()

        summary = ExtractSummary(
            proposals_created=len(created_ids),
            proposals_skipped_idempotent=skipped,
            proposal_ids=tuple(created_ids),
        )
        return OperationResult.success(
            summary=(
                f"SaO created {summary.proposals_created} proposals; "
                f"skipped {summary.proposals_skipped_idempotent} idempotent"
            ),
            data=summary,
            next_actions=("Run ofw book verify to promote proposals",),
        )

    def _persist_draft(
        self,
        *,
        workspace_id: WorkspaceId,
        boundary_id: BoundaryId,
        episode_id: EpisodeId,
        proposer_id: IdentityId,
        draft: SaOProposalDraft,
        created_at: datetime,
    ) -> ProposalId | None:
        idempotency_key = build_idempotency_key(
            extractor=draft.extractor,
            boundary_id=str(boundary_id),
            what=draft.what,
            locator_value=draft.locator.value,
            content_fingerprint=draft.content_fingerprint,
        )
        with self._database.write() as conn:
            existing = self._proposals.find_by_idempotency_key(
                conn, workspace_id=workspace_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                return None
            anchor = self._episodes.insert_anchor(
                conn,
                episode_id=episode_id,
                locator=draft.locator,
                label=draft.anchor_label,
            )
            proposal = self._proposals.insert_proposal(
                conn,
                workspace_id=workspace_id,
                boundary_id=boundary_id,
                what=draft.what,
                how=draft.how,
                section=draft.section,
                proposer_identity_id=proposer_id,
                anchor_ids=(anchor.id,),
                status=ProposalStatus.PENDING,
                idempotency_key=idempotency_key,
                created_at=created_at,
                proposal_id=ProposalId(str(uuid4())),
            )
            return proposal.id
