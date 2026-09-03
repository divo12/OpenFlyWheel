from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ofw.evaluation.langfuse import OutcomeScoreSubmission
from ofw.evaluation.outcome import OutcomeEvaluation, RunSide
from ofw.evolution.candidate import TraceMatch, TraceMatchRequest, candidate_policy_digest
from ofw.evolution.gate import PromotionStatus, decide_promotion
from ofw.evolution.integration import (
    HarborEvidenceService,
    PreparedExperimentIntegration,
    RunEvidenceInput,
)
from ofw.evolution.ledger import (
    EvolutionEventDraft,
    EvolutionEventType,
    EvolutionStarted,
    FileEvolutionLedger,
)
from ofw.evolution.publication import (
    PublicationService,
    RollbackRequest,
)
from ofw.observability.langfuse.domain import ScoreId
from ofw.preparation.contracts import (
    BaselineConfiguration,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import ExperimentPolicySnapshot, build_experiment_policy

_WHEN = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _Locator:
    def __init__(self) -> None:
        self.requests: list[TraceMatchRequest] = []

    def locate(self, request: TraceMatchRequest) -> TraceMatch:
        self.requests.append(request)
        return TraceMatch(
            trace_id=f"trace-{request.session_id}-{request.task_id}",
            blocker=None,
            cost_usd=0.1,
        )


class _Store:
    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        return OutcomeScoreSubmission(
            score_id=ScoreId(f"score-{outcome.task_id.value}-{outcome.trace_id.value}"),
            trace_id=outcome.trace_id,
        )


class _Runner:
    def __init__(self, summary: ExperimentSummary) -> None:
        self.summary = summary

    def validate(
        self,
        benchmark_root: Path,
        harbor_executable: Path,
        harbor_config: Path,
    ) -> ExperimentControls:
        del benchmark_root, harbor_executable, harbor_config
        return self.summary_controls

    def start(self, run: ExperimentRun) -> int:
        del run
        return 1

    def summarize(self, run: ExperimentRun) -> ExperimentSummary:
        del run
        return self.summary

    def cancel(self, run: ExperimentRun, process_id: int | None) -> None:
        del run, process_id

    @property
    def summary_controls(self) -> ExperimentControls:
        return _controls()


def _controls() -> ExperimentControls:
    return ExperimentControls(
        model="model",
        task_ids=tuple(f"task-{index}" for index in range(1, 11)),
        benchmark_config_digest="sha256:" + "b" * 64,
        verifier="verifier",
        environment="itsm-bench",
        concurrency=1,
        max_retries=0,
    )


def _summary(passes: tuple[str, ...]) -> ExperimentSummary:
    controls = _controls()
    return ExperimentSummary(
        trials=tuple(
            ExperimentTrial(
                task_id=task_id,
                task_checksum=f"checksum-{task_id}",
                exception=False,
                verdict=None,
                reward=1.0 if task_id in passes else 0.0,
                started_at=_WHEN + timedelta(minutes=index),
                finished_at=_WHEN + timedelta(minutes=index, seconds=30),
                evaluated_at=_WHEN + timedelta(minutes=index, seconds=31),
                evidence=(f"harbor://run/{task_id}",),
            )
            for index, task_id in enumerate(controls.task_ids)
        )
    )


def _run(
    root: Path,
    run_id: str,
    commit: str,
    session_id: str,
) -> ExperimentRun:
    return ExperimentRun(
        run_id=run_id,
        benchmark_root=root / "benchmark",
        harbor_executable=Path("/bin/harbor"),
        harbor_config=root / "benchmark/config.json",
        job_path=root / "benchmark/jobs" / run_id,
        log_path=root / "control" / f"{run_id}.log",
        source_root=root,
        release=commit,
        session_id=session_id,
        controls=_controls(),
    )


def _policy(root: Path, initial: str) -> ExperimentPolicySnapshot:
    request = PrepareWorkspaceInput(
        experiment_id="experiment-one",
        harness_root=root,
        base_ref="HEAD",
        worktree_parent=root.parent,
        benchmark_root=root / "benchmark",
        harbor_executable=Path("/bin/harbor"),
        harbor_config=Path("config.json"),
        expected_task_count=10,
        editable_paths=(Path("prompt.md"),),
        goal="Improve quality",
        quality_target=1.0,
        max_iterations=3,
        no_improvement_limit=2,
        max_baseline_seconds=600,
    )
    return build_experiment_policy(
        request,
        PreparedGitWorkspace(
            branch_name="ofw/experiment-one",
            worktree_path=root,
            base_commit=initial,
            initialization_commit=initial,
            program_path=root / "PROGRAM.md",
        ),
        BaselineConfiguration(
            model="model",
            task_ids=_controls().task_ids,
            benchmark_config_digest=_controls().benchmark_config_digest,
            verifier="verifier",
            environment="itsm-bench",
        ),
    )


def test_ten_task_loop_rejects_accepts_rolls_forward_and_reruns_after_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "harness"
    root.mkdir()
    (root / "PROGRAM.md").write_text("managed\n", encoding="utf-8")
    (root / "experiment_config.yaml").write_text("benchmark: itsm-bench\n", encoding="utf-8")
    (root / "prompt.md").write_text("initial\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    _git(root, "branch", "-m", "ofw/experiment-one")
    initial = _git(root, "rev-parse", "HEAD")
    policy = _policy(root, initial)
    ledger = FileEvolutionLedger()
    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            experiment_id="experiment-one",
            payload=EvolutionStarted(
                policy_digest=candidate_policy_digest(policy),
                accepted_commit=initial,
                accepted_release_id="initial",
            ),
            occurred_at=_WHEN,
            causation_id="start",
            correlation_id="start",
        ),
    )
    _git(root, "switch", "-c", "candidate")
    (root / "prompt.md").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", "prompt.md")
    _git(root, "commit", "-qm", "candidate")
    candidate_commit = _git(root, "rev-parse", "HEAD")
    candidate_tree = _git(root, "rev-parse", "HEAD^{tree}")
    _git(root, "switch", "ofw/experiment-one")

    locator = _Locator()
    store = _Store()
    evidence = HarborEvidenceService(locator, store)
    baseline = PreparedExperimentIntegration(_Runner(_summary(())), evidence).poll(
        RunEvidenceInput(
            run=_run(root, "baseline-run", initial, "baseline-session"),
            side=RunSide.ACCEPTED,
            policy_digest=candidate_policy_digest(policy),
            controls_digest=policy.controls_digest,
            evaluated_commit=initial,
            evaluated_tree=_git(root, "rev-parse", "HEAD^{tree}"),
            controls=_controls(),
        )
    )
    assert baseline is not None

    rejected = PreparedExperimentIntegration(_Runner(_summary(())), evidence).poll(
        RunEvidenceInput(
            run=_run(root, "candidate-rejected", candidate_commit, "reject-session"),
            side=RunSide.CANDIDATE,
            policy_digest=candidate_policy_digest(policy),
            controls_digest=policy.controls_digest,
            evaluated_commit=candidate_commit,
            evaluated_tree=candidate_tree,
            controls=_controls(),
        )
    )
    assert rejected is not None
    assert decide_promotion(policy, baseline, rejected).status is PromotionStatus.REJECT

    accepted_candidate = PreparedExperimentIntegration(
        _Runner(_summary(("task-1",))), evidence
    ).poll(
        RunEvidenceInput(
            run=_run(root, "candidate-accepted", candidate_commit, "accept-session"),
            side=RunSide.CANDIDATE,
            policy_digest=candidate_policy_digest(policy),
            controls_digest=policy.controls_digest,
            evaluated_commit=candidate_commit,
            evaluated_tree=candidate_tree,
            controls=_controls(),
        )
    )
    assert accepted_candidate is not None
    decision = decide_promotion(policy, baseline, accepted_candidate)
    assert decision.status is PromotionStatus.ACCEPT

    publication = PublicationService(ledger)
    current = publication.current_accepted(root, "experiment-one", candidate_policy_digest(policy))
    published = publication.promote(
        root=root,
        experiment_id="experiment-one",
        policy_digest=candidate_policy_digest(policy),
        operation_id="sha256:" + "1" * 64,
        publication_id="release-1",
        expected=current.cas_token,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        gate=decision,
    )
    assert published.parent_publication_id == "initial"

    rolled_back = publication.rollback(
        RollbackRequest(
            root=root,
            experiment_id="experiment-one",
            policy_digest=candidate_policy_digest(policy),
            operation_id="sha256:" + "2" * 64,
            publication_id="release-2",
            expected=publication.current_accepted(
                root, "experiment-one", candidate_policy_digest(policy)
            ).cas_token,
            target_publication_id="initial",
        )
    )
    assert rolled_back.publication_id == "release-2"
    assert rolled_back.parent_publication_id == "release-1"
    assert rolled_back.content_commit != initial
    assert rolled_back.content_tree == _git(root, "rev-parse", f"{initial}^{{tree}}")

    fresh_run = replace(
        _run(root, "rollback-run", rolled_back.content_commit, "rollback-session"),
        source_root=root,
    )
    fresh = PreparedExperimentIntegration(_Runner(_summary(())), evidence).poll(
        RunEvidenceInput(
            run=fresh_run,
            side=RunSide.ACCEPTED,
            policy_digest=candidate_policy_digest(policy),
            controls_digest=policy.controls_digest,
            evaluated_commit=rolled_back.content_commit,
            evaluated_tree=rolled_back.content_tree,
            controls=_controls(),
        )
    )
    assert fresh is not None
    assert fresh.run_id == "rollback-run"
    assert fresh.run_id != baseline.run_id
    assert locator.requests[-1].session_id == "rollback-session"
    assert locator.requests[-1].release == rolled_back.content_commit
    assert len(fresh.task_ids) == 10
