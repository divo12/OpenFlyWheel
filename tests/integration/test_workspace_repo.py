"""Workspace repository integration tests."""

from datetime import UTC, datetime

from openflywheel.contracts.enums import DeploymentMode, IdentityKind, VisibilityLevel
from openflywheel.contracts.ids import WorkspaceId
from openflywheel.contracts.workspace import WorkspacePolicy
from openflywheel.store.db import ConnectionFactory, Database, DatabaseConfig
from openflywheel.store.migrate import apply_migrations
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


def test_workspace_repo_roundtrip(tmp_path) -> None:
    db_path = tmp_path / "book.sqlite"
    database = Database(ConnectionFactory(DatabaseConfig(path=db_path)))
    repo = SqliteWorkspaceRepository()
    now = datetime(2026, 8, 15, tzinfo=UTC)
    policy = WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL)

    with database.connection() as conn:
        apply_migrations(conn)
        ws = repo.create_workspace(
            conn,
            name="FixtureCo",
            deployment_mode=DeploymentMode.LOCAL,
            policy=policy,
            admin_identity_ids=tuple(),
            created_at=now,
            workspace_id=WorkspaceId("ws-1"),
        )
        admin = repo.create_identity(
            conn,
            workspace_id=ws.id,
            kind=IdentityKind.HUMAN,
            display_name="Admin",
            created_at=now,
        )
        ws = repo.set_admin_identity_ids(conn, ws.id, (admin.id,))
        loaded = repo.get_workspace(conn, ws.id)

    assert loaded is not None
    assert loaded.name == "FixtureCo"
    assert loaded.admin_identity_ids == (admin.id,)
