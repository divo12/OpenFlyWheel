"""Onboarding state repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import OnboardingStage
from openflywheel.contracts.ids import OnboardingRunId, WorkspaceId
from openflywheel.contracts.onboarding import (
    ConnectStageData,
    LocateStageData,
    LockStageData,
    OnboardingState,
)
from openflywheel.store.rows import OnboardingRow
from openflywheel.store.serde import model_from_json, model_to_json
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_one_row,
)


class OnboardingRepository(Protocol):
    def get_active(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> OnboardingState | None: ...

    def save(self, conn: sqlite3.Connection, state: OnboardingState) -> OnboardingState: ...


def _row_to_state(row: OnboardingRow) -> OnboardingState:
    connect = model_from_json(ConnectStageData, row.connect_json) if row.connect_json else None
    locate = model_from_json(LocateStageData, row.locate_json) if row.locate_json else None
    lock = model_from_json(LockStageData, row.lock_json) if row.lock_json else None
    return OnboardingState(
        run_id=OnboardingRunId(row.id),
        workspace_id=WorkspaceId(row.workspace_id),
        stage=OnboardingStage(row.stage),
        connect=connect,
        locate=locate,
        lock=lock,
        updated_at=datetime.fromisoformat(row.updated_at),
    )


class SqliteOnboardingRepository:
    def get_active(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> OnboardingState | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT * FROM onboarding_runs
            WHERE workspace_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(workspace_id),),
        )
        if raw is None:
            return None
        row = OnboardingRow(
            id=cell_str(raw, "id"),
            workspace_id=cell_str(raw, "workspace_id"),
            stage=cell_str(raw, "stage"),
            connect_json=cell_str(raw, "connect_json") if raw["connect_json"] else None,
            locate_json=cell_str(raw, "locate_json") if raw["locate_json"] else None,
            lock_json=cell_str(raw, "lock_json") if raw["lock_json"] else None,
            updated_at=cell_str(raw, "updated_at"),
        )
        return _row_to_state(row)

    def save(self, conn: sqlite3.Connection, state: OnboardingState) -> OnboardingState:
        connect_json = model_to_json(state.connect) if state.connect else None
        locate_json = model_to_json(state.locate) if state.locate else None
        lock_json = model_to_json(state.lock) if state.lock else None
        existing = conn.execute(
            "SELECT id FROM onboarding_runs WHERE id = ?", (str(state.run_id),)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO onboarding_runs
                (id, workspace_id, stage, connect_json, locate_json, lock_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(state.run_id),
                    str(state.workspace_id),
                    state.stage.value,
                    connect_json,
                    locate_json,
                    lock_json,
                    state.updated_at.isoformat(),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE onboarding_runs
                SET stage = ?, connect_json = ?, locate_json = ?, lock_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state.stage.value,
                    connect_json,
                    locate_json,
                    lock_json,
                    state.updated_at.isoformat(),
                    str(state.run_id),
                ),
            )
        return state

    def create_run(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        updated_at: datetime,
    ) -> OnboardingState:
        state = OnboardingState(
            run_id=OnboardingRunId(str(uuid4())),
            workspace_id=workspace_id,
            stage=OnboardingStage.WORKSPACE,
            updated_at=updated_at,
        )
        return self.save(conn, state)
