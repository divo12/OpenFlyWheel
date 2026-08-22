"""Durable single-process scheduler and reconciliation heartbeat."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock
from typing import Protocol, cast

from pydantic import TypeAdapter, ValidationError

from ofw.contracts import HarnessRevisionId, Sha256Digest
from ofw.fit import FitPolicy

logger = logging.getLogger(__name__)


class JobKind(StrEnum):
    TRACE_SYNC = "trace_sync"
    MINE = "mine"
    EXPORT_GOOD_TRACES = "export_good_traces"
    EXPORT_BENCH_EVAL = "export_bench_eval"
    PROPOSE_MEMORY = "propose_memory"
    FIT = "fit"
    PROMOTE = "promote"
    POST_PROMOTION_MONITOR = "post_promotion_monitor"


class JobState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_OPTIONAL = "failed_optional"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class DependencyMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class FailureDisposition(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    OPTIONAL = "optional"


class EvidenceOrigin(StrEnum):
    PRODUCTION = "production"
    OFW_CONTROL_PLANE = "ofw_control_plane"


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class BlockerCode(StrEnum):
    DEPENDENCY_WAITING = "dependency_waiting"
    DEPENDENCY_FAILED = "dependency_failed"
    DEPENDENCY_INVALID = "dependency_invalid"
    POLICY_MISMATCH = "policy_mismatch"
    ACTIVE_MINE = "active_mine"
    ACTIVE_FIT = "active_fit"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"
    QUIET_HOURS = "quiet_hours"
    NO_WINNER = "no_winner"


class SchedulerErrorCode(StrEnum):
    DATABASE_ERROR = "database_error"
    STALE_HARNESS = "stale_harness"
    POLICY_MISMATCH = "policy_mismatch"
    JOB_NOT_FOUND = "job_not_found"
    INVALID_TRANSITION = "invalid_transition"
    LATE_COMPLETION = "late_completion"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HEARTBEAT_LEASE_HELD = "heartbeat_lease_held"
    HANDLER_FAILED = "handler_failed"
    HANDLER_MISSING = "handler_missing"
    LEASE_EXPIRED = "lease_expired"
    DEPENDENCY_FAILED = "dependency_failed"
    RESULT_INVALID = "result_invalid"


class SchedulerError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: SchedulerErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True, order=True)
class Money:
    micros: int

    def __post_init__(self) -> None:
        if self.micros < 0:
            raise ValueError("money cannot be negative")

    def __add__(self, other: Money) -> Money:
        return Money(self.micros + other.micros)

    def __sub__(self, other: Money) -> Money:
        if other.micros > self.micros:
            raise ValueError("money cannot be negative")
        return Money(self.micros - other.micros)


@dataclass(frozen=True, slots=True)
class JobId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ResultId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SourceWindowId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class WorkerId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class HeartbeatOwner:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LeaseToken:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class QuietHours:
    start: time
    end: time

    def contains(self, instant: datetime) -> bool:
        current = _utc(instant).time().replace(tzinfo=None)
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


@dataclass(frozen=True, slots=True)
class StageBudgets:
    trace_sync: Money
    mine: Money
    good_export: Money
    benchmark_export: Money
    memory: Money
    fit: Money
    promotion: Money

    def for_kind(self, kind: JobKind) -> Money:
        match kind:
            case JobKind.TRACE_SYNC:
                return self.trace_sync
            case JobKind.MINE:
                return self.mine
            case JobKind.EXPORT_GOOD_TRACES:
                return self.good_export
            case JobKind.EXPORT_BENCH_EVAL:
                return self.benchmark_export
            case JobKind.PROPOSE_MEMORY:
                return self.memory
            case JobKind.FIT:
                return self.fit
            case JobKind.PROMOTE | JobKind.POST_PROMOTION_MONITOR:
                return self.promotion


@dataclass(frozen=True, slots=True)
class AutomationPolicy:
    heartbeat_interval: timedelta
    scheduler_lease: timedelta
    job_lease: timedelta
    retry_backoff: timedelta
    fit_cooldown: timedelta
    maximum_attempts: int
    no_progress_limit: int
    minimum_new_verified_traces: int
    daily_budget: Money
    quiet_hours: QuietHours
    fit_policy: FitPolicy
    stage_budgets: StageBudgets

    def __post_init__(self) -> None:
        durations = (
            self.heartbeat_interval,
            self.scheduler_lease,
            self.job_lease,
            self.retry_backoff,
        )
        if (
            any(duration <= timedelta(0) for duration in durations)
            or self.fit_cooldown < timedelta(0)
            or self.maximum_attempts < 1
            or self.no_progress_limit < 1
            or self.minimum_new_verified_traces < 1
            or self.daily_budget == Money(0)
        ):
            raise ValueError("invalid automation policy")

    @property
    def digest(self) -> Sha256Digest:
        payload = "\0".join(
            (
                str(self.heartbeat_interval.total_seconds()),
                str(self.scheduler_lease.total_seconds()),
                str(self.job_lease.total_seconds()),
                str(self.retry_backoff.total_seconds()),
                str(self.fit_cooldown.total_seconds()),
                str(self.maximum_attempts),
                str(self.no_progress_limit),
                str(self.minimum_new_verified_traces),
                str(self.daily_budget.micros),
                self.quiet_hours.start.isoformat(),
                self.quiet_hours.end.isoformat(),
                str(self.fit_policy.digest),
                str(self.stage_budgets.trace_sync.micros),
                str(self.stage_budgets.mine.micros),
                str(self.stage_budgets.good_export.micros),
                str(self.stage_budgets.benchmark_export.micros),
                str(self.stage_budgets.memory.micros),
                str(self.stage_budgets.fit.micros),
                str(self.stage_budgets.promotion.micros),
            )
        )
        return _digest(payload.encode())

    def to_json(self) -> str:
        return _POLICY_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: JobKind
    revision_id: HarnessRevisionId
    source: SourceWindowId
    maximum_cost: Money
    fit_policy_digest: Sha256Digest | None = None

    def __post_init__(self) -> None:
        if self.kind is JobKind.FIT and self.fit_policy_digest is None:
            raise SchedulerError(SchedulerErrorCode.POLICY_MISMATCH, self.source.value)


@dataclass(frozen=True, slots=True)
class Dependency:
    job_id: JobId
    mode: DependencyMode


@dataclass(frozen=True, slots=True)
class JobResult:
    id: ResultId
    kind: JobKind
    revision_id: HarnessRevisionId
    source_result_id: ResultId | None
    fit_policy_digest: Sha256Digest | None
    progress: bool


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    id: JobId
    idempotency_key: Sha256Digest
    spec: JobSpec
    state: JobState
    maximum_attempts: int
    attempt_count: int
    available_at: datetime
    lease_owner: WorkerId | None
    lease_token: LeaseToken | None
    lease_expires_at: datetime | None
    result: JobResult | None
    error_code: SchedulerErrorCode | None
    reserved_day: date | None
    reserved_cost: Money
    created_at: datetime
    updated_at: datetime

    def to_json(self) -> str:
        return _JOB_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class JobLease:
    job: ScheduledJob
    token: LeaseToken
    attempt: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class JobAttempt:
    job_id: JobId
    number: int
    state: JobState
    worker_id: WorkerId
    lease_token: LeaseToken
    leased_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    spent: Money
    error_code: SchedulerErrorCode | None


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    day: date
    reserved: Money
    spent: Money
    limit: Money


@dataclass(frozen=True, slots=True)
class RevisionAutomationState:
    revision_id: HarnessRevisionId
    no_progress_count: int
    circuit: CircuitState
    last_fit_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class JobBlocker:
    job_id: JobId
    code: BlockerCode


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    recovered: tuple[JobId, ...]
    readied: tuple[JobId, ...]
    failed: tuple[JobId, ...]
    skipped: tuple[JobId, ...]
    blockers: tuple[JobBlocker, ...]


@dataclass(frozen=True, slots=True)
class HeartbeatEvidence:
    source: SourceWindowId
    origin: EvidenceOrigin
    new_verified_traces: int
    confirmed_cluster_ready: bool
    manual_fit: bool = False

    def __post_init__(self) -> None:
        if self.new_verified_traces < 0:
            raise ValueError("trace count cannot be negative")


@dataclass(frozen=True, slots=True)
class HeartbeatReport:
    created: tuple[JobId, ...]
    reconciliation: ReconcileReport
    next_wake: datetime


@dataclass(frozen=True, slots=True)
class JobExecution:
    result: JobResult
    spent: Money


@dataclass(frozen=True, slots=True)
class JobContext:
    scheduler: LocalScheduler
    lease: JobLease
    now: datetime

    def renew(self, now: datetime) -> JobLease:
        return self.scheduler.renew(self.lease, now)


class JobHandler(Protocol):
    @property
    def kind(self) -> JobKind: ...

    def execute(self, job: JobSpec, context: JobContext) -> JobExecution: ...


class EvidenceReader(Protocol):
    def read(self, revision_id: HarnessRevisionId, now: datetime) -> HeartbeatEvidence: ...


@dataclass(frozen=True, slots=True)
class JobExecutionError(Exception):
    disposition: FailureDisposition
    code: SchedulerErrorCode
    spent: Money


@dataclass(frozen=True, slots=True)
class _JobIdentity:
    spec: JobSpec
    dependencies: tuple[Dependency, ...]


@dataclass(frozen=True, slots=True)
class _EnqueueResult:
    job: ScheduledJob
    created: bool


@dataclass(frozen=True, slots=True)
class _Readiness:
    ready: bool
    blocker: BlockerCode | None = None
    failed: bool = False
    skipped: bool = False


_JOB_ADAPTER: TypeAdapter[ScheduledJob] = TypeAdapter(ScheduledJob)
_POLICY_ADAPTER: TypeAdapter[AutomationPolicy] = TypeAdapter(AutomationPolicy)
_ATTEMPT_ADAPTER: TypeAdapter[JobAttempt] = TypeAdapter(JobAttempt)
_REVISION_ADAPTER: TypeAdapter[RevisionAutomationState] = TypeAdapter(RevisionAutomationState)
_IDENTITY_ADAPTER: TypeAdapter[_JobIdentity] = TypeAdapter(_JobIdentity)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    policy_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    state TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_expires_at TEXT,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduler_jobs_ready
ON scheduler_jobs (state, available_at, job_id);
CREATE INDEX IF NOT EXISTS scheduler_jobs_revision
ON scheduler_jobs (revision_id, kind, state);
CREATE TABLE IF NOT EXISTS scheduler_dependencies (
    job_id TEXT NOT NULL REFERENCES scheduler_jobs(job_id),
    predecessor_id TEXT NOT NULL REFERENCES scheduler_jobs(job_id),
    mode TEXT NOT NULL,
    PRIMARY KEY (job_id, predecessor_id)
);
CREATE TABLE IF NOT EXISTS scheduler_attempts (
    job_id TEXT NOT NULL REFERENCES scheduler_jobs(job_id),
    attempt_number INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (job_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS scheduler_budgets (
    day TEXT PRIMARY KEY,
    reserved_micros INTEGER NOT NULL,
    spent_micros INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduler_revision_state (
    revision_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
"""


class LocalScheduler:
    """SQLite scheduler for one local daemon and a small worker pool."""

    def __init__(self, path: Path, policy: AutomationPolicy) -> None:
        connection: sqlite3.Connection | None = None
        try:
            self._path = path
            self.policy = policy
            self._lock = RLock()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.executescript(_SCHEMA)
            self._bind_policy()
            self._restrict_sidecar_permissions()
        except SchedulerError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, str(path)) from error

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(OSError):
                self._restrict_sidecar_permissions()
            self._connection.close()

    def enqueue(
        self,
        spec: JobSpec,
        dependencies: tuple[Dependency, ...],
        now: datetime,
    ) -> ScheduledJob:
        return self._enqueue(spec, dependencies, now).job

    def _enqueue(
        self,
        spec: JobSpec,
        dependencies: tuple[Dependency, ...],
        now: datetime,
    ) -> _EnqueueResult:
        instant = _utc(now)
        ordered = tuple(sorted(dependencies, key=_dependency_sort_key))
        identity = _JobIdentity(spec, ordered)
        key = _digest(_IDENTITY_ADAPTER.dump_json(identity))
        job_id = JobId(f"job_{key.value.removeprefix('sha256:')}")
        with self._transaction():
            existing = self._job_by_key(key)
            if existing is not None:
                return _EnqueueResult(existing, False)
            if len({dependency.job_id for dependency in ordered}) != len(ordered):
                raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job_id.value)
            for dependency in ordered:
                self._load_job(dependency.job_id)
            state = JobState.PENDING if ordered else JobState.READY
            if spec.kind is JobKind.MINE and self._active_kind(spec, None):
                state = JobState.PENDING
            if spec.kind is JobKind.FIT:
                state = JobState.PENDING
            job = ScheduledJob(
                job_id,
                key,
                spec,
                state,
                self.policy.maximum_attempts,
                0,
                instant,
                None,
                None,
                None,
                None,
                None,
                None,
                Money(0),
                instant,
                instant,
            )
            self._connection.execute(
                """
                INSERT INTO scheduler_jobs (
                    job_id, idempotency_key, kind, revision_id, state,
                    available_at, lease_expires_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id.value,
                    str(job.idempotency_key),
                    job.spec.kind.value,
                    str(job.spec.revision_id),
                    job.state.value,
                    _iso(job.available_at),
                    None,
                    _JOB_ADAPTER.dump_json(job).decode(),
                ),
            )
            for dependency in ordered:
                self._connection.execute(
                    """
                    INSERT INTO scheduler_dependencies (job_id, predecessor_id, mode)
                    VALUES (?, ?, ?)
                    """,
                    (job.id.value, dependency.job_id.value, dependency.mode.value),
                )
            return _EnqueueResult(job, True)

    def job(self, job_id: JobId) -> ScheduledJob:
        try:
            with self._lock:
                return self._load_job(job_id)
        except sqlite3.Error as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value) from error

    def jobs(self) -> tuple[ScheduledJob, ...]:
        try:
            with self._lock:
                rows = cast(
                    Iterator[tuple[str]],
                    self._connection.execute(
                        "SELECT record_json FROM scheduler_jobs ORDER BY rowid"
                    ),
                )
                return tuple(_JOB_ADAPTER.validate_json(row[0]) for row in rows)
        except (sqlite3.Error, ValidationError) as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, str(self._path)) from error

    def attempts(self, job_id: JobId) -> tuple[JobAttempt, ...]:
        try:
            with self._lock:
                rows = cast(
                    Iterator[tuple[str]],
                    self._connection.execute(
                        """
                        SELECT record_json FROM scheduler_attempts
                        WHERE job_id = ? ORDER BY attempt_number
                        """,
                        (job_id.value,),
                    ),
                )
                return tuple(_ATTEMPT_ADAPTER.validate_json(row[0]) for row in rows)
        except (sqlite3.Error, ValidationError) as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value) from error

    def predecessors(self, job_id: JobId) -> tuple[ScheduledJob, ...]:
        try:
            with self._lock:
                return tuple(
                    self._load_job(dependency.job_id) for dependency in self._dependencies(job_id)
                )
        except sqlite3.Error as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value) from error

    def reconcile(self, now: datetime) -> ReconcileReport:
        instant = _utc(now)
        recovered: tuple[JobId, ...] = ()
        readied: tuple[JobId, ...] = ()
        failed: tuple[JobId, ...] = ()
        skipped: tuple[JobId, ...] = ()
        blockers: tuple[JobBlocker, ...] = ()
        with self._transaction():
            expired_ids = self._job_ids(
                """
                SELECT job_id FROM scheduler_jobs
                WHERE state IN (?, ?) AND lease_expires_at <= ?
                ORDER BY job_id
                """,
                (JobState.LEASED.value, JobState.RUNNING.value, _iso(instant)),
            )
            for job_id in expired_ids:
                expired = self._expire(self._load_job(job_id), instant)
                recovered = (*recovered, expired.id)
                if expired.state is JobState.FAILED:
                    failed = (*failed, expired.id)
            pending_ids = self._job_ids(
                "SELECT job_id FROM scheduler_jobs WHERE state = ? ORDER BY job_id",
                (JobState.PENDING.value,),
            )
            for job_id in pending_ids:
                job = self._load_job(job_id)
                readiness = self._readiness(job, instant)
                if readiness.failed:
                    updated = replace(
                        job,
                        state=JobState.FAILED,
                        error_code=SchedulerErrorCode.DEPENDENCY_FAILED,
                        updated_at=instant,
                    )
                    self._save_job(updated)
                    failed = (*failed, job.id)
                    continue
                if readiness.skipped:
                    updated = replace(job, state=JobState.SKIPPED, updated_at=instant)
                    self._save_job(updated)
                    skipped = (*skipped, job.id)
                    continue
                if readiness.ready:
                    updated = replace(job, state=JobState.READY, updated_at=instant)
                    self._save_job(updated)
                    readied = (*readied, job.id)
                    continue
                if readiness.blocker is not None:
                    blockers = (*blockers, JobBlocker(job.id, readiness.blocker))
        return ReconcileReport(recovered, readied, failed, skipped, blockers)

    def claim(self, worker_id: WorkerId, now: datetime) -> JobLease | None:
        instant = _utc(now)
        with self._transaction():
            ready_ids = self._job_ids(
                """
                SELECT job_id FROM scheduler_jobs
                WHERE state = ? AND available_at <= ?
                ORDER BY available_at, rowid
                """,
                (JobState.READY.value, _iso(instant)),
            )
            for job_id in ready_ids:
                job = self._load_job(job_id)
                if not self._readiness(job, instant).ready:
                    continue
                if job.spec.kind in (JobKind.MINE, JobKind.FIT) and self._active_kind(
                    job.spec,
                    job.id,
                    leased_only=True,
                ):
                    continue
                if not self._reserve(job.spec.maximum_cost, instant.date()):
                    continue
                token = LeaseToken(secrets.token_hex(16))
                expires_at = instant + self.policy.job_lease
                attempt_number = job.attempt_count + 1
                leased = replace(
                    job,
                    state=JobState.LEASED,
                    attempt_count=attempt_number,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_expires_at=expires_at,
                    reserved_day=instant.date(),
                    reserved_cost=job.spec.maximum_cost,
                    updated_at=instant,
                )
                self._save_job(leased)
                attempt = JobAttempt(
                    leased.id,
                    attempt_number,
                    JobState.LEASED,
                    worker_id,
                    token,
                    instant,
                    None,
                    None,
                    Money(0),
                    None,
                )
                self._save_attempt(attempt)
                return JobLease(leased, token, attempt_number, expires_at)
        return None

    def start(self, lease: JobLease, now: datetime) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._leased_job(lease, (JobState.LEASED,), instant)
            running = replace(job, state=JobState.RUNNING, updated_at=instant)
            self._save_job(running)
            attempt = self._load_attempt(job.id, lease.attempt)
            self._save_attempt(replace(attempt, state=JobState.RUNNING, started_at=instant))
            return running

    def renew(self, lease: JobLease, now: datetime) -> JobLease:
        instant = _utc(now)
        with self._transaction():
            job = self._leased_job(lease, (JobState.LEASED, JobState.RUNNING), instant)
            expires_at = instant + self.policy.job_lease
            renewed = replace(job, lease_expires_at=expires_at, updated_at=instant)
            self._save_job(renewed)
            return JobLease(renewed, lease.token, lease.attempt, expires_at)

    def succeed(
        self,
        lease: JobLease,
        result: JobResult,
        spent: Money,
        now: datetime,
    ) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._leased_job(lease, (JobState.LEASED, JobState.RUNNING), instant)
            self._validate_result(job, result)
            self._validate_spend(job, spent)
            self._settle_budget(job, spent)
            succeeded = replace(
                job,
                state=JobState.SUCCEEDED,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                result=result,
                error_code=None,
                reserved_day=None,
                reserved_cost=Money(0),
                updated_at=instant,
            )
            self._save_job(succeeded)
            attempt = self._load_attempt(job.id, lease.attempt)
            self._save_attempt(
                replace(
                    attempt,
                    state=JobState.SUCCEEDED,
                    finished_at=instant,
                    spent=spent,
                )
            )
            if result.kind is JobKind.FIT:
                self._record_fit_result(result, instant)
            return succeeded

    def fail(
        self,
        lease: JobLease,
        disposition: FailureDisposition,
        code: SchedulerErrorCode,
        spent: Money,
        now: datetime,
    ) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._leased_job(lease, (JobState.LEASED, JobState.RUNNING), instant)
            self._validate_spend(job, spent)
            self._settle_budget(job, spent)
            state = JobState.FAILED
            available_at = job.available_at
            if disposition is FailureDisposition.OPTIONAL:
                state = JobState.FAILED_OPTIONAL
            elif (
                disposition is FailureDisposition.RETRYABLE
                and job.attempt_count < job.maximum_attempts
            ):
                state = JobState.READY
                # ponytail: deterministic local backoff; add jitter for service-mode workers.
                available_at = instant + _backoff(
                    self.policy.retry_backoff,
                    job.attempt_count,
                )
            failed = replace(
                job,
                state=state,
                available_at=available_at,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                error_code=code,
                reserved_day=None,
                reserved_cost=Money(0),
                updated_at=instant,
            )
            self._save_job(failed)
            attempt = self._load_attempt(job.id, lease.attempt)
            self._save_attempt(
                replace(
                    attempt,
                    state=JobState.FAILED,
                    finished_at=instant,
                    spent=spent,
                    error_code=code,
                )
            )
            return failed

    def cancel(self, job_id: JobId, now: datetime) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._load_job(job_id)
            if job.state is JobState.CANCELLED:
                return job
            if job.state in (JobState.SUCCEEDED, JobState.SKIPPED):
                raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job.id.value)
            if job.lease_token is not None:
                self._settle_budget(job, Money(0))
                attempt = self._load_attempt(job.id, job.attempt_count)
                self._save_attempt(
                    replace(
                        attempt,
                        state=JobState.CANCELLED,
                        finished_at=instant,
                        error_code=SchedulerErrorCode.INVALID_TRANSITION,
                    )
                )
            cancelled = replace(
                job,
                state=JobState.CANCELLED,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                reserved_day=None,
                reserved_cost=Money(0),
                updated_at=instant,
            )
            self._save_job(cancelled)
            return cancelled

    def resume(self, job_id: JobId, now: datetime) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._load_job(job_id)
            if job.state not in (
                JobState.FAILED,
                JobState.FAILED_OPTIONAL,
                JobState.CANCELLED,
            ):
                raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job.id.value)
            dependencies = self._dependencies(job.id)
            resumed = replace(
                job,
                state=(JobState.READY if not dependencies else JobState.PENDING),
                maximum_attempts=job.attempt_count + self.policy.maximum_attempts,
                available_at=instant,
                result=None,
                error_code=None,
                updated_at=instant,
            )
            self._save_job(resumed)
            return resumed

    def skip(self, job_id: JobId, now: datetime) -> ScheduledJob:
        instant = _utc(now)
        with self._transaction():
            job = self._load_job(job_id)
            if job.state not in (JobState.PENDING, JobState.READY):
                raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job.id.value)
            skipped = replace(job, state=JobState.SKIPPED, updated_at=instant)
            self._save_job(skipped)
            return skipped

    def resume_revision(self, revision_id: HarnessRevisionId, now: datetime) -> None:
        instant = _utc(now)
        with self._transaction():
            state = self._revision_state(revision_id)
            self._save_revision_state(
                replace(
                    state,
                    no_progress_count=0,
                    circuit=CircuitState.CLOSED,
                    last_fit_completed_at=None,
                )
            )
            pending_ids = self._job_ids(
                """
                SELECT job_id FROM scheduler_jobs
                WHERE revision_id = ? AND kind = ? AND state = ?
                """,
                (str(revision_id), JobKind.FIT.value, JobState.PENDING.value),
            )
            for job_id in pending_ids:
                job = self._load_job(job_id)
                self._save_job(replace(job, available_at=instant, updated_at=instant))

    def budget(self, day: date) -> BudgetStatus:
        try:
            with self._lock:
                row = cast(
                    tuple[int, int] | None,
                    self._connection.execute(
                        """
                        SELECT reserved_micros, spent_micros
                        FROM scheduler_budgets WHERE day = ?
                        """,
                        (day.isoformat(),),
                    ).fetchone(),
                )
        except sqlite3.Error as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, day.isoformat()) from error
        if row is None:
            return BudgetStatus(day, Money(0), Money(0), self.policy.daily_budget)
        return BudgetStatus(day, Money(row[0]), Money(row[1]), self.policy.daily_budget)

    def acquire_heartbeat(self, owner: HeartbeatOwner, now: datetime) -> LeaseToken:
        instant = _utc(now)
        expires_at = instant + self.policy.scheduler_lease
        with self._transaction():
            row = cast(
                tuple[str, str, str] | None,
                self._connection.execute(
                    """
                    SELECT owner, lease_token, expires_at
                    FROM scheduler_heartbeat WHERE singleton = 1
                    """
                ).fetchone(),
            )
            if row is not None:
                existing_owner, _existing_token, existing_expiry = row
                if existing_owner != owner.value and _datetime(existing_expiry) > instant:
                    raise SchedulerError(
                        SchedulerErrorCode.HEARTBEAT_LEASE_HELD,
                        existing_owner,
                    )
            token = LeaseToken(secrets.token_hex(16))
            self._connection.execute(
                """
                INSERT INTO scheduler_heartbeat (singleton, owner, lease_token, expires_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    owner = excluded.owner,
                    lease_token = excluded.lease_token,
                    expires_at = excluded.expires_at
                """,
                (owner.value, token.value, _iso(expires_at)),
            )
            return token

    def _readiness(self, job: ScheduledJob, now: datetime) -> _Readiness:
        dependencies = self._dependencies(job.id)
        predecessors = tuple(
            (dependency, self._load_job(dependency.job_id)) for dependency in dependencies
        )
        for dependency, predecessor in predecessors:
            if dependency.mode is DependencyMode.REQUIRED:
                if predecessor.state in (
                    JobState.FAILED,
                    JobState.FAILED_OPTIONAL,
                    JobState.CANCELLED,
                    JobState.SKIPPED,
                ):
                    return _Readiness(False, BlockerCode.DEPENDENCY_FAILED, True)
                if predecessor.state is not JobState.SUCCEEDED:
                    return _Readiness(False, BlockerCode.DEPENDENCY_WAITING)
            elif predecessor.state not in (
                JobState.SUCCEEDED,
                JobState.FAILED_OPTIONAL,
                JobState.SKIPPED,
            ):
                if predecessor.state in (JobState.FAILED, JobState.CANCELLED):
                    return _Readiness(False, BlockerCode.DEPENDENCY_INVALID)
                return _Readiness(False, BlockerCode.DEPENDENCY_WAITING)
        if job.spec.kind is JobKind.MINE and self._active_kind(job.spec, job.id):
            return _Readiness(False, BlockerCode.ACTIVE_MINE)
        if job.spec.kind is JobKind.PROMOTE:
            return self._promotion_readiness(job, predecessors)
        if job.spec.kind is not JobKind.FIT:
            return _Readiness(True)
        return self._fit_readiness(job, predecessors, now)

    def _promotion_readiness(
        self,
        job: ScheduledJob,
        predecessors: tuple[tuple[Dependency, ScheduledJob], ...],
    ) -> _Readiness:
        fit_jobs = tuple(
            predecessor
            for dependency, predecessor in predecessors
            if dependency.mode is DependencyMode.REQUIRED and predecessor.spec.kind is JobKind.FIT
        )
        if len(fit_jobs) != 1:
            return _Readiness(False, BlockerCode.DEPENDENCY_INVALID)
        result = fit_jobs[0].result
        if (
            result is None
            or result.kind is not JobKind.FIT
            or result.revision_id != job.spec.revision_id
        ):
            return _Readiness(False, BlockerCode.DEPENDENCY_INVALID)
        if not result.progress:
            return _Readiness(False, BlockerCode.NO_WINNER, skipped=True)
        return _Readiness(True)

    def _fit_readiness(
        self,
        job: ScheduledJob,
        predecessors: tuple[tuple[Dependency, ScheduledJob], ...],
        now: datetime,
    ) -> _Readiness:
        if job.spec.fit_policy_digest != self.policy.fit_policy.digest:
            return _Readiness(False, BlockerCode.POLICY_MISMATCH)
        mine_jobs = tuple(
            predecessor
            for dependency, predecessor in predecessors
            if dependency.mode is DependencyMode.REQUIRED and predecessor.spec.kind is JobKind.MINE
        )
        benchmark_jobs = tuple(
            predecessor
            for dependency, predecessor in predecessors
            if dependency.mode is DependencyMode.REQUIRED
            and predecessor.spec.kind is JobKind.EXPORT_BENCH_EVAL
        )
        if len(mine_jobs) != 1 or len(benchmark_jobs) != 1:
            return _Readiness(False, BlockerCode.DEPENDENCY_INVALID)
        mine_result = mine_jobs[0].result
        benchmark_result = benchmark_jobs[0].result
        if (
            mine_result is None
            or benchmark_result is None
            or mine_result.kind is not JobKind.MINE
            or benchmark_result.kind is not JobKind.EXPORT_BENCH_EVAL
            or mine_result.revision_id != job.spec.revision_id
            or benchmark_result.revision_id != job.spec.revision_id
            or benchmark_result.source_result_id != mine_result.id
        ):
            return _Readiness(False, BlockerCode.DEPENDENCY_INVALID)
        if self._active_kind(job.spec, job.id):
            return _Readiness(False, BlockerCode.ACTIVE_FIT)
        revision_state = self._revision_state(job.spec.revision_id)
        if revision_state.circuit is CircuitState.OPEN:
            return _Readiness(False, BlockerCode.CIRCUIT_OPEN)
        if (
            revision_state.last_fit_completed_at is not None
            and now < revision_state.last_fit_completed_at + self.policy.fit_cooldown
        ):
            return _Readiness(False, BlockerCode.COOLDOWN)
        if self.policy.quiet_hours.contains(now):
            return _Readiness(False, BlockerCode.QUIET_HOURS)
        return _Readiness(True)

    def _active_kind(
        self,
        spec: JobSpec,
        excluded: JobId | None,
        *,
        leased_only: bool = False,
    ) -> bool:
        excluded_id = "" if excluded is None else excluded.value
        parameters: tuple[str, ...]
        if leased_only:
            query = """
                SELECT job_id FROM scheduler_jobs
                WHERE revision_id = ? AND kind = ?
                  AND state IN (?, ?)
                  AND job_id != ?
                ORDER BY rowid LIMIT 1
                """
            parameters = (
                str(spec.revision_id),
                spec.kind.value,
                JobState.LEASED.value,
                JobState.RUNNING.value,
                excluded_id,
            )
        else:
            query = """
                SELECT job_id FROM scheduler_jobs
                WHERE revision_id = ? AND kind = ?
                  AND state IN (?, ?, ?)
                  AND job_id != ?
                ORDER BY rowid LIMIT 1
                """
            parameters = (
                str(spec.revision_id),
                spec.kind.value,
                JobState.READY.value,
                JobState.LEASED.value,
                JobState.RUNNING.value,
                excluded_id,
            )
        row = cast(
            tuple[str] | None,
            self._connection.execute(query, parameters).fetchone(),
        )
        return row is not None

    def _expire(self, job: ScheduledJob, now: datetime) -> ScheduledJob:
        if job.lease_token is None:
            raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job.id.value)
        self._settle_budget(job, job.reserved_cost)
        state = JobState.READY if job.attempt_count < job.maximum_attempts else JobState.FAILED
        available_at = now + _backoff(self.policy.retry_backoff, job.attempt_count)
        expired = replace(
            job,
            state=state,
            available_at=available_at,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            error_code=SchedulerErrorCode.LEASE_EXPIRED,
            reserved_day=None,
            reserved_cost=Money(0),
            updated_at=now,
        )
        self._save_job(expired)
        attempt = self._load_attempt(job.id, job.attempt_count)
        self._save_attempt(
            replace(
                attempt,
                state=JobState.FAILED,
                finished_at=now,
                spent=job.reserved_cost,
                error_code=SchedulerErrorCode.LEASE_EXPIRED,
            )
        )
        return expired

    def _validate_result(self, job: ScheduledJob, result: JobResult) -> None:
        if (
            result.kind is not job.spec.kind
            or result.revision_id != job.spec.revision_id
            or (
                job.spec.kind is JobKind.FIT
                and result.fit_policy_digest != job.spec.fit_policy_digest
            )
        ):
            raise SchedulerError(SchedulerErrorCode.RESULT_INVALID, job.id.value)

    def _validate_spend(self, job: ScheduledJob, spent: Money) -> None:
        if spent > job.reserved_cost:
            raise SchedulerError(SchedulerErrorCode.BUDGET_EXHAUSTED, job.id.value)

    def _record_fit_result(self, result: JobResult, now: datetime) -> None:
        state = self._revision_state(result.revision_id)
        no_progress = 0 if result.progress else state.no_progress_count + 1
        circuit = (
            CircuitState.OPEN
            if no_progress >= self.policy.no_progress_limit
            else CircuitState.CLOSED
        )
        self._save_revision_state(
            RevisionAutomationState(
                result.revision_id,
                no_progress,
                circuit,
                now,
            )
        )

    def _leased_job(
        self,
        lease: JobLease,
        allowed_states: tuple[JobState, ...],
        now: datetime,
    ) -> ScheduledJob:
        job = self._load_job(lease.job.id)
        if (
            job.state not in allowed_states
            or job.lease_token != lease.token
            or job.attempt_count != lease.attempt
        ):
            raise SchedulerError(SchedulerErrorCode.LATE_COMPLETION, lease.job.id.value)
        if job.lease_expires_at is None or now >= job.lease_expires_at:
            raise SchedulerError(SchedulerErrorCode.LEASE_EXPIRED, job.id.value)
        return job

    def _reserve(self, amount: Money, day: date) -> bool:
        status = self._budget(day)
        if status.reserved + status.spent + amount > status.limit:
            return False
        self._connection.execute(
            """
            INSERT INTO scheduler_budgets (day, reserved_micros, spent_micros)
            VALUES (?, ?, 0)
            ON CONFLICT(day) DO UPDATE SET
                reserved_micros = scheduler_budgets.reserved_micros + excluded.reserved_micros
            """,
            (day.isoformat(), amount.micros),
        )
        return True

    def _settle_budget(self, job: ScheduledJob, spent: Money) -> None:
        if job.reserved_day is None:
            if spent != Money(0):
                raise SchedulerError(SchedulerErrorCode.INVALID_TRANSITION, job.id.value)
            return
        self._connection.execute(
            """
            UPDATE scheduler_budgets
            SET reserved_micros = reserved_micros - ?,
                spent_micros = spent_micros + ?
            WHERE day = ?
            """,
            (job.reserved_cost.micros, spent.micros, job.reserved_day.isoformat()),
        )

    def _budget(self, day: date) -> BudgetStatus:
        row = cast(
            tuple[int, int] | None,
            self._connection.execute(
                """
                SELECT reserved_micros, spent_micros
                FROM scheduler_budgets WHERE day = ?
                """,
                (day.isoformat(),),
            ).fetchone(),
        )
        if row is None:
            return BudgetStatus(day, Money(0), Money(0), self.policy.daily_budget)
        return BudgetStatus(day, Money(row[0]), Money(row[1]), self.policy.daily_budget)

    def _revision_state(self, revision_id: HarnessRevisionId) -> RevisionAutomationState:
        row = cast(
            tuple[str] | None,
            self._connection.execute(
                """
                SELECT record_json FROM scheduler_revision_state WHERE revision_id = ?
                """,
                (str(revision_id),),
            ).fetchone(),
        )
        if row is None:
            return RevisionAutomationState(revision_id, 0, CircuitState.CLOSED, None)
        try:
            return _REVISION_ADAPTER.validate_json(row[0])
        except ValidationError as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, str(revision_id)) from error

    def _save_revision_state(self, state: RevisionAutomationState) -> None:
        self._connection.execute(
            """
            INSERT INTO scheduler_revision_state (revision_id, record_json)
            VALUES (?, ?)
            ON CONFLICT(revision_id) DO UPDATE SET record_json = excluded.record_json
            """,
            (str(state.revision_id), _REVISION_ADAPTER.dump_json(state).decode()),
        )

    def _dependencies(self, job_id: JobId) -> tuple[Dependency, ...]:
        rows = cast(
            Iterator[tuple[str, str]],
            self._connection.execute(
                """
                SELECT predecessor_id, mode FROM scheduler_dependencies
                WHERE job_id = ? ORDER BY predecessor_id
                """,
                (job_id.value,),
            ),
        )
        return tuple(Dependency(JobId(row[0]), DependencyMode(row[1])) for row in rows)

    def _load_job(self, job_id: JobId) -> ScheduledJob:
        row = cast(
            tuple[str] | None,
            self._connection.execute(
                "SELECT record_json FROM scheduler_jobs WHERE job_id = ?",
                (job_id.value,),
            ).fetchone(),
        )
        if row is None:
            raise SchedulerError(SchedulerErrorCode.JOB_NOT_FOUND, job_id.value)
        try:
            return _JOB_ADAPTER.validate_json(row[0])
        except ValidationError as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value) from error

    def _job_by_key(self, key: Sha256Digest) -> ScheduledJob | None:
        row = cast(
            tuple[str] | None,
            self._connection.execute(
                "SELECT record_json FROM scheduler_jobs WHERE idempotency_key = ?",
                (str(key),),
            ).fetchone(),
        )
        if row is None:
            return None
        try:
            return _JOB_ADAPTER.validate_json(row[0])
        except ValidationError as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, str(key)) from error

    def _save_job(self, job: ScheduledJob) -> None:
        self._connection.execute(
            """
            UPDATE scheduler_jobs
            SET state = ?, available_at = ?, lease_expires_at = ?, record_json = ?
            WHERE job_id = ?
            """,
            (
                job.state.value,
                _iso(job.available_at),
                None if job.lease_expires_at is None else _iso(job.lease_expires_at),
                _JOB_ADAPTER.dump_json(job).decode(),
                job.id.value,
            ),
        )

    def _load_attempt(self, job_id: JobId, number: int) -> JobAttempt:
        row = cast(
            tuple[str] | None,
            self._connection.execute(
                """
                SELECT record_json FROM scheduler_attempts
                WHERE job_id = ? AND attempt_number = ?
                """,
                (job_id.value, number),
            ).fetchone(),
        )
        if row is None:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value)
        try:
            return _ATTEMPT_ADAPTER.validate_json(row[0])
        except ValidationError as error:
            raise SchedulerError(SchedulerErrorCode.DATABASE_ERROR, job_id.value) from error

    def _save_attempt(self, attempt: JobAttempt) -> None:
        self._connection.execute(
            """
            INSERT INTO scheduler_attempts (job_id, attempt_number, record_json)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id, attempt_number) DO UPDATE SET record_json = excluded.record_json
            """,
            (
                attempt.job_id.value,
                attempt.number,
                _ATTEMPT_ADAPTER.dump_json(attempt).decode(),
            ),
        )

    def _job_ids(self, query: str, parameters: tuple[str, ...]) -> tuple[JobId, ...]:
        rows = cast(Iterator[tuple[str]], self._connection.execute(query, parameters))
        return tuple(JobId(row[0]) for row in rows)

    def _bind_policy(self) -> None:
        row = cast(
            tuple[int, str] | None,
            self._connection.execute(
                """
                SELECT schema_version, policy_digest FROM scheduler_meta WHERE singleton = 1
                """
            ).fetchone(),
        )
        if row is None:
            self._connection.execute(
                """
                INSERT INTO scheduler_meta (singleton, schema_version, policy_digest)
                VALUES (1, 1, ?)
                """,
                (str(self.policy.digest),),
            )
            return
        if row != (1, str(self.policy.digest)):
            raise SchedulerError(SchedulerErrorCode.POLICY_MISMATCH, str(self._path))

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield
                self._connection.execute("COMMIT")
            except sqlite3.Error as error:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                raise SchedulerError(
                    SchedulerErrorCode.DATABASE_ERROR,
                    str(self._path),
                ) from error
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                raise

    def _restrict_sidecar_permissions(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = self._path.with_name(f"{self._path.name}{suffix}")
            try:
                sidecar.chmod(0o600)
            except FileNotFoundError:
                continue


@dataclass(frozen=True, slots=True)
class Heartbeat:
    scheduler: LocalScheduler
    owner: HeartbeatOwner

    def tick(
        self,
        revision_id: HarnessRevisionId,
        evidence: HeartbeatEvidence,
        now: datetime,
    ) -> HeartbeatReport:
        instant = _utc(now)
        self.scheduler.acquire_heartbeat(self.owner, instant)
        if evidence.origin is EvidenceOrigin.OFW_CONTROL_PLANE:
            reconciliation = self.scheduler.reconcile(instant)
            return HeartbeatReport(
                (),
                reconciliation,
                instant + self.scheduler.policy.heartbeat_interval,
            )
        created: tuple[JobId, ...] = ()
        trace = self.scheduler._enqueue(
            JobSpec(
                JobKind.TRACE_SYNC,
                revision_id,
                evidence.source,
                self.scheduler.policy.stage_budgets.for_kind(JobKind.TRACE_SYNC),
            ),
            (),
            instant,
        )
        if trace.created:
            created = (*created, trace.job.id)
        if evidence.new_verified_traces >= self.scheduler.policy.minimum_new_verified_traces:
            mine = self.scheduler._enqueue(
                JobSpec(
                    JobKind.MINE,
                    revision_id,
                    evidence.source,
                    self.scheduler.policy.stage_budgets.for_kind(JobKind.MINE),
                ),
                (Dependency(trace.job.id, DependencyMode.REQUIRED),),
                instant,
            )
            if mine.created:
                created = (*created, mine.job.id)
            good = self.scheduler._enqueue(
                JobSpec(
                    JobKind.EXPORT_GOOD_TRACES,
                    revision_id,
                    evidence.source,
                    self.scheduler.policy.stage_budgets.for_kind(JobKind.EXPORT_GOOD_TRACES),
                ),
                (Dependency(mine.job.id, DependencyMode.REQUIRED),),
                instant,
            )
            if good.created:
                created = (*created, good.job.id)
            benchmark = self.scheduler._enqueue(
                JobSpec(
                    JobKind.EXPORT_BENCH_EVAL,
                    revision_id,
                    evidence.source,
                    self.scheduler.policy.stage_budgets.for_kind(JobKind.EXPORT_BENCH_EVAL),
                ),
                (Dependency(mine.job.id, DependencyMode.REQUIRED),),
                instant,
            )
            if benchmark.created:
                created = (*created, benchmark.job.id)
            memory = self.scheduler._enqueue(
                JobSpec(
                    JobKind.PROPOSE_MEMORY,
                    revision_id,
                    evidence.source,
                    self.scheduler.policy.stage_budgets.for_kind(JobKind.PROPOSE_MEMORY),
                ),
                (Dependency(mine.job.id, DependencyMode.REQUIRED),),
                instant,
            )
            if memory.created:
                created = (*created, memory.job.id)
            if evidence.confirmed_cluster_ready or evidence.manual_fit:
                fit = self.scheduler._enqueue(
                    JobSpec(
                        JobKind.FIT,
                        revision_id,
                        evidence.source,
                        self.scheduler.policy.stage_budgets.for_kind(JobKind.FIT),
                        self.scheduler.policy.fit_policy.digest,
                    ),
                    (
                        Dependency(mine.job.id, DependencyMode.REQUIRED),
                        Dependency(benchmark.job.id, DependencyMode.REQUIRED),
                        Dependency(memory.job.id, DependencyMode.OPTIONAL),
                    ),
                    instant,
                )
                if fit.created:
                    created = (*created, fit.job.id)
                promotion = self.scheduler._enqueue(
                    JobSpec(
                        JobKind.PROMOTE,
                        revision_id,
                        evidence.source,
                        self.scheduler.policy.stage_budgets.for_kind(JobKind.PROMOTE),
                    ),
                    (Dependency(fit.job.id, DependencyMode.REQUIRED),),
                    instant,
                )
                if promotion.created:
                    created = (*created, promotion.job.id)
        reconciliation = self.scheduler.reconcile(instant)
        return HeartbeatReport(
            created,
            reconciliation,
            instant + self.scheduler.policy.heartbeat_interval,
        )


@dataclass(frozen=True, slots=True)
class SchedulerDaemon:
    scheduler: LocalScheduler
    owner: HeartbeatOwner
    revisions: tuple[HarnessRevisionId, ...]
    evidence: EvidenceReader

    def __post_init__(self) -> None:
        if not self.revisions or len(set(self.revisions)) != len(self.revisions):
            raise ValueError("daemon requires unique harness revisions")

    def serve(self, stop: Event) -> None:
        heartbeat = Heartbeat(self.scheduler, self.owner)
        while not stop.is_set():
            now = datetime.now(UTC)
            for revision_id in self.revisions:
                report = heartbeat.tick(
                    revision_id,
                    self.evidence.read(revision_id, now),
                    now,
                )
                logger.debug(
                    "Scheduler heartbeat: revision=%s created=%d blockers=%d next=%s",
                    revision_id,
                    len(report.created),
                    len(report.reconciliation.blockers),
                    report.next_wake.isoformat(),
                )
            stop.wait(self.scheduler.policy.heartbeat_interval.total_seconds())


@dataclass(frozen=True, slots=True)
class Worker:
    scheduler: LocalScheduler
    id: WorkerId
    handlers: tuple[JobHandler, ...]

    def run_once(self, now: datetime) -> ScheduledJob | None:
        lease = self.scheduler.claim(self.id, now)
        if lease is None:
            return None
        self.scheduler.start(lease, now)
        handler = next(
            (candidate for candidate in self.handlers if candidate.kind is lease.job.spec.kind),
            None,
        )
        if handler is None:
            return self._fail_lease(
                lease,
                FailureDisposition.TERMINAL,
                SchedulerErrorCode.HANDLER_MISSING,
                Money(0),
                now,
            )
        try:
            execution = handler.execute(
                lease.job.spec,
                JobContext(self.scheduler, lease, _utc(now)),
            )
        except JobExecutionError as error:
            return self._fail_lease(
                lease,
                error.disposition,
                error.code,
                error.spent,
                now,
            )
        except Exception:
            return self._fail_lease(
                lease,
                FailureDisposition.TERMINAL,
                SchedulerErrorCode.HANDLER_FAILED,
                Money(0),
                now,
            )
        try:
            return self.scheduler.succeed(lease, execution.result, execution.spent, now)
        except SchedulerError as error:
            if error.code in (
                SchedulerErrorCode.BUDGET_EXHAUSTED,
                SchedulerErrorCode.RESULT_INVALID,
            ):
                return self._fail_lease(
                    lease,
                    FailureDisposition.TERMINAL,
                    error.code,
                    (
                        execution.spent
                        if execution.spent <= lease.job.spec.maximum_cost
                        else lease.job.spec.maximum_cost
                    ),
                    now,
                )
            raise

    def _fail_lease(
        self,
        lease: JobLease,
        disposition: FailureDisposition,
        code: SchedulerErrorCode,
        spent: Money,
        now: datetime,
    ) -> ScheduledJob:
        try:
            return self.scheduler.fail(lease, disposition, code, spent, now)
        except SchedulerError as error:
            if error.code is SchedulerErrorCode.BUDGET_EXHAUSTED:
                return self.scheduler.fail(
                    lease,
                    FailureDisposition.TERMINAL,
                    SchedulerErrorCode.BUDGET_EXHAUSTED,
                    lease.job.spec.maximum_cost,
                    now,
                )
            raise

    def serve(self, stop: Event, poll_interval: timedelta) -> None:
        if poll_interval <= timedelta(0):
            raise ValueError("poll interval must be positive")
        while not stop.is_set():
            completed = self.run_once(datetime.now(UTC))
            if completed is None:
                stop.wait(poll_interval.total_seconds())


def _dependency_sort_key(dependency: Dependency) -> tuple[str, str]:
    return dependency.job_id.value, dependency.mode.value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _datetime(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _digest(payload: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")


def _backoff(base: timedelta, attempt: int) -> timedelta:
    if attempt < 1:
        raise ValueError("attempt must be positive")
    return base * (1 << (attempt - 1))


def read_automation_policy(path: Path) -> AutomationPolicy:
    try:
        return _POLICY_ADAPTER.validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise SchedulerError(SchedulerErrorCode.POLICY_MISMATCH, str(path)) from error
