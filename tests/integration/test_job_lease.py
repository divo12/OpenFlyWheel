"""Background job lease, retry, and worker behavior tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from tests.agent_helpers import episode_request, setup_agent_pipeline
from tests.book_helpers import setup_book_pipeline

from openflywheel.application.agent_worker import BackgroundWorkerService
from openflywheel.application.recursion import background_scope, recursion_disabled
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.enums import BackgroundJobKind, BackgroundJobStatus, PlatformKind
from openflywheel.contracts.ids import BackgroundJobId, WorkspaceId
from openflywheel.contracts.jobs import MAX_JOB_ATTEMPTS, TranscriptExtractPayload
from openflywheel.store.repos.job_repo import SqliteBackgroundJobRepository


@pytest.fixture
def transcript_root() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "agent-transcripts"


def _enqueue_extract(
    conn,
    *,
    workspace_id: WorkspaceId,
    episode_id: str,
    created_at: datetime,
    job_id: BackgroundJobId | None = None,
) -> BackgroundJobId:
    repo = SqliteBackgroundJobRepository()
    payload = TranscriptExtractPayload(
        episode_id=episode_id,
        session_id=str(uuid4()),
        boundary_id=None,
        disable_recursion=True,
    )
    record = repo.enqueue(
        conn,
        workspace_id=workspace_id,
        kind=BackgroundJobKind.TRANSCRIPT_EXTRACT,
        payload_json=payload.model_dump_json(),
        created_at=created_at,
        job_id=job_id,
    )
    return record.id


def test_expired_leased_job_is_reacquired(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    now = datetime.now(tz=UTC)
    expired = now - timedelta(minutes=5)
    job_id = BackgroundJobId("job-expired-lease")

    with database.write() as conn:
        _enqueue_extract(
            conn,
            workspace_id=workspace_id,
            episode_id="missing-episode",
            created_at=now,
            job_id=job_id,
        )
        conn.execute(
            """
            UPDATE background_jobs
            SET status = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                BackgroundJobStatus.LEASED.value,
                "stale-worker",
                expired.isoformat(),
                expired.isoformat(),
                str(job_id),
            ),
        )

    repo = SqliteBackgroundJobRepository()
    with database.write() as conn:
        lease = repo.acquire_lease(conn, owner="fresh-worker", lease_seconds=30, now=now)
    assert lease is not None
    assert lease.job_id == job_id
    assert lease.owner == "fresh-worker"


def test_fail_job_increments_retry_count(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    now = datetime.now(tz=UTC)
    job_id = BackgroundJobId("job-retry-inc")

    with database.write() as conn:
        _enqueue_extract(
            conn,
            workspace_id=workspace_id,
            episode_id="missing-episode",
            created_at=now,
            job_id=job_id,
        )

    repo = SqliteBackgroundJobRepository()
    with database.write() as conn:
        repo.fail_job(conn, job_id=job_id, now=now, retryable=True)
        job = repo.get_job(conn, job_id)
    assert job is not None
    assert job.status == BackgroundJobStatus.PENDING
    assert job.retry_count == 1


def test_missing_episode_never_completes(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    now = datetime.now(tz=UTC)
    job_id = BackgroundJobId("job-missing-episode")

    with database.write() as conn:
        _enqueue_extract(
            conn,
            workspace_id=workspace_id,
            episode_id="does-not-exist",
            created_at=now,
            job_id=job_id,
        )

    worker = BackgroundWorkerService(database)
    result = worker.process_next()
    assert result.error is not None
    assert result.error.code == "WORKER_EPISODE_MISSING"

    repo = SqliteBackgroundJobRepository()
    with database.read() as conn:
        job = repo.get_job(conn, job_id)
    assert job is not None
    assert job.status == BackgroundJobStatus.FAILED
    assert job.status != BackgroundJobStatus.COMPLETED


def test_max_attempts_marks_failed(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    now = datetime.now(tz=UTC)
    job_id = BackgroundJobId("job-max-attempts")

    with database.write() as conn:
        _enqueue_extract(
            conn,
            workspace_id=workspace_id,
            episode_id="does-not-exist",
            created_at=now,
            job_id=job_id,
        )

    repo = SqliteBackgroundJobRepository()
    for _ in range(MAX_JOB_ATTEMPTS):
        with database.write() as conn:
            job = repo.get_job(conn, job_id)
            assert job is not None
            if job.status == BackgroundJobStatus.FAILED:
                break
            repo.fail_job(conn, job_id=job_id, now=now, retryable=True)

    with database.read() as conn:
        job = repo.get_job(conn, job_id)
    assert job is not None
    assert job.status == BackgroundJobStatus.FAILED
    assert job.retry_count == MAX_JOB_ATTEMPTS


def test_worker_extracts_from_admitted_episode_not_transcript_file(
    workspace_home, fixture_root, transcript_root, tmp_path: Path
) -> None:
    workspace_id, book, home = setup_agent_pipeline(workspace_home, fixture_root)
    local_transcript = tmp_path / "session.jsonl"
    local_transcript.write_text(
        (transcript_root / "claude-session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-content-only",
        transcript_path=local_transcript,
        agent_home=str(tmp_path),
        project_root=str(fixture_root),
    )
    recorded = book.episode_record(request)
    assert recorded.error is None
    assert recorded.data is not None

    local_transcript.unlink()

    database = WorkspaceService().load_database(home)
    worker = BackgroundWorkerService(database)
    processed = worker.process_next()
    assert processed.error is None
    assert processed.data is not None
    assert processed.data >= 1

    with database.read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM proposals WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
    assert row is not None
    assert int(row["cnt"]) >= 1


def test_recursion_context_resets_after_worker(
    workspace_home, fixture_root, transcript_root
) -> None:
    workspace_id, book, home = setup_agent_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    worker = BackgroundWorkerService(database)

    assert recursion_disabled() is False
    with background_scope():
        assert recursion_disabled() is True
    assert recursion_disabled() is False

    first = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-recursion-a",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )
    first_result = book.episode_record(first)
    assert first_result.error is None
    assert first_result.data is not None
    assert first_result.data.job_scheduled is True

    processed = worker.process_next()
    assert processed.error is None
    assert recursion_disabled() is False

    second = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="sess-recursion-b",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )
    second_result = book.episode_record(second)
    assert second_result.error is None
    assert second_result.data is not None
    assert second_result.data.job_scheduled is True
