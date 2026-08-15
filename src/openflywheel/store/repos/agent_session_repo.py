"""Agent session repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId, WorkspaceId
from openflywheel.store.sqlite_access import (
    cell_str,
    fetch_one_row,
)


class AgentSessionRecord:
    __slots__ = (
        "id",
        "workspace_id",
        "platform",
        "session_ref",
        "transcript_pointer",
        "created_at",
    )

    def __init__(
        self,
        *,
        id: AgentSessionId,
        workspace_id: WorkspaceId,
        platform: PlatformKind,
        session_ref: str,
        transcript_pointer: str,
        created_at: datetime,
    ) -> None:
        self.id = id
        self.workspace_id = workspace_id
        self.platform = platform
        self.session_ref = session_ref
        self.transcript_pointer = transcript_pointer
        self.created_at = created_at


class AgentSessionRepository(Protocol):
    def upsert_session(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        platform: PlatformKind,
        session_ref: str,
        transcript_pointer: str,
        created_at: datetime,
        session_id: AgentSessionId | None = None,
    ) -> AgentSessionRecord: ...

    def get_by_ref(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        platform: PlatformKind,
        session_ref: str,
    ) -> AgentSessionRecord | None: ...


class SqliteAgentSessionRepository:
    def upsert_session(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        platform: PlatformKind,
        session_ref: str,
        transcript_pointer: str,
        created_at: datetime,
        session_id: AgentSessionId | None = None,
    ) -> AgentSessionRecord:
        existing = conn.execute(
            """
            SELECT id FROM agent_sessions
            WHERE workspace_id = ? AND platform = ? AND session_ref = ?
            """,
            (str(workspace_id), platform.value, session_ref),
        ).fetchone()
        sid = (
            AgentSessionId(str(existing["id"]))
            if existing
            else (session_id or AgentSessionId(str(uuid4())))
        )
        if existing is not None:
            conn.execute(
                """
                UPDATE agent_sessions
                SET transcript_pointer = ?
                WHERE id = ?
                """,
                (transcript_pointer, str(sid)),
            )
        else:
            conn.execute(
                """
                INSERT INTO agent_sessions
                (id, workspace_id, platform, session_ref, transcript_pointer, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(sid),
                    str(workspace_id),
                    platform.value,
                    session_ref,
                    transcript_pointer,
                    created_at.isoformat(),
                ),
            )
        return AgentSessionRecord(
            id=sid,
            workspace_id=workspace_id,
            platform=platform,
            session_ref=session_ref,
            transcript_pointer=transcript_pointer,
            created_at=created_at,
        )

    def get_by_ref(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        platform: PlatformKind,
        session_ref: str,
    ) -> AgentSessionRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT id, workspace_id, platform, session_ref, transcript_pointer, created_at
            FROM agent_sessions
            WHERE workspace_id = ? AND platform = ? AND session_ref = ?
            """,
            (str(workspace_id), platform.value, session_ref),
        )
        if raw is None:
            return None
        return AgentSessionRecord(
            id=AgentSessionId(cell_str(raw, "id")),
            workspace_id=WorkspaceId(cell_str(raw, "workspace_id")),
            platform=PlatformKind(cell_str(raw, "platform")),
            session_ref=cell_str(raw, "session_ref"),
            transcript_pointer=cell_str(raw, "transcript_pointer"),
            created_at=datetime.fromisoformat(cell_str(raw, "created_at")),
        )
