"""Workspace repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import DeploymentMode, IdentityKind, VisibilityLevel
from openflywheel.contracts.identity import IdentityRecord
from openflywheel.contracts.ids import IdentityId, WorkspaceId
from openflywheel.contracts.workspace import WorkspacePolicy, WorkspaceRecord
from openflywheel.store.rows import IdentityRow, WorkspaceRow
from openflywheel.store.serde import model_from_json, model_to_json, tuple_from_json, tuple_to_json
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_all_rows,
    fetch_one_row,
)


class WorkspaceRepository(Protocol):
    def create_workspace(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        deployment_mode: DeploymentMode,
        policy: WorkspacePolicy,
        admin_identity_ids: tuple[IdentityId, ...],
        created_at: datetime,
        workspace_id: WorkspaceId | None = None,
    ) -> WorkspaceRecord: ...

    def set_admin_identity_ids(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        admin_identity_ids: tuple[IdentityId, ...],
    ) -> WorkspaceRecord: ...

    def get_workspace(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> WorkspaceRecord | None: ...

    def create_identity(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: IdentityKind,
        display_name: str,
        created_at: datetime,
    ) -> IdentityRecord: ...


def _row_to_workspace(row: WorkspaceRow) -> WorkspaceRecord:
    return WorkspaceRecord(
        id=WorkspaceId(row.id),
        name=row.name,
        deployment_mode=DeploymentMode(row.deployment_mode),
        policy=model_from_json(WorkspacePolicy, row.policy_json),
        admin_identity_ids=tuple(
            IdentityId(i) for i in tuple_from_json(row.admin_identity_ids_json)
        ),
        created_at=datetime.fromisoformat(row.created_at),
    )


def _row_to_identity(row: IdentityRow) -> IdentityRecord:
    from openflywheel.contracts.acl import AclLabel

    return IdentityRecord(
        id=IdentityId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        kind=IdentityKind(row.kind),
        display_name=row.display_name,
        acl=model_from_json(AclLabel, row.acl_json),
        created_at=datetime.fromisoformat(row.created_at),
    )


class SqliteWorkspaceRepository:
    def create_workspace(
        self,
        conn: sqlite3.Connection,
        *,
        name: str,
        deployment_mode: DeploymentMode,
        policy: WorkspacePolicy,
        admin_identity_ids: tuple[IdentityId, ...],
        created_at: datetime,
        workspace_id: WorkspaceId | None = None,
    ) -> WorkspaceRecord:
        workspace_id = workspace_id or WorkspaceId(str(uuid4()))
        row = WorkspaceRow(
            id=workspace_id,
            name=name,
            deployment_mode=deployment_mode.value,
            policy_json=model_to_json(policy),
            admin_identity_ids_json=tuple_to_json(tuple(str(i) for i in admin_identity_ids)),
            created_at=created_at.isoformat(),
        )
        conn.execute(
            """
            INSERT INTO workspaces (id, name, deployment_mode, policy_json, admin_identity_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.id,
                row.name,
                row.deployment_mode,
                row.policy_json,
                row.admin_identity_ids_json,
                row.created_at,
            ),
        )
        return _row_to_workspace(row)

    def set_admin_identity_ids(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        admin_identity_ids: tuple[IdentityId, ...],
    ) -> WorkspaceRecord:
        conn.execute(
            "UPDATE workspaces SET admin_identity_ids_json = ? WHERE id = ?",
            (tuple_to_json(tuple(str(i) for i in admin_identity_ids)), str(workspace_id)),
        )
        record = self.get_workspace(conn, workspace_id)
        if record is None:
            from openflywheel.store.exceptions import StoreNotFoundError

            raise StoreNotFoundError(
                code="WORKSPACE_NOT_FOUND",
                message=f"Workspace {workspace_id} not found after update",
                root_cause_hint="Workspace row missing after admin assignment",
                safe_retry=False,
                stop_condition="Repair workspace database manually",
            )
        return record

    def get_workspace(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> WorkspaceRecord | None:
        raw = fetch_one_row(conn, "SELECT * FROM workspaces WHERE id = ?", (str(workspace_id),))
        if raw is None:
            return None
        row = WorkspaceRow(
            id=cell_str(raw, "id"),
            name=cell_str(raw, "name"),
            deployment_mode=cell_str(raw, "deployment_mode"),
            policy_json=cell_str(raw, "policy_json"),
            admin_identity_ids_json=cell_str(raw, "admin_identity_ids_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_workspace(row)

    def create_identity(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: IdentityKind,
        display_name: str,
        created_at: datetime,
    ) -> IdentityRecord:
        from openflywheel.contracts.acl import AclLabel

        identity_id = IdentityId(str(uuid4()))
        acl = AclLabel(visibility=VisibilityLevel.INTERNAL)
        row = IdentityRow(
            id=identity_id,
            workspace_id=str(workspace_id),
            kind=kind.value,
            display_name=display_name,
            acl_json=model_to_json(acl),
            created_at=created_at.isoformat(),
        )
        conn.execute(
            """
            INSERT INTO identities (id, workspace_id, kind, display_name, acl_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row.id, row.workspace_id, row.kind, row.display_name, row.acl_json, row.created_at),
        )
        return _row_to_identity(row)

    def find_identity_by_display_name(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        display_name: str,
    ) -> IdentityRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM identities
            WHERE workspace_id = ? AND display_name = ?
            LIMIT 1
            """,
            (str(workspace_id), display_name),
        )
        if raw is None:
            return None
        row = IdentityRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            kind=cell_str(raw, "kind"),
            display_name=cell_str(raw, "display_name"),
            acl_json=cell_str(raw, "acl_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_identity(row)

    def get_identity(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
    ) -> IdentityRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM identities
            WHERE workspace_id = ? AND id = ?
            LIMIT 1
            """,
            (str(workspace_id), str(identity_id)),
        )
        if raw is None:
            return None
        row = IdentityRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            kind=cell_str(raw, "kind"),
            display_name=cell_str(raw, "display_name"),
            acl_json=cell_str(raw, "acl_json"),
            created_at=cell_str(raw, "created_at"),
        )
        return _row_to_identity(row)

    def list_identities(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[IdentityRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM identities WHERE workspace_id = ? ORDER BY created_at",
            (str(workspace_id),),
        )
        result: list[IdentityRecord] = []
        for raw in rows:
            row = IdentityRow(
                id=cell_str(raw, "id"),
                workspace_id=cell_str(raw, "workspace_id"),
                kind=cell_str(raw, "kind"),
                display_name=cell_str(raw, "display_name"),
                acl_json=cell_str(raw, "acl_json"),
                created_at=cell_str(raw, "created_at"),
            )
            result.append(_row_to_identity(row))
        return tuple(result)
