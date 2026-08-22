"""Durable local heartbeat, scheduler, recovery, and worker contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from ofw import FitPolicy
from ofw import ofw as ofw_namespace
from ofw.contracts import HarnessRevisionId
from ofw.scheduler import (
    AutomationPolicy,
    BlockerCode,
    Dependency,
    DependencyMode,
    EvidenceOrigin,
    FailureDisposition,
    Heartbeat,
    HeartbeatEvidence,
    HeartbeatOwner,
    JobContext,
    JobExecution,
    JobHandler,
    JobKind,
    JobResult,
    JobSpec,
    JobState,
    LocalScheduler,
    Money,
    QuietHours,
    ResultId,
    SchedulerError,
    SchedulerErrorCode,
    SourceWindowId,
    StageBudgets,
    Worker,
    WorkerId,
)

_NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
_REVISION = HarnessRevisionId("revision-fixture")
_DEFAULT_DAILY_BUDGET = Money(1_000_000)
_DEFAULT_JOB_COST = Money(10_000)


def test_scheduler_is_available_from_public_namespace() -> None:
    assert ofw_namespace.AutomationPolicy is AutomationPolicy
    assert ofw_namespace.LocalScheduler is LocalScheduler
    assert ofw_namespace.Money is Money


def _fit_policy() -> FitPolicy:
    return FitPolicy(0.1, 0.99, 0, 0.1, 0.1, 0.99, 1.0)


def _policy(*, daily_budget: Money = _DEFAULT_DAILY_BUDGET) -> AutomationPolicy:
    return AutomationPolicy(
        heartbeat_interval=timedelta(seconds=30),
        scheduler_lease=timedelta(seconds=45),
        job_lease=timedelta(minutes=1),
        retry_backoff=timedelta(seconds=5),
        fit_cooldown=timedelta(hours=1),
        maximum_attempts=2,
        no_progress_limit=3,
        minimum_new_verified_traces=5,
        daily_budget=daily_budget,
        quiet_hours=QuietHours(time(22), time(6)),
        fit_policy=_fit_policy(),
        stage_budgets=StageBudgets(
            trace_sync=Money(10_000),
            mine=Money(40_000),
            good_export=Money(5_000),
            benchmark_export=Money(10_000),
            memory=Money(10_000),
            fit=Money(100_000),
        ),
    )


def _scheduler(tmp_path: Path, *, policy: AutomationPolicy | None = None) -> LocalScheduler:
    return LocalScheduler(tmp_path / "scheduler.sqlite3", policy or _policy())


def _spec(
    kind: JobKind,
    source: str,
    *,
    cost: Money = _DEFAULT_JOB_COST,
    fit_policy: FitPolicy | None = None,
) -> JobSpec:
    return JobSpec(
        kind,
        _REVISION,
        SourceWindowId(source),
        cost,
        None if fit_policy is None else fit_policy.digest,
    )


def _result(
    job_kind: JobKind,
    name: str,
    *,
    source: ResultId | None = None,
    progress: bool = True,
    fit_policy: FitPolicy | None = None,
) -> JobResult:
    return JobResult(
        ResultId(name),
        job_kind,
        _REVISION,
        source,
        None if fit_policy is None else fit_policy.digest,
        progress,
    )


def _run_success(
    scheduler: LocalScheduler,
    worker: WorkerId,
    now: datetime,
    result: JobResult,
) -> None:
    lease = scheduler.claim(worker, now)
    assert lease is not None
    scheduler.start(lease, now)
    scheduler.succeed(lease, result, Money(1_000), now)


def test_dependency_readiness_and_idempotent_enqueue(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    trace = scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "window-1"), (), _NOW)
    duplicate = scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "window-1"), (), _NOW)
    mine = scheduler.enqueue(
        _spec(JobKind.MINE, "window-1"),
        (Dependency(trace.id, DependencyMode.REQUIRED),),
        _NOW,
    )

    assert duplicate.id == trace.id
    assert scheduler.job(mine.id).state is JobState.PENDING
    _run_success(
        scheduler,
        WorkerId("worker-1"),
        _NOW,
        _result(JobKind.TRACE_SYNC, "sync-result"),
    )

    scheduler.reconcile(_NOW)

    assert scheduler.job(mine.id).state is JobState.READY
    scheduler.close()


def test_required_dependency_failure_stops_its_branch(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    parent = scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "failure"), (), _NOW)
    child = scheduler.enqueue(
        _spec(JobKind.MINE, "blocked"),
        (Dependency(parent.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    lease = scheduler.claim(WorkerId("worker"), _NOW)
    assert lease is not None
    scheduler.start(lease, _NOW)
    scheduler.fail(
        lease,
        FailureDisposition.TERMINAL,
        SchedulerErrorCode.HANDLER_FAILED,
        Money(0),
        _NOW,
    )

    report = scheduler.reconcile(_NOW)

    assert scheduler.job(child.id).state is JobState.FAILED
    assert report.failed == (child.id,)
    scheduler.close()


def test_fit_requires_matching_benchmark_lineage_and_optional_memory(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    mine = scheduler.enqueue(_spec(JobKind.MINE, "mine"), (), _NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(JobKind.MINE, "mine-result"),
    )
    benchmark = scheduler.enqueue(
        _spec(JobKind.EXPORT_BENCH_EVAL, "benchmark"),
        (Dependency(mine.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    scheduler.reconcile(_NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(
            JobKind.EXPORT_BENCH_EVAL,
            "benchmark-result",
            source=ResultId("wrong-mine"),
        ),
    )
    memory = scheduler.enqueue(
        _spec(JobKind.PROPOSE_MEMORY, "memory"),
        (Dependency(mine.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    scheduler.reconcile(_NOW)
    lease = scheduler.claim(WorkerId("worker"), _NOW)
    assert lease is not None
    assert lease.job.id == memory.id
    scheduler.start(lease, _NOW)
    scheduler.fail(
        lease,
        FailureDisposition.OPTIONAL,
        SchedulerErrorCode.HANDLER_FAILED,
        Money(1_000),
        _NOW,
    )
    fit = scheduler.enqueue(
        _spec(JobKind.FIT, "fit-wrong-lineage", cost=Money(100_000), fit_policy=_fit_policy()),
        (
            Dependency(mine.id, DependencyMode.REQUIRED),
            Dependency(benchmark.id, DependencyMode.REQUIRED),
            Dependency(memory.id, DependencyMode.OPTIONAL),
        ),
        _NOW,
    )

    report = scheduler.reconcile(_NOW)

    assert scheduler.job(fit.id).state is JobState.PENDING
    assert any(
        blocker.job_id == fit.id and blocker.code is BlockerCode.DEPENDENCY_INVALID
        for blocker in report.blockers
    )

    valid_benchmark = scheduler.enqueue(
        _spec(JobKind.EXPORT_BENCH_EVAL, "benchmark-valid"),
        (Dependency(mine.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    scheduler.reconcile(_NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(
            JobKind.EXPORT_BENCH_EVAL,
            "benchmark-valid-result",
            source=ResultId("mine-result"),
        ),
    )
    valid_fit = scheduler.enqueue(
        _spec(JobKind.FIT, "fit-valid", cost=Money(100_000), fit_policy=_fit_policy()),
        (
            Dependency(mine.id, DependencyMode.REQUIRED),
            Dependency(valid_benchmark.id, DependencyMode.REQUIRED),
            Dependency(memory.id, DependencyMode.OPTIONAL),
        ),
        _NOW,
    )

    scheduler.reconcile(_NOW)

    assert scheduler.job(valid_fit.id).state is JobState.READY
    scheduler.close()


def test_policy_mismatch_and_overlapping_fit_are_blocked(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    mine = scheduler.enqueue(_spec(JobKind.MINE, "mine"), (), _NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(JobKind.MINE, "mine-result"),
    )
    benchmark = scheduler.enqueue(
        _spec(JobKind.EXPORT_BENCH_EVAL, "benchmark"),
        (Dependency(mine.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    scheduler.reconcile(_NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(
            JobKind.EXPORT_BENCH_EVAL,
            "benchmark-result",
            source=ResultId("mine-result"),
        ),
    )
    dependencies = (
        Dependency(mine.id, DependencyMode.REQUIRED),
        Dependency(benchmark.id, DependencyMode.REQUIRED),
    )
    mismatched = scheduler.enqueue(
        _spec(
            JobKind.FIT,
            "mismatched",
            fit_policy=FitPolicy(0.2, 0.99, 0, 0.1, 0.1, 0.99, 1.0),
        ),
        dependencies,
        _NOW,
    )
    first = scheduler.enqueue(
        _spec(JobKind.FIT, "first", fit_policy=_fit_policy()),
        dependencies,
        _NOW,
    )
    second = scheduler.enqueue(
        _spec(JobKind.FIT, "second", fit_policy=_fit_policy()),
        dependencies,
        _NOW,
    )

    report = scheduler.reconcile(_NOW)

    assert scheduler.job(mismatched.id).state is JobState.PENDING
    assert scheduler.job(first.id).state is JobState.READY
    assert scheduler.job(second.id).state is JobState.PENDING
    assert any(
        blocker.job_id == mismatched.id and blocker.code is BlockerCode.POLICY_MISMATCH
        for blocker in report.blockers
    )
    assert any(
        blocker.job_id == second.id and blocker.code is BlockerCode.ACTIVE_FIT
        for blocker in report.blockers
    )
    scheduler.close()


def test_only_one_mine_for_a_revision_can_be_active(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    first = scheduler.enqueue(_spec(JobKind.MINE, "first-mine"), (), _NOW)
    second = scheduler.enqueue(_spec(JobKind.MINE, "second-mine"), (), _NOW)

    report = scheduler.reconcile(_NOW)

    assert scheduler.job(first.id).state is JobState.READY
    assert scheduler.job(second.id).state is JobState.PENDING
    assert any(
        blocker.job_id == second.id and blocker.code is BlockerCode.ACTIVE_MINE
        for blocker in report.blockers
    )
    scheduler.close()


def test_expired_lease_retries_and_rejects_late_completion(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.sqlite3"
    scheduler = LocalScheduler(path, _policy())
    job = scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "retry"), (), _NOW)
    first = scheduler.claim(WorkerId("worker-1"), _NOW)
    assert first is not None
    scheduler.start(first, _NOW)
    scheduler.close()

    scheduler = LocalScheduler(path, _policy())
    scheduler.reconcile(_NOW + timedelta(minutes=2))
    second = scheduler.claim(WorkerId("worker-2"), _NOW + timedelta(minutes=2, seconds=5))
    assert second is not None
    assert second.job.id == job.id
    assert second.token != first.token

    with pytest.raises(SchedulerError) as raised:
        scheduler.succeed(
            first,
            _result(JobKind.TRACE_SYNC, "late"),
            Money(1_000),
            _NOW + timedelta(minutes=2, seconds=6),
        )
    assert raised.value.code is SchedulerErrorCode.LATE_COMPLETION

    scheduler.start(second, _NOW + timedelta(minutes=2, seconds=6))
    scheduler.succeed(
        second,
        _result(JobKind.TRACE_SYNC, "committed"),
        Money(1_000),
        _NOW + timedelta(minutes=2, seconds=7),
    )
    assert scheduler.job(job.id).result == _result(JobKind.TRACE_SYNC, "committed")
    assert tuple(attempt.state for attempt in scheduler.attempts(job.id)) == (
        JobState.FAILED,
        JobState.SUCCEEDED,
    )
    scheduler.close()


def test_expired_lease_cannot_commit_before_reconcile_and_releases_budget(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path, policy=_policy(daily_budget=Money(100_000)))
    job = scheduler.enqueue(
        _spec(JobKind.TRACE_SYNC, "expired-budget", cost=Money(60_000)),
        (),
        _NOW,
    )
    lease = scheduler.claim(WorkerId("worker-1"), _NOW)
    assert lease is not None
    scheduler.start(lease, _NOW)

    expired = _NOW + timedelta(minutes=2)
    with pytest.raises(SchedulerError) as raised:
        scheduler.succeed(
            lease,
            _result(JobKind.TRACE_SYNC, "too-late"),
            Money(1_000),
            expired,
        )

    assert raised.value.code is SchedulerErrorCode.LEASE_EXPIRED
    assert scheduler.job(job.id).state is JobState.RUNNING
    assert scheduler.budget(_NOW.date()).reserved == Money(60_000)

    scheduler.reconcile(expired)

    assert scheduler.job(job.id).state is JobState.READY
    assert scheduler.budget(_NOW.date()).reserved == Money(0)
    assert scheduler.budget(_NOW.date()).spent == Money(0)
    assert scheduler.claim(WorkerId("worker-2"), expired + timedelta(seconds=5)) is not None
    scheduler.close()


def test_cancel_resume_and_restart_are_durable(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.sqlite3"
    scheduler = LocalScheduler(path, _policy())
    job = scheduler.enqueue(_spec(JobKind.MINE, "restart"), (), _NOW)
    scheduler.cancel(job.id, _NOW)
    scheduler.close()

    restarted = LocalScheduler(path, _policy())
    assert restarted.job(job.id).state is JobState.CANCELLED
    restarted.resume(job.id, _NOW)
    assert restarted.job(job.id).state is JobState.READY
    restarted.close()


def test_restart_rejects_a_different_automation_policy(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.sqlite3"
    LocalScheduler(path, _policy()).close()

    with pytest.raises(SchedulerError) as raised:
        LocalScheduler(path, _policy(daily_budget=Money(999_999)))

    assert raised.value.code is SchedulerErrorCode.POLICY_MISMATCH


def test_quiet_hours_budget_cooldown_and_no_progress_circuit(tmp_path: Path) -> None:
    policy = _policy(daily_budget=Money(250_000))
    scheduler = _scheduler(tmp_path, policy=policy)
    mine = scheduler.enqueue(_spec(JobKind.MINE, "mine"), (), _NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(JobKind.MINE, "mine-result"),
    )
    benchmark = scheduler.enqueue(
        _spec(JobKind.EXPORT_BENCH_EVAL, "benchmark"),
        (Dependency(mine.id, DependencyMode.REQUIRED),),
        _NOW,
    )
    scheduler.reconcile(_NOW)
    _run_success(
        scheduler,
        WorkerId("worker"),
        _NOW,
        _result(
            JobKind.EXPORT_BENCH_EVAL,
            "benchmark-result",
            source=ResultId("mine-result"),
        ),
    )
    dependencies = (
        Dependency(mine.id, DependencyMode.REQUIRED),
        Dependency(benchmark.id, DependencyMode.REQUIRED),
    )
    quiet = _NOW.replace(hour=23)
    quiet_fit = scheduler.enqueue(
        _spec(JobKind.FIT, "quiet", cost=Money(100_000), fit_policy=_fit_policy()),
        dependencies,
        quiet,
    )
    quiet_report = scheduler.reconcile(quiet)
    assert scheduler.job(quiet_fit.id).state is JobState.PENDING
    assert any(blocker.code is BlockerCode.QUIET_HOURS for blocker in quiet_report.blockers)

    current = _NOW + timedelta(days=1)
    for index in range(3):
        fit = scheduler.enqueue(
            _spec(
                JobKind.FIT,
                f"fit-{index}",
                cost=Money(60_000),
                fit_policy=_fit_policy(),
            ),
            dependencies,
            current,
        )
        scheduler.reconcile(current)
        lease = scheduler.claim(WorkerId("fit-worker"), current)
        assert lease is not None
        assert lease.job.id == fit.id
        scheduler.start(lease, current)
        scheduler.succeed(
            lease,
            _result(
                JobKind.FIT,
                f"fit-result-{index}",
                progress=False,
                fit_policy=_fit_policy(),
            ),
            Money(50_000),
            current,
        )
        current += timedelta(days=1)

    blocked = scheduler.enqueue(
        _spec(JobKind.FIT, "circuit", cost=Money(60_000), fit_policy=_fit_policy()),
        dependencies,
        current,
    )
    report = scheduler.reconcile(current)
    assert scheduler.job(blocked.id).state is JobState.PENDING
    assert any(blocker.code is BlockerCode.CIRCUIT_OPEN for blocker in report.blockers)

    scheduler.resume_revision(_REVISION, current)
    scheduler.reconcile(current)
    assert scheduler.job(blocked.id).state is JobState.READY
    scheduler.close()


def test_hard_daily_budget_reservation_prevents_second_claim(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, policy=_policy(daily_budget=Money(100_000)))
    first = scheduler.enqueue(
        _spec(JobKind.TRACE_SYNC, "first", cost=Money(60_000)),
        (),
        _NOW,
    )
    second = scheduler.enqueue(
        _spec(JobKind.TRACE_SYNC, "second", cost=Money(60_000)),
        (),
        _NOW,
    )

    lease = scheduler.claim(WorkerId("worker-1"), _NOW)
    assert lease is not None
    assert lease.job.id == first.id
    assert scheduler.claim(WorkerId("worker-2"), _NOW) is None
    assert scheduler.job(second.id).state is JobState.READY
    budget = scheduler.budget(_NOW.date())
    assert budget.reserved == Money(60_000)
    scheduler.close()


def test_two_claimers_cannot_race_through_the_last_budget_slot(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.sqlite3"
    policy = _policy(daily_budget=Money(100_000))
    first_scheduler = LocalScheduler(path, policy)
    first_scheduler.enqueue(
        _spec(JobKind.TRACE_SYNC, "race-1", cost=Money(60_000)),
        (),
        _NOW,
    )
    first_scheduler.enqueue(
        _spec(JobKind.TRACE_SYNC, "race-2", cost=Money(60_000)),
        (),
        _NOW,
    )
    second_scheduler = LocalScheduler(path, policy)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first_scheduler.claim,
            WorkerId("worker-1"),
            _NOW,
        )
        second_future = executor.submit(
            second_scheduler.claim,
            WorkerId("worker-2"),
            _NOW,
        )
        leases = tuple(
            lease for lease in (first_future.result(), second_future.result()) if lease is not None
        )

    assert len(leases) == 1
    assert first_scheduler.budget(_NOW.date()).reserved == Money(60_000)
    second_scheduler.close()
    first_scheduler.close()


def test_heartbeat_materializes_pipeline_once_and_excludes_ofw_evidence(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    heartbeat = Heartbeat(scheduler, HeartbeatOwner("daemon-1"))
    evidence = HeartbeatEvidence(
        SourceWindowId("window-1"),
        EvidenceOrigin.PRODUCTION,
        new_verified_traces=10,
        confirmed_cluster_ready=True,
    )

    first = heartbeat.tick(_REVISION, evidence, _NOW)
    second = heartbeat.tick(_REVISION, evidence, _NOW + timedelta(seconds=30))
    excluded = heartbeat.tick(
        _REVISION,
        HeartbeatEvidence(
            SourceWindowId("ofw-window"),
            EvidenceOrigin.OFW_CONTROL_PLANE,
            new_verified_traces=100,
            confirmed_cluster_ready=True,
        ),
        _NOW + timedelta(seconds=60),
    )

    assert len(first.created) == 6
    assert second.created == ()
    assert excluded.created == ()
    assert all(job.state in (JobState.PENDING, JobState.READY) for job in scheduler.jobs())
    with pytest.raises(SchedulerError) as raised:
        Heartbeat(scheduler, HeartbeatOwner("daemon-2")).tick(
            _REVISION,
            evidence,
            _NOW + timedelta(seconds=61),
        )
    assert raised.value.code is SchedulerErrorCode.HEARTBEAT_LEASE_HELD
    scheduler.close()


@dataclass(frozen=True, slots=True)
class _TraceHandler:
    kind: JobKind = JobKind.TRACE_SYNC

    def execute(self, job: JobSpec, context: JobContext) -> JobExecution:
        assert context.lease.job.spec == job
        return JobExecution(
            JobResult(
                ResultId(f"result-{job.source.value}"),
                job.kind,
                job.revision_id,
                None,
                job.fit_policy_digest,
                True,
            ),
            Money(1_000),
        )


def test_worker_dispatches_typed_handler_and_commits_once(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    job = scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "worker"), (), _NOW)
    handler: JobHandler = _TraceHandler()
    worker = Worker(scheduler, WorkerId("worker"), (handler,))

    completed = worker.run_once(_NOW)

    assert completed is not None
    assert completed.id == job.id
    assert scheduler.job(job.id).state is JobState.SUCCEEDED
    assert worker.run_once(_NOW) is None
    scheduler.close()


def test_worker_can_renew_a_live_lease(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    scheduler.enqueue(_spec(JobKind.TRACE_SYNC, "renew"), (), _NOW)
    lease = scheduler.claim(WorkerId("worker"), _NOW)
    assert lease is not None
    scheduler.start(lease, _NOW)

    renewed = scheduler.renew(lease, _NOW + timedelta(seconds=50))
    scheduler.reconcile(_NOW + timedelta(seconds=70))

    assert renewed.expires_at == _NOW + timedelta(seconds=110)
    assert scheduler.job(lease.job.id).state is JobState.RUNNING
    scheduler.close()
