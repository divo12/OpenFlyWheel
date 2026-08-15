"""Resolve workspace identities before any read or write."""

from __future__ import annotations

import sqlite3

from openflywheel.contracts.identity import IdentityRecord
from openflywheel.contracts.ids import IdentityId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.store.db import Database
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


class IdentityGate:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._workspaces = SqliteWorkspaceRepository()

    def resolve(
        self,
        *,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
    ) -> OperationResult[IdentityRecord]:
        with self._database.read() as conn:
            record = self._get_identity(conn, workspace_id, identity_id)
        if record is None:
            return OperationResult.failure(
                code="IDENTITY_UNKNOWN",
                message="Identity not found in workspace",
                root_cause_hint="Use a workspace identity id from onboarding",
                safe_retry=False,
                stop_condition="Provide X-OFW-Identity from workspace identities table",
            )
        return OperationResult.success(
            summary=f"Resolved identity {record.display_name}", data=record
        )

    def _get_identity(
        self,
        conn: sqlite3.Connection,
        workspace_id: WorkspaceId,
        identity_id: IdentityId,
    ) -> IdentityRecord | None:
        return self._workspaces.get_identity(conn, workspace_id, identity_id)
