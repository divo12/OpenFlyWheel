"""Ingest application orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openflywheel.connectors.github.fixture import FixtureGitHubClient
from openflywheel.contracts.enums import SourceKind
from openflywheel.contracts.ids import WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.ingest.episode_service import EpisodeIngestService, IngestSummary
from openflywheel.store.db import Database
from openflywheel.store.exceptions import StoreNotFoundError
from openflywheel.store.repos.source_repo import SqliteSourceRepository


class IngestApplication:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._ingest = EpisodeIngestService(database)
        self._sources = SqliteSourceRepository()

    def run_fixture_ingest(
        self,
        *,
        workspace_id: WorkspaceId,
        fixture_root: Path,
        cli_excluded_paths: tuple[str, ...] = (),
    ) -> OperationResult[IngestSummary]:
        with self._database.read() as conn:
            source = self._sources.get_by_slug(conn, workspace_id, SourceKind.GITHUB.value)
            if source is None:
                return OperationResult.failure(
                    code="INGEST_NO_SOURCE",
                    message="GitHub source not configured",
                    root_cause_hint="Run onboard connect first",
                    safe_retry=True,
                    stop_condition="Configure github source via onboard connect",
                )

        self._configure_github_root(workspace_id, fixture_root)

        with self._database.read() as conn:
            source = self._sources.get_by_slug(conn, workspace_id, SourceKind.GITHUB.value)
            if source is None:
                return StoreNotFoundError(
                    code="INGEST_NO_SOURCE",
                    message="GitHub source not configured after update",
                    root_cause_hint="Source registration failed",
                    safe_retry=True,
                    stop_condition="Re-run onboard connect",
                ).to_operation_result()

        client = FixtureGitHubClient(fixture_root.resolve())
        return self._ingest.ingest_github_fixture(
            workspace_id=workspace_id,
            source_id=source.id,
            client=client,
            cli_excluded_paths=cli_excluded_paths,
        )

    def _configure_github_root(self, workspace_id: WorkspaceId, fixture_root: Path) -> None:
        now = datetime.now(tz=UTC)
        with self._database.write() as conn:
            source = self._sources.get_by_slug(conn, workspace_id, SourceKind.GITHUB.value)
            if source is None:
                raise StoreNotFoundError(
                    code="INGEST_NO_SOURCE",
                    message="GitHub source not configured",
                    root_cause_hint="Run onboard connect first",
                    safe_retry=True,
                    stop_condition="Configure github source via onboard connect",
                )
            self._sources.upsert_source(
                conn,
                workspace_id=workspace_id,
                kind=source.kind,
                slug=source.slug,
                display_name=source.display_name,
                capability=FixtureGitHubClient(fixture_root.resolve()).capability_report(),
                root_path=str(fixture_root.resolve()),
                created_at=now,
            )
