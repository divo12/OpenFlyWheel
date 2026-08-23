"""Offline proof of the complete trace-to-review flywheel."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ofw import (
    BenchmarkPolicy,
    CandidateBuilder,
    CandidateEvidence,
    CandidatePolicy,
    ChangePrediction,
    ClusterFamilyId,
    ClusterPartitionRule,
    ClusterReview,
    ClusterReviewDecision,
    ClusterReviewerId,
    ComponentKind,
    ConsentStatus,
    DataLicense,
    DiagnosisReview,
    EvidenceOrigin,
    ExportPartition,
    ExportPolicy,
    FileEdit,
    FitPolicy,
    FunctionName,
    Harness,
    Heartbeat,
    HeartbeatEvidence,
    HeartbeatOwner,
    JobKind,
    JobResult,
    JobSpec,
    JobState,
    LangfuseProject,
    LocalProcess,
    LocalScheduler,
    Mine,
    MineExports,
    MiningPolicy,
    ModuleName,
    Money,
    PairedEvidencePolicy,
    ProcessLimits,
    PromotionJobHandler,
    PromotionMode,
    PromotionPolicy,
    PromotionRequest,
    PromotionRequestResolver,
    PromotionService,
    PythonDiagnoser,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
    QuietHours,
    ResultId,
    ScoreName,
    SourceWindowId,
    StageBudgets,
    StatisticalGateMode,
    Tool,
    TraceQualityThreshold,
    TraceWindow,
    Worker,
    WorkerId,
    ofw,
)
from ofw.contracts import AssetAccess
from ofw.diagnosis import ClusterId, DiagnosisResult, DiagnosisRun
from ofw.observability.langfuse.domain import CollectionCapabilityReason, ScoreSource, TraceId
from ofw.promotion import GitRemote
from ofw.scheduler import AutomationPolicy
from tests.test_promotion import _PullRequests

_NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
_TRACE_NAMES = ("admission", "frontier", "good", "regression", "selection")


@dataclass(slots=True)
class _LangfuseState:
    revision_id: str = "pending"
    writes: int = 0


def _observation(trace: str, index: int, revision_id: str) -> str:
    return (
        "{"
        f'"id":"observation-{trace}",'
        f'"traceId":"{trace}",'
        f'"startTime":"2026-08-22T00:00:{index:02d}Z",'
        '"endTime":"2026-08-22T00:01:00Z",'
        '"projectId":"project-offline",'
        '"parentObservationId":null,'
        '"type":"AGENT",'
        '"isRootObservation":true,'
        f'"name":"{trace}-shape",'
        '"environment":"production",'
        f'"sessionId":"session-{trace}",'
        f'"metadata":{{"ofw.harness.revision":"{revision_id}"}},'
        '"release":"offline-v1",'
        '"modelId":null,'
        '"inputPrice":null,'
        '"outputPrice":null,'
        '"totalPrice":null'
        "}"
    )


def _score(trace: str, index: int) -> str:
    value = "true" if trace == "good" else "false"
    return (
        "{"
        f'"id":"score-{trace}",'
        '"projectId":"project-offline",'
        '"name":"correctness",'
        f'"value":{value},'
        '"dataType":"BOOLEAN",'
        '"source":"ANNOTATION",'
        f'"timestamp":"2026-08-22T00:02:{index:02d}Z",'
        '"environment":"production",'
        f'"createdAt":"2026-08-22T00:02:{index:02d}Z",'
        f'"updatedAt":"2026-08-22T00:02:{index:02d}Z",'
        f'"subject":{{"kind":"trace","id":"{trace}"}}'
        "}"
    )


def _langfuse_handler(state: _LangfuseState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/public/health":
                payload = '{"version":"4.7.0","status":"OK"}'
            elif path == "/api/public/v2/observations":
                records = ",".join(
                    _observation(trace, index, state.revision_id)
                    for index, trace in enumerate(_TRACE_NAMES)
                )
                payload = f'{{"data":[{records}],"meta":{{"cursor":null}}}}'
            elif path == "/api/public/v3/scores":
                records = ",".join(_score(trace, index) for index, trace in enumerate(_TRACE_NAMES))
                payload = f'{{"data":[{records}],"meta":{{"limit":100,"cursor":null}}}}'
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = payload.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            state.writes += 1
            self.send_response(405)
            self.end_headers()

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            del code, size

    return Handler


def _run_git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _harness(tmp_path: Path, base_url: str, monkeypatch: pytest.MonkeyPatch) -> Harness:
    root = tmp_path / "offline-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "tool.py").write_text(
        "def run(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    (root / "agent_loop.py").write_text(
        "from tool import run\ndef run_case(value: str) -> str:\n    return run(value)\n",
        encoding="utf-8",
    )
    (root / "verifiers.py").write_text(
        "from __future__ import annotations\n"
        "from ofw import RunResult, VerifierResult, VerifierVerdict\n"
        "def verify(result: RunResult) -> VerifierResult:\n"
        "    output = result.output or ''\n"
        "    target = any(name in output for name in ('frontier', 'selection', 'admission'))\n"
        "    passed = ('FIXED' in output) if target else ('BROKEN' not in output)\n"
        "    verdict = VerifierVerdict.PASS if passed else VerifierVerdict.FAIL\n"
        "    return VerifierResult(verdict, 1.0 if passed else 0.0, 'offline')\n",
        encoding="utf-8",
    )
    (root / "diagnoser.py").write_text(
        "from __future__ import annotations\n"
        "from ofw import (ComponentKind, EvidenceAnchor, EvidenceAnchorKind, MechanismKey, "
        "Severity, TraceDiagnosis)\n"
        "from ofw.mine import TraceSnapshot\n"
        "def diagnose(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    trace = snapshot.trace.id\n"
        "    observation = snapshot.observations[0]\n"
        "    return TraceDiagnosis.proposed(\n"
        "        trace, MechanismKey(f'{trace.value}-failure'), f'{trace.value} failure',\n"
        "        'offline planted failure',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, observation.id.value),),\n"
        "        (ComponentKind.TOOL,), Severity.HIGH, 0.99,\n"
        "    )\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "offline baseline")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-offline")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-offline")
    project = LangfuseProject.from_env(
        environment="production",
        base_url=base_url,
        allow_private_network=True,
    )
    harness = Harness("offline-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_tools(Tool("run", ofw.editable(Path("tool.py"))))
    harness.connect_middleware(Path("diagnoser.py"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=5))))
    harness.connect_lifecycle(
        PythonLoop(PythonEntrypoint(ModuleName("agent_loop"), FunctionName("run_case")))
    )
    harness.connect_verifiers(
        PythonVerifier(
            "offline",
            PythonEntrypoint(ModuleName("verifiers"), FunctionName("verify")),
        )
    )
    harness.connect_observability(project)
    harness.process()
    return harness


def _cluster_id(diagnosis: DiagnosisResult, trace: str) -> ClusterId:
    return next(
        cluster.id for cluster in diagnosis.clusters if TraceId(trace) in cluster.source_trace_ids
    )


def _finish(
    scheduler: LocalScheduler,
    expected: JobSpec,
    result: JobResult,
    now: datetime,
) -> None:
    lease = scheduler.claim(WorkerId(f"worker-{expected.kind.value}"), now)
    assert lease is not None
    assert lease.job.spec == expected
    scheduler.start(lease, now)
    scheduler.succeed(lease, result, Money(100), now)


@dataclass(frozen=True, slots=True)
class _PromotionRequests:
    fit_result_id: ResultId
    request: PromotionRequest

    def resolve(self, fit_result_id: ResultId) -> PromotionRequest:
        assert fit_result_id == self.fit_result_id
        return self.request


def _scheduler_policy(fit_policy: FitPolicy) -> AutomationPolicy:
    return AutomationPolicy(
        timedelta(seconds=30),
        timedelta(seconds=45),
        timedelta(minutes=2),
        timedelta(seconds=5),
        timedelta(hours=1),
        2,
        3,
        1,
        Money(1_000_000),
        QuietHours(time(22), time(6)),
        fit_policy,
        StageBudgets(
            Money(10_000),
            Money(20_000),
            Money(5_000),
            Money(5_000),
            Money(5_000),
            Money(50_000),
            Money(10_000),
        ),
    )


def test_offline_trace_to_review_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _LangfuseState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _langfuse_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        harness = _harness(tmp_path, f"http://127.0.0.1:{port}", monkeypatch)
        revision = harness.current_revision
        assert revision is not None
        state.revision_id = str(revision.id)
        window = TraceWindow(_NOW.replace(hour=0), _NOW.replace(hour=1))

        collection = ofw.collect(
            revision,
            window=window,
            store_path=tmp_path / "collection.sqlite3",
        )
        mine = Mine(
            revision,
            collection,
            MiningPolicy(
                (ScoreName("correctness"),),
                (ScoreSource.ANNOTATION,),
                TraceQualityThreshold.COMPLETE,
            ),
        ).run()
        diagnoser = PythonDiagnoser(
            PythonEntrypoint(ModuleName("diagnoser"), FunctionName("diagnose")),
            ProcessLimits(timedelta(seconds=5)),
        )
        diagnosis_run = DiagnosisRun(harness, mine, diagnoser)
        diagnosis = diagnosis_run.run()
        diagnosis = DiagnosisReview(
            diagnosis,
            tuple(
                ClusterReview(
                    cluster.id,
                    cluster.revision,
                    cluster.content_digest,
                    ClusterReviewerId("offline-reviewer"),
                    ClusterReviewDecision.CONFIRM,
                    _NOW,
                )
                for cluster in diagnosis.clusters
            ),
        ).run()
        partitions = (
            ("frontier", ExportPartition.FRONTIER),
            ("regression", ExportPartition.REGRESSION),
            ("selection", ExportPartition.SELECTION),
            ("admission", ExportPartition.ADMISSION),
        )
        export_policy = ExportPolicy(
            tuple(partition for _trace, partition in partitions),
            0.2,
            DataLicense("offline-approved"),
            ConsentStatus.APPROVED,
            tuple(
                ClusterPartitionRule(
                    ClusterFamilyId(_cluster_id(diagnosis, trace).value),
                    partition,
                )
                for trace, partition in partitions
            ),
        )
        bundle = MineExports(revision, mine, diagnosis, export_policy).run()

        frontier_cluster = _cluster_id(diagnosis, "frontier")
        regression_case = next(
            case
            for case in bundle.developer_evals.cases
            if case.partition is ExportPartition.REGRESSION
        )
        tool_asset = next(
            asset
            for asset in revision.assets
            if asset.access is AssetAccess.FIT_EDITABLE
            and asset.source.relative_path == Path("tool.py")
        )
        candidate = CandidateBuilder(
            revision,
            CandidateEvidence(
                revision.id,
                (frontier_cluster,),
                (regression_case.id,),
                (),
            ),
            CandidatePolicy(1, 4096, (ComponentKind.TOOL,)),
        ).create(
            (
                FileEdit(
                    Path("tool.py"),
                    tool_asset.digest,
                    "def run(value: str) -> str:\n"
                    "    return value + (' FIXED' if 'regression' not in value else '')\n",
                ),
            ),
            ChangePrediction(
                "Fix the planted target without changing regression behavior.",
                (frontier_cluster,),
                (regression_case.id,),
                (ComponentKind.TOOL,),
                (),
                1.0,
                0.0,
                0.0,
            ),
        )
        fit_policy = FitPolicy(
            0.5,
            1.0,
            0,
            10.0,
            0.0,
            1.0,
            1.0,
            PairedEvidencePolicy(StatisticalGateMode.EFFECT_SIZE_ONLY, 0, 1.0),
        )
        campaign = ofw.fit(
            harness,
            bundle,
            (candidate,),
            benchmark_policy=BenchmarkPolicy(1, 10, 0, 0.25),
            policy=fit_policy,
        )
        fit_result = campaign.wait()
        assert fit_result.winner_id == candidate.candidate.id

        remote = tmp_path / "review.git"
        subprocess.run(("git", "init", "--bare", "-q", str(remote)), check=True)
        _run_git(harness.root, "remote", "add", "review", str(remote))
        promotion_request = PromotionRequest(
            campaign,
            fit_result,
            candidate,
            PromotionPolicy(
                PromotionMode.PULL_REQUEST,
                GitRemote("review", "main", "ofw"),
                False,
            ),
            None,
        )
        scheduler_path = tmp_path / "scheduler.sqlite3"
        scheduler_policy = _scheduler_policy(fit_policy)
        scheduler = LocalScheduler(scheduler_path, scheduler_policy)
        source = SourceWindowId("offline-window")
        heartbeat = Heartbeat(scheduler, HeartbeatOwner("offline-daemon"))
        heartbeat_report = heartbeat.tick(
            revision.id,
            HeartbeatEvidence(source, EvidenceOrigin.PRODUCTION, 5, True),
            _NOW,
        )
        assert len(heartbeat_report.created) == 7
        trace_job = next(job for job in scheduler.jobs() if job.spec.kind is JobKind.TRACE_SYNC)
        _finish(
            scheduler,
            trace_job.spec,
            JobResult(
                ResultId(str(collection.snapshot_digest)),
                JobKind.TRACE_SYNC,
                revision.id,
                None,
                None,
                True,
            ),
            _NOW,
        )
        scheduler.reconcile(_NOW)
        mine_job = next(job for job in scheduler.jobs() if job.spec.kind is JobKind.MINE)
        _finish(
            scheduler,
            mine_job.spec,
            JobResult(ResultId(str(mine.id)), JobKind.MINE, revision.id, None, None, True),
            _NOW,
        )
        scheduler.reconcile(_NOW)
        good_job = next(
            job for job in scheduler.jobs() if job.spec.kind is JobKind.EXPORT_GOOD_TRACES
        )
        export_job = next(
            job for job in scheduler.jobs() if job.spec.kind is JobKind.EXPORT_BENCH_EVAL
        )
        memory_job = next(
            job for job in scheduler.jobs() if job.spec.kind is JobKind.PROPOSE_MEMORY
        )
        _finish(
            scheduler,
            good_job.spec,
            JobResult(
                ResultId(bundle.good_traces.id),
                JobKind.EXPORT_GOOD_TRACES,
                revision.id,
                ResultId(str(mine.id)),
                None,
                True,
            ),
            _NOW,
        )
        _finish(
            scheduler,
            export_job.spec,
            JobResult(
                ResultId(bundle.id),
                JobKind.EXPORT_BENCH_EVAL,
                revision.id,
                ResultId(str(mine.id)),
                None,
                True,
            ),
            _NOW,
        )
        _finish(
            scheduler,
            memory_job.spec,
            JobResult(
                ResultId(bundle.memory.id),
                JobKind.PROPOSE_MEMORY,
                revision.id,
                ResultId(str(mine.id)),
                None,
                True,
            ),
            _NOW,
        )
        scheduler.reconcile(_NOW)
        fit_job = next(job for job in scheduler.jobs() if job.spec.kind is JobKind.FIT)
        _finish(
            scheduler,
            fit_job.spec,
            JobResult(
                ResultId(fit_result.id),
                JobKind.FIT,
                revision.id,
                ResultId(bundle.id),
                fit_policy.digest,
                True,
            ),
            _NOW,
        )
        scheduler.reconcile(_NOW)
        promotion_job = next(job for job in scheduler.jobs() if job.spec.kind is JobKind.PROMOTE)
        assert promotion_job.state is JobState.READY
        scheduler.close()
        scheduler = LocalScheduler(scheduler_path, scheduler_policy)
        assert scheduler.job(promotion_job.id).state is JobState.READY
        requests: PromotionRequestResolver = _PromotionRequests(
            ResultId(fit_result.id),
            promotion_request,
        )
        publisher = _PullRequests()
        worker = Worker(
            scheduler,
            WorkerId("promotion-worker"),
            (PromotionJobHandler(PromotionService(publisher, None), requests),),
        )

        completed = worker.run_once(_NOW)

        assert completed is not None
        assert completed.id == promotion_job.id
        assert completed.state is JobState.SUCCEEDED
        stored_promotion = scheduler.job(promotion_job.id).result
        assert stored_promotion is not None
        assert len(publisher.opened) == 1
        promotion = PromotionService(publisher, None).run(promotion_request, _NOW)
        assert stored_promotion.id == ResultId(promotion.id)
        assert len(publisher.opened) == 1
        assert collection.capability is CollectionCapabilityReason.READY
        assert collection.observation_count == 5
        assert mine.verified_failure_count == 4
        assert mine.verified_good_count == 1
        assert len(diagnosis.clusters) == 4
        assert bundle.ledger.validate()
        assert promotion.pull_request is not None
        assert promotion.deployment is None
        assert promotion.rollback.reverse_patch.read_bytes()
        budget = scheduler.budget(_NOW.date())
        assert budget.reserved == Money(0)
        assert budget.spent == Money(600)
        assert state.writes == 0
        scheduler.close()
        candidate.workspace.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
