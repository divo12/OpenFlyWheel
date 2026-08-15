"""Onboarding stage orchestration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from openflywheel.book.coverage import CoverageService
from openflywheel.connectors.agents import claude, cursor
from openflywheel.connectors.github.fixture import FixtureGitHubClient
from openflywheel.connectors.notes import notes_capability_report
from openflywheel.contracts.boundary import BoundaryManifest
from openflywheel.contracts.enums import ConnectorKind, IdentityKind, OnboardingStage, SourceKind
from openflywheel.contracts.ids import (
    BoundaryId,
    IdentityId,
    ManifestVersion,
    OnboardingRunId,
    WorkspaceId,
)
from openflywheel.contracts.onboarding import (
    ConnectStageData,
    LocateStageData,
    LockBoundaryRequest,
    LockStageData,
    OnboardingState,
)
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.source import ConnectorCapabilityReport
from openflywheel.onboarding.locate import scan_fixture_root
from openflywheel.onboarding.stage import stage_at_least
from openflywheel.store.db import Database
from openflywheel.store.exceptions import DomainError, OnboardingTransactionError, map_sqlite_error
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.onboarding_repo import SqliteOnboardingRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


class OnboardingService:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._onboarding = SqliteOnboardingRepository()
        self._coverage = CoverageService()
        self._boundaries = SqliteBoundaryRepository()
        self._sources = SqliteSourceRepository()
        self._workspaces = SqliteWorkspaceRepository()

    def start_or_resume(self, workspace_id: WorkspaceId) -> OperationResult[OnboardingState]:
        now = datetime.now(tz=UTC)
        with self._database.read() as conn:
            state = self._onboarding.get_active(conn, workspace_id)
            if state is None:
                with self._database.write() as write_conn:
                    state = self._onboarding.create_run(
                        write_conn, workspace_id=workspace_id, updated_at=now
                    )
        return OperationResult.success(
            summary=f"Onboarding at stage {state.stage.value}",
            data=state,
            next_actions=("Run ofw onboard connect --home <path>",),
        )

    def run_connect(self, workspace_id: WorkspaceId) -> OperationResult[ConnectStageData]:
        with self._database.read() as conn:
            state = self._onboarding.get_active(conn, workspace_id)
            if state is not None and stage_at_least(state.stage, OnboardingStage.LOCK):
                connect = state.connect
                if connect is None:
                    return OperationResult.failure(
                        code="CONNECT_STATE_INCONSISTENT",
                        message="Locked onboarding missing connect snapshot",
                        root_cause_hint="Onboarding state is inconsistent after lock",
                        safe_retry=False,
                        stop_condition="Inspect onboarding_runs row",
                    )
                return OperationResult.warning(
                    summary="Connect skipped; workspace already locked",
                    data=connect,
                    next_actions=("Run ofw ingest run",),
                )
            if (
                state is not None
                and stage_at_least(state.stage, OnboardingStage.CONNECT)
                and state.connect is not None
            ):
                return OperationResult.warning(
                    summary="Connect already completed; stage unchanged",
                    data=state.connect,
                    next_actions=("Run ofw onboard locate",),
                )

        reports: tuple[ConnectorCapabilityReport, ...] = (
            FixtureGitHubClient(Path(".")).capability_report(),
            claude.report(),
            cursor.report(),
            notes_capability_report(),
        )
        now = datetime.now(tz=UTC)
        try:
            with self._database.write() as conn:
                state = self._require_state(conn, workspace_id)
                if stage_at_least(state.stage, OnboardingStage.LOCK):
                    raise OnboardingTransactionError(
                        code="CONNECT_AFTER_LOCK",
                        message="Connect refused after lock",
                        root_cause_hint="Stage advanced concurrently to lock",
                        safe_retry=False,
                        stop_condition="Use ingest or relock instead of connect",
                    )
                if (
                    stage_at_least(state.stage, OnboardingStage.CONNECT)
                    and state.connect is not None
                ):
                    raise OnboardingTransactionError(
                        code="CONNECT_ALREADY_DONE",
                        message="Connect already completed",
                        root_cause_hint="Stage advanced concurrently",
                        safe_retry=True,
                        stop_condition="Proceed to locate",
                    )

                connect = ConnectStageData(reports=reports)
                next_stage = (
                    state.stage
                    if stage_at_least(state.stage, OnboardingStage.CONNECT)
                    else OnboardingStage.CONNECT
                )
                updated = OnboardingState(
                    run_id=state.run_id,
                    workspace_id=workspace_id,
                    stage=next_stage,
                    connect=connect,
                    locate=state.locate,
                    lock=state.lock,
                    updated_at=now,
                )
                self._onboarding.save(conn, updated)
                for report in reports:
                    kind = _connector_to_source_kind(report.connector_kind)
                    self._sources.upsert_source(
                        conn,
                        workspace_id=workspace_id,
                        kind=kind,
                        slug=report.connector_kind.value,
                        display_name=report.connector_kind.value,
                        capability=report,
                        root_path=None,
                        created_at=now,
                    )
        except OnboardingTransactionError as exc:
            if exc.code == "CONNECT_AFTER_LOCK":
                with self._database.read() as conn:
                    state = self._onboarding.get_active(conn, workspace_id)
                if state is not None and state.connect is not None:
                    return OperationResult.warning(
                        summary="Connect skipped; workspace already locked",
                        data=state.connect,
                        next_actions=("Run ofw ingest run",),
                    )
            if exc.code == "CONNECT_ALREADY_DONE":
                with self._database.read() as conn:
                    state = self._onboarding.get_active(conn, workspace_id)
                if state is not None and state.connect is not None:
                    return OperationResult.warning(
                        summary="Connect already completed; stage unchanged",
                        data=state.connect,
                        next_actions=("Run ofw onboard locate",),
                    )
            return exc.to_operation_result()
        except DomainError as exc:
            return exc.to_operation_result()
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        return OperationResult.success(
            summary=f"Connected {len(reports)} capability stubs",
            data=connect,
            next_actions=("Run ofw onboard locate",),
        )

    def run_locate(
        self, workspace_id: WorkspaceId, fixture_root: Path
    ) -> OperationResult[LocateStageData]:
        with self._database.read() as conn:
            state = self._onboarding.get_active(conn, workspace_id)
            if state is None or state.connect is None:
                return OperationResult.failure(
                    code="LOCATE_PRECONDITION",
                    message="Connect stage must complete before locate",
                    root_cause_hint="Run onboard connect first",
                    safe_retry=True,
                    stop_condition="Complete connect stage",
                )
            if stage_at_least(state.stage, OnboardingStage.LOCK):
                if state.locate is None:
                    return OperationResult.failure(
                        code="LOCATE_STATE_INCONSISTENT",
                        message="Locked onboarding missing locate snapshot",
                        root_cause_hint="Onboarding state is inconsistent after lock",
                        safe_retry=False,
                        stop_condition="Inspect onboarding_runs row",
                    )
                return OperationResult.warning(
                    summary="Locate skipped; locked boundaries preserved",
                    data=state.locate,
                    next_actions=("Run ofw onboard lock to relock or ofw ingest run",),
                )

        candidates = scan_fixture_root(fixture_root)
        if len(candidates) < 2:
            return OperationResult.failure(
                code="LOCATE_INSUFFICIENT",
                message="Scanner found fewer than two boundary candidates",
                root_cause_hint="Fixture root must contain at least two repos with pyproject.toml",
                safe_retry=False,
                stop_condition="Add repos under fixtures/tiny-system",
            )

        now = datetime.now(tz=UTC)
        locate = LocateStageData(
            candidates=candidates,
            fixture_root=str(fixture_root.resolve()),
        )
        try:
            with self._database.write() as conn:
                state = self._require_state(conn, workspace_id)
                if state.connect is None:
                    raise OnboardingTransactionError(
                        code="LOCATE_PRECONDITION",
                        message="Connect stage must complete before locate",
                        root_cause_hint="Run onboard connect first",
                        safe_retry=True,
                        stop_condition="Complete connect stage",
                    )
                if stage_at_least(state.stage, OnboardingStage.LOCK):
                    raise OnboardingTransactionError(
                        code="LOCATE_AFTER_LOCK",
                        message="Locate refused after lock",
                        root_cause_hint="Stage advanced concurrently to lock",
                        safe_retry=False,
                        stop_condition="Use relock instead of locate",
                    )

                for candidate in candidates:
                    existing = self._boundaries.get_by_slug(conn, workspace_id, candidate.slug)
                    preserved_manifest = existing.manifest if existing is not None else None
                    boundary_id = existing.id if existing is not None else None
                    created_at = existing.created_at if existing is not None else now
                    self._boundaries.upsert_boundary(
                        conn,
                        workspace_id=workspace_id,
                        name=candidate.name,
                        slug=candidate.slug,
                        component_paths=candidate.component_paths,
                        manifest=preserved_manifest,
                        created_at=created_at,
                        boundary_id=boundary_id,
                    )
                next_stage = (
                    state.stage
                    if stage_at_least(state.stage, OnboardingStage.LOCATE)
                    else OnboardingStage.LOCATE
                )
                updated = OnboardingState(
                    run_id=state.run_id,
                    workspace_id=workspace_id,
                    stage=next_stage,
                    connect=state.connect,
                    locate=locate,
                    lock=state.lock,
                    updated_at=now,
                )
                self._onboarding.save(conn, updated)
        except OnboardingTransactionError as exc:
            if exc.code == "LOCATE_AFTER_LOCK":
                with self._database.read() as conn:
                    state = self._onboarding.get_active(conn, workspace_id)
                if state is not None and state.locate is not None:
                    return OperationResult.warning(
                        summary="Locate skipped; locked boundaries preserved",
                        data=state.locate,
                        next_actions=("Run ofw onboard lock to relock or ofw ingest run",),
                    )
            return exc.to_operation_result()
        except DomainError as exc:
            return exc.to_operation_result()
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        return OperationResult.success(
            summary=f"Proposed {len(candidates)} boundaries",
            data=locate,
            next_actions=("Run ofw onboard lock for each boundary",),
        )

    def run_lock(
        self,
        workspace_id: WorkspaceId,
        requests: tuple[LockBoundaryRequest, ...],
    ) -> OperationResult[LockStageData]:
        with self._database.read() as conn:
            state = self._onboarding.get_active(conn, workspace_id)
            if state is None or state.locate is None:
                return OperationResult.failure(
                    code="LOCK_PRECONDITION",
                    message="Locate stage must complete before lock",
                    root_cause_hint="Run onboard locate first",
                    safe_retry=True,
                    stop_condition="Complete locate stage",
                )
            for request in requests:
                boundary = self._boundaries.get_by_slug(conn, workspace_id, request.candidate_slug)
                if boundary is None:
                    return OperationResult.failure(
                        code="LOCK_UNKNOWN_BOUNDARY",
                        message=f"Unknown boundary slug: {request.candidate_slug}",
                        root_cause_hint="Slug must match a locate candidate",
                        safe_retry=True,
                        stop_condition="Use a slug from locate output",
                    )

        now = datetime.now(tz=UTC)
        try:
            with self._database.write() as conn:
                state = self._require_state(conn, workspace_id)
                if state.locate is None:
                    raise OnboardingTransactionError(
                        code="LOCK_PRECONDITION",
                        message="Locate stage must complete before lock",
                        root_cause_hint="Run onboard locate first",
                        safe_retry=True,
                        stop_condition="Complete locate stage",
                    )
                for request in requests:
                    boundary = self._boundaries.get_by_slug(
                        conn, workspace_id, request.candidate_slug
                    )
                    if boundary is None:
                        raise OnboardingTransactionError(
                            code="LOCK_UNKNOWN_BOUNDARY",
                            message=f"Unknown boundary slug: {request.candidate_slug}",
                            root_cause_hint="Slug must match a locate candidate",
                            safe_retry=True,
                            stop_condition="Use a slug from locate output",
                        )
                    owner_ids = self._resolve_owner_ids(conn, workspace_id, request, now)
                    version = self._next_manifest_version(boundary.manifest)
                    manifest = BoundaryManifest(
                        version=version,
                        purpose=request.purpose,
                        system_shape=request.system_shape,
                        owner_identity_ids=owner_ids,
                        primary_kpi=request.primary_kpi,
                        source_authorities=request.source_authorities,
                        exclusions=request.exclusions,
                        locked_at=now,
                    )
                    self._boundaries.upsert_boundary(
                        conn,
                        workspace_id=workspace_id,
                        name=boundary.name,
                        slug=boundary.slug,
                        component_paths=boundary.component_paths,
                        manifest=manifest,
                        created_at=boundary.created_at,
                        boundary_id=boundary.id,
                    )
                    locked_boundary = self._boundaries.get_by_id(conn, boundary.id)
                    if locked_boundary is not None:
                        self._coverage.seed_for_boundary(conn, locked_boundary)
                lock_data = self._snapshot_locked_boundaries(conn, workspace_id)
                updated = OnboardingState(
                    run_id=state.run_id,
                    workspace_id=workspace_id,
                    stage=OnboardingStage.LOCK,
                    connect=state.connect,
                    locate=state.locate,
                    lock=lock_data,
                    updated_at=now,
                )
                self._onboarding.save(conn, updated)
        except OnboardingTransactionError as exc:
            return exc.to_operation_result()
        except DomainError as exc:
            return exc.to_operation_result()
        except sqlite3.Error as exc:
            return map_sqlite_error(exc).to_operation_result()

        locked_count = len(lock_data.locked_boundary_ids)
        return OperationResult.success(
            summary=f"Locked {len(requests)} boundaries; {locked_count} total locked",
            data=lock_data,
            next_actions=("Run ofw ingest run",),
        )

    def refuse_extraction_without_lock(self, workspace_id: WorkspaceId) -> OperationResult[None]:
        with self._database.read() as conn:
            if not self._boundaries.has_locked_boundary(conn, workspace_id):
                return OperationResult.failure(
                    code="EXTRACT_BEFORE_LOCK",
                    message="Claim extraction requires at least one locked boundary",
                    root_cause_hint="Complete onboard lock before extraction",
                    safe_retry=True,
                    stop_condition="Lock a boundary manifest",
                    next_actions=("Run ofw onboard lock",),
                )
        return OperationResult.success(
            summary="Boundary lock present; extraction may proceed in later waves",
            data=None,
        )

    def _snapshot_locked_boundaries(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> LockStageData:
        boundaries = self._boundaries.list_boundaries(conn, workspace_id)
        locked_ids: list[BoundaryId] = []
        manifests: list[BoundaryManifest] = []
        for boundary in boundaries:
            if boundary.manifest is None:
                continue
            locked_ids.append(boundary.id)
            manifests.append(boundary.manifest)
        return LockStageData(
            locked_boundary_ids=tuple(locked_ids),
            manifests=tuple(manifests),
        )

    def _next_manifest_version(self, manifest: BoundaryManifest | None) -> ManifestVersion:
        if manifest is None:
            return ManifestVersion(1)
        return ManifestVersion(manifest.version + 1)

    def _resolve_owner_ids(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        request: LockBoundaryRequest,
        created_at: datetime,
    ) -> tuple[IdentityId, ...]:
        resolved: list[IdentityId] = list(request.owner_identity_ids)
        for name in request.owner_display_names:
            existing = self._workspaces.find_identity_by_display_name(conn, workspace_id, name)
            if existing is not None:
                if existing.id not in resolved:
                    resolved.append(existing.id)
                continue
            created = self._workspaces.create_identity(
                conn,
                workspace_id=workspace_id,
                kind=IdentityKind.HUMAN,
                display_name=name,
                created_at=created_at,
            )
            resolved.append(created.id)
        return tuple(resolved)

    def _require_state(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> OnboardingState:
        state = self._onboarding.get_active(conn, workspace_id)
        if state is None:
            run_id = OnboardingRunId(str(uuid4()))
            now = datetime.now(tz=UTC)
            state = OnboardingState(
                run_id=run_id,
                workspace_id=workspace_id,
                stage=OnboardingStage.WORKSPACE,
                updated_at=now,
            )
            self._onboarding.save(conn, state)
        return state


def _connector_to_source_kind(kind: ConnectorKind) -> SourceKind:
    mapping = {
        ConnectorKind.GITHUB: SourceKind.GITHUB,
        ConnectorKind.CLAUDE_CODE: SourceKind.CLAUDE_CODE,
        ConnectorKind.CURSOR: SourceKind.CURSOR,
        ConnectorKind.EXPERT_NOTES: SourceKind.EXPERT_NOTES,
    }
    return mapping[kind]
