"""Workspace application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from openflywheel.application.workspace_config import WorkspaceConfigFile
from openflywheel.cli.paths import resolve_home
from openflywheel.contracts.enums import IdentityKind, VisibilityLevel
from openflywheel.contracts.ids import WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.workspace import (
    WorkspaceInitRequest,
    WorkspaceInitResult,
    WorkspacePolicy,
    WorkspaceRecord,
)
from openflywheel.store.db import ConnectionFactory, Database, DatabaseConfig
from openflywheel.store.migrate import apply_migrations
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


@dataclass(frozen=True)
class WorkspaceHandle:
    home: Path
    config: WorkspaceConfigFile
    database: Database


class WorkspaceService:
    def init_workspace(self, request: WorkspaceInitRequest) -> OperationResult[WorkspaceInitResult]:
        home = resolve_home(request.home)
        db_path = home / "book.sqlite"
        config_path = home / "workspace.json"

        if not request.force and (config_path.exists() or db_path.exists()):
            return OperationResult.failure(
                code="WORKSPACE_EXISTS",
                message=f"Workspace home already exists: {home}",
                root_cause_hint="Home contains workspace.json or book.sqlite",
                safe_retry=False,
                stop_condition="Pass --force to reinitialize explicitly",
                next_actions=(f"Use --force to overwrite {home}",),
            )

        if request.force and home.exists():
            for path in (config_path, db_path):
                if path.exists():
                    path.unlink()

        home.mkdir(parents=True, exist_ok=True)

        factory = ConnectionFactory(DatabaseConfig(path=db_path))
        database = Database(factory)
        workspace_repo = SqliteWorkspaceRepository()

        now = datetime.now(tz=UTC)
        policy = WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL, retention_days=365)
        workspace_id = WorkspaceId(str(uuid4()))

        with database.write() as conn:
            apply_migrations(conn)
            workspace = workspace_repo.create_workspace(
                conn,
                name=request.name,
                deployment_mode=request.deployment_mode,
                policy=policy,
                admin_identity_ids=tuple(),
                created_at=now,
                workspace_id=workspace_id,
            )
            admin = workspace_repo.create_identity(
                conn,
                workspace_id=workspace.id,
                kind=IdentityKind.HUMAN,
                display_name="admin",
                created_at=now,
            )
            workspace = workspace_repo.set_admin_identity_ids(conn, workspace.id, (admin.id,))

        config = WorkspaceConfigFile(
            workspace_id=workspace.id,
            name=request.name,
            home=str(home),
            database_path=str(db_path),
        )
        config_path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")

        result = WorkspaceInitResult(
            workspace_id=workspace.id,
            home=str(home),
            database_path=str(db_path),
        )
        return OperationResult.success(
            summary=f"Initialized workspace {request.name}",
            data=result,
            artifacts=(str(config_path), str(db_path)),
            next_actions=("Run ofw onboard",),
        )

    def open_workspace(self, home: str | Path) -> OperationResult[WorkspaceHandle]:
        resolved = resolve_home(str(home))
        config_path = resolved / "workspace.json"
        db_path = resolved / "book.sqlite"

        if not config_path.is_file():
            return OperationResult.failure(
                code="WORKSPACE_NOT_FOUND",
                message=f"Workspace config not found: {config_path}",
                root_cause_hint="Run ofw workspace init before onboarding",
                safe_retry=False,
                stop_condition="Initialize workspace home with workspace.json",
                next_actions=("Run ofw workspace init --home <path>",),
            )

        if not db_path.is_file():
            return OperationResult.failure(
                code="WORKSPACE_DB_MISSING",
                message=f"Workspace database not found: {db_path}",
                root_cause_hint="book.sqlite is missing from workspace home",
                safe_retry=False,
                stop_condition="Re-run workspace init or restore book.sqlite",
                next_actions=("Run ofw workspace init --home <path>",),
            )

        try:
            config = WorkspaceConfigFile.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except ValidationError as exc:
            return OperationResult.failure(
                code="WORKSPACE_CONFIG_INVALID",
                message="workspace.json failed validation",
                root_cause_hint=str(exc),
                safe_retry=False,
                stop_condition="Fix or regenerate workspace.json",
            )

        factory = ConnectionFactory(DatabaseConfig(path=db_path, ensure_parent=False))
        database = Database(factory)
        with database.write() as conn:
            apply_migrations(conn)

        return OperationResult.success(
            summary=f"Opened workspace {config.name}",
            data=WorkspaceHandle(home=resolved, config=config, database=database),
        )

    def load_database(self, home: Path) -> Database:
        opened = self.open_workspace(home)
        if opened.error is not None:
            msg = opened.error.message
            raise FileNotFoundError(msg)
        if opened.data is None:
            msg = "Workspace open returned no handle"
            raise FileNotFoundError(msg)
        return opened.data.database

    def read_config(self, home: Path) -> WorkspaceConfigFile:
        opened = self.open_workspace(home)
        if opened.error is not None:
            msg = opened.error.message
            raise FileNotFoundError(msg)
        if opened.data is None:
            msg = "Workspace open returned no handle"
            raise FileNotFoundError(msg)
        return opened.data.config

    def read_workspace(self, home: Path) -> WorkspaceRecord | None:
        opened = self.open_workspace(home)
        if opened.error is not None or opened.data is None:
            return None
        repo = SqliteWorkspaceRepository()
        with opened.data.database.read() as conn:
            return repo.get_workspace(conn, opened.data.config.workspace_id)
