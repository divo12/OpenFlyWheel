"""Background job queue repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from openflywheel.contracts.enums import BackgroundJobKind, BackgroundJobStatus
from openflywheel.contracts.ids import BackgroundJobId, WorkspaceId
from openflywheel.contracts.jobs import MAX_JOB_ATTEMPTS, BackgroundJobRecord, JobLease
from openflywheel.store.sqlite_access import cell_int, cell_optional_str, cell_str, fetch_one_row


class BackgroundJobRepository(Protocol):
    def enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: BackgroundJobKind,
        payload_json: str,
        created_at: datetime,
        job_id: BackgroundJobId | None = None,
    ) -> BackgroundJobRecord: ...

    def acquire_lease(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        lease_seconds: int,
        now: datetime,
    ) -> JobLease | None: ...

    def complete_job(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: BackgroundJobId,
        now: datetime,
    ) -> None: ...

    def fail_job(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: BackgroundJobId,
        now: datetime,
        retryable: bool = True,
    ) -> None: ...

    def get_job(
        self, conn: sqlite3.Connection, job_id: BackgroundJobId
    ) -> BackgroundJobRecord | None: ...


class SqliteBackgroundJobRepository:
    def enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: WorkspaceId,
        kind: BackgroundJobKind,
        payload_json: str,
        created_at: datetime,
        job_id: BackgroundJobId | None = None,
    ) -> BackgroundJobRecord:
        jid = job_id or BackgroundJobId(str(uuid4()))
        conn.execute(
            """
            INSERT INTO background_jobs
            (id, workspace_id, kind, payload_json, status, lease_owner, lease_expires_at,
             retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
            """,
            (
                str(jid),
                str(workspace_id),
                kind.value,
                payload_json,
                BackgroundJobStatus.PENDING.value,
                created_at.isoformat(),
                created_at.isoformat(),
            ),
        )
        return BackgroundJobRecord(
            id=jid,
            workspace_id=workspace_id,
            kind=kind,
            payload_json=payload_json,
            status=BackgroundJobStatus.PENDING,
            lease_owner=None,
            lease_expires_at=None,
            retry_count=0,
            created_at=created_at,
            updated_at=created_at,
        )

    def acquire_lease(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        lease_seconds: int,
        now: datetime,
    ) -> JobLease | None:
        row = fetch_one_row(
            conn,
            """
            SELECT id, status FROM background_jobs
            WHERE retry_count < ?
              AND (
                status = ?
                OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (
                MAX_JOB_ATTEMPTS,
                BackgroundJobStatus.PENDING.value,
                BackgroundJobStatus.LEASED.value,
                now.isoformat(),
            ),
        )
        if row is None:
            return None
        job_id = BackgroundJobId(cell_str(row, "id"))
        prior_status = cell_str(row, "status")
        expires = now + timedelta(seconds=lease_seconds)
        updated = conn.execute(
            """
            UPDATE background_jobs
            SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                BackgroundJobStatus.LEASED.value,
                owner,
                expires.isoformat(),
                now.isoformat(),
                str(job_id),
                prior_status,
            ),
        ).rowcount
        if updated != 1:
            return None
        return JobLease(job_id=job_id, owner=owner, expires_at=expires)

    def complete_job(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: BackgroundJobId,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            UPDATE background_jobs
            SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (BackgroundJobStatus.COMPLETED.value, now.isoformat(), str(job_id)),
        )

    def fail_job(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: BackgroundJobId,
        now: datetime,
        retryable: bool = True,
    ) -> None:
        job = self.get_job(conn, job_id)
        if job is None:
            return
        next_retry = job.retry_count + 1
        if retryable and next_retry < MAX_JOB_ATTEMPTS:
            conn.execute(
                """
                UPDATE background_jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    retry_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    BackgroundJobStatus.PENDING.value,
                    next_retry,
                    now.isoformat(),
                    str(job_id),
                ),
            )
            return
        conn.execute(
            """
            UPDATE background_jobs
            SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                retry_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                BackgroundJobStatus.FAILED.value,
                next_retry,
                now.isoformat(),
                str(job_id),
            ),
        )

    def get_job(
        self, conn: sqlite3.Connection, job_id: BackgroundJobId
    ) -> BackgroundJobRecord | None:
        raw = fetch_one_row(
            conn,
            """
            SELECT id, workspace_id, kind, payload_json, status, lease_owner,
                   lease_expires_at, retry_count, created_at, updated_at
            FROM background_jobs WHERE id = ?
            """,
            (str(job_id),),
        )
        if raw is None:
            return None
        lease_expires = cell_optional_str(raw, "lease_expires_at")
        lease_owner = cell_optional_str(raw, "lease_owner")
        return BackgroundJobRecord(
            id=BackgroundJobId(cell_str(raw, "id")),
            workspace_id=WorkspaceId(cell_str(raw, "workspace_id")),
            kind=BackgroundJobKind(cell_str(raw, "kind")),
            payload_json=cell_str(raw, "payload_json"),
            status=BackgroundJobStatus(cell_str(raw, "status")),
            lease_owner=lease_owner,
            lease_expires_at=datetime.fromisoformat(lease_expires) if lease_expires else None,
            retry_count=cell_int(raw, "retry_count"),
            created_at=datetime.fromisoformat(cell_str(raw, "created_at")),
            updated_at=datetime.fromisoformat(cell_str(raw, "updated_at")),
        )


def payload_to_json(payload: object) -> str:
    from openflywheel.contracts.pydantic_json import PydanticJsonModel

    if isinstance(payload, PydanticJsonModel):
        return payload.model_dump_json()
    msg = "payload must be a pydantic model"
    raise TypeError(msg)
