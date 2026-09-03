from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ofw.evaluation.langfuse import OutcomeScoreSubmission
from ofw.evaluation.outcome import (
    OutcomeEvaluation,
    RunSide,
)
from ofw.evolution.candidate import (
    CandidateErrorCode,
    CandidateFailure,
    TraceMatch,
    TraceMatchRequest,
)
from ofw.evolution.integration import (
    HarborEvidenceService,
    RunEvidenceInput,
    accepted_view,
    baseline_run_for_evidence,
)
from ofw.observability.langfuse.domain import ScoreId
from ofw.preparation.contracts import (
    BaselineRun,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
)

_WHEN = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
_COMMIT = "a" * 40
_TREE = "b" * 40
_POLICY = "sha256:" + "c" * 64


class _TraceLocator:
    def __init__(self) -> None:
        self.requests: list[TraceMatchRequest] = []

    def locate(self, request: TraceMatchRequest) -> TraceMatch:
        self.requests.append(request)
        return TraceMatch(trace_id=f"trace-{request.task_id}", blocker=None, cost_usd=0.25)


class _OutcomeStore:
    def __init__(self) -> None:
        self.outcomes: list[OutcomeEvaluation] = []

    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        self.outcomes.append(outcome)
        return OutcomeScoreSubmission(
            score_id=ScoreId(f"score-{outcome.task_id.value}"),
            trace_id=outcome.trace_id,
        )


class _FailingOutcomeStore(_OutcomeStore):
    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        del outcome
        raise RuntimeError("provider details must stay private")


class _Runner:
    def __init__(self, summary: ExperimentSummary | None) -> None:
        self.summary = summary
        self.calls = 0

    def validate(
        self,
        benchmark_root: Path,
        harbor_executable: Path,
        harbor_config: Path,
    ) -> ExperimentControls:
        del benchmark_root, harbor_executable, harbor_config
        return _controls()

    def start(self, run: ExperimentRun) -> int:
        del run
        return 1

    def summarize(self, run: ExperimentRun) -> ExperimentSummary | None:
        del run
        self.calls += 1
        return self.summary

    def cancel(self, run: ExperimentRun, process_id: int | None) -> None:
        del run, process_id


def _controls() -> ExperimentControls:
    return ExperimentControls(
        model="model",
        task_ids=tuple(f"task-{index}" for index in range(1, 11)),
        benchmark_config_digest="sha256:" + "d" * 64,
        verifier="itsm-bench",
        environment="itsm-bench",
        concurrency=1,
        max_retries=0,
    )


def _summary(controls: ExperimentControls) -> ExperimentSummary:
    trials = tuple(
        ExperimentTrial(
            task_id=task_id,
            task_checksum=f"checksum-{task_id}",
            exception=False,
            verdict=None,
            reward=1.0 if index == 0 else 0.0,
            started_at=_WHEN + timedelta(minutes=index),
            finished_at=_WHEN + timedelta(minutes=index, seconds=30),
            evaluated_at=_WHEN + timedelta(minutes=index, seconds=31),
            evidence=(f"harbor://run/{task_id}",),
        )
        for index, task_id in enumerate(controls.task_ids)
    )
    return ExperimentSummary(trials=trials)


def _run() -> ExperimentRun:
    return ExperimentRun(
        run_id="run-1",
        benchmark_root=Path("/benchmark"),
        harbor_executable=Path("/bin/harbor"),
        harbor_config=Path("/benchmark/config.json"),
        job_path=Path("/benchmark/jobs/run-1"),
        log_path=Path("/control/run.log"),
        source_root=Path("/candidate"),
        release=_COMMIT,
        session_id="session-1",
        controls=_controls(),
    )


def test_harbor_evidence_service_builds_ordered_authoritative_receipt() -> None:
    controls = _controls()
    locator = _TraceLocator()
    store = _OutcomeStore()
    receipt = HarborEvidenceService(locator, store).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.CANDIDATE,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        _summary(controls),
    )

    assert receipt.side is RunSide.CANDIDATE
    assert receipt.task_ids == controls.task_ids
    assert tuple(item.task_id for item in receipt.outcome_receipts) == controls.task_ids
    assert not receipt.blockers
    assert len(locator.requests) == 10
    assert len(store.outcomes) == 10
    assert "prompt" not in receipt.model_dump_json()


def test_prepared_integration_polls_baseline_or_rollback_with_the_shared_reducer() -> None:
    from ofw.evolution.integration import PreparedExperimentIntegration

    controls = _controls()
    runner = _Runner(_summary(controls))
    evidence = HarborEvidenceService(_TraceLocator(), _OutcomeStore())
    integration = PreparedExperimentIntegration(runner, evidence)
    request = RunEvidenceInput(
        run=_run(),
        side=RunSide.ACCEPTED,
        policy_digest=_POLICY,
        controls_digest="sha256:" + "e" * 64,
        evaluated_commit=_COMMIT,
        evaluated_tree=_TREE,
        controls=controls,
    )

    receipt = integration.poll(request)

    assert receipt is not None
    assert receipt.side is RunSide.ACCEPTED
    assert receipt.run_id == "run-1"
    assert runner.calls == 1


def test_prepared_integration_keeps_incomplete_harbor_runs_unresolved() -> None:
    from ofw.evolution.integration import PreparedExperimentIntegration

    controls = _controls()
    runner = _Runner(None)
    integration = PreparedExperimentIntegration(
        runner,
        HarborEvidenceService(_TraceLocator(), _OutcomeStore()),
    )
    request = RunEvidenceInput(
        run=_run(),
        side=RunSide.ACCEPTED,
        policy_digest=_POLICY,
        controls_digest="sha256:" + "e" * 64,
        evaluated_commit=_COMMIT,
        evaluated_tree=_TREE,
        controls=controls,
    )

    assert integration.poll(request) is None
    assert runner.calls == 1


def test_baseline_run_adapter_uses_initial_commit_and_experiment_identity() -> None:
    controls = _controls()
    baseline = BaselineRun(
        experiment_id="experiment-one",
        benchmark_root=Path("/benchmark"),
        harbor_executable=Path("/bin/harbor"),
        harbor_config=Path("/benchmark/config.json"),
        job_path=Path("/benchmark/jobs/experiment-one"),
        log_path=Path("/control/baseline.log"),
        worktree_path=Path("/accepted"),
        initialization_commit=_COMMIT,
        controls=controls,
    )

    run = baseline_run_for_evidence(baseline)

    assert (run.run_id, run.release, run.session_id, run.source_root) == (
        "experiment-one",
        _COMMIT,
        "experiment-one",
        Path("/accepted"),
    )


def test_evidence_input_requires_run_release_to_equal_evaluated_commit() -> None:
    controls = _controls()
    with pytest.raises(CandidateFailure) as raised:
        RunEvidenceInput(
            run=_run(),
            side=RunSide.ACCEPTED,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit="f" * 40,
            evaluated_tree=_TREE,
            controls=controls,
        )

    assert raised.value.code is CandidateErrorCode.INVALID_RESULT


@pytest.mark.parametrize("field", ("policy_digest", "evaluated_commit"))
def test_evidence_input_rejects_invalid_text_authority(field: str) -> None:
    controls = _controls()

    with pytest.raises(CandidateFailure) as raised:
        if field == "policy_digest":
            RunEvidenceInput(
                run=_run(),
                side=RunSide.ACCEPTED,
                policy_digest="invalid",
                controls_digest="sha256:" + "e" * 64,
                evaluated_commit=_COMMIT,
                evaluated_tree=_TREE,
                controls=controls,
            )
        else:
            RunEvidenceInput(
                run=_run(),
                side=RunSide.ACCEPTED,
                policy_digest=_POLICY,
                controls_digest="sha256:" + "e" * 64,
                evaluated_commit="invalid",
                evaluated_tree=_TREE,
                controls=controls,
            )

    assert raised.value.code is CandidateErrorCode.INVALID_RESULT


def test_evidence_input_rejects_control_drift() -> None:
    with pytest.raises(CandidateFailure) as raised:
        RunEvidenceInput(
            run=_run(),
            side=RunSide.ACCEPTED,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=replace(_controls(), model="other"),
        )

    assert raised.value.code is CandidateErrorCode.CONTROLS_DRIFT


def test_harbor_evidence_service_preserves_authoritative_blockers_and_verdicts() -> None:
    controls = _controls()
    trials = list(_summary(controls).trials)
    trials[0] = replace(trials[0], exception=True)
    trials[1] = replace(trials[1], reward=0.5)
    trials[2] = replace(trials[2], reward=None, verdict="abstain")
    trials[3] = replace(trials[3], reward=None, verdict=None)
    store = _OutcomeStore()

    receipt = HarborEvidenceService(_TraceLocator(), store).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.ACCEPTED,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        ExperimentSummary(trials=tuple(trials)),
    )

    assert tuple(item.task_id for item in receipt.blockers[:3]) == (
        "task-1",
        "task-2",
        "task-4",
    )
    assert receipt.blockers[0].code == "unverified"
    assert receipt.blockers[1].code == "unsupported_reward"
    assert any(item.verdict.value == "abstain" for item in receipt.outcome_receipts)


def test_harbor_evidence_service_sanitizes_outcome_store_failures() -> None:
    controls = _controls()

    with pytest.raises(CandidateFailure) as raised:
        HarborEvidenceService(_TraceLocator(), _FailingOutcomeStore()).evaluate(
            RunEvidenceInput(
                run=_run(),
                side=RunSide.ACCEPTED,
                policy_digest=_POLICY,
                controls_digest="sha256:" + "e" * 64,
                evaluated_commit=_COMMIT,
                evaluated_tree=_TREE,
                controls=controls,
            ),
            _summary(controls),
        )

    assert raised.value.code is CandidateErrorCode.OUTCOME_STORE_FAILED
    assert "provider details" not in str(raised.value)


def test_accepted_view_reuses_candidate_evidence_with_new_deterministic_identity() -> None:
    controls = _controls()
    receipt = HarborEvidenceService(_TraceLocator(), _OutcomeStore()).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.CANDIDATE,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        _summary(controls),
    )

    current = accepted_view(receipt)
    assert current.side is RunSide.ACCEPTED
    assert current.run_id == receipt.run_id
    assert current.evaluated_commit == receipt.evaluated_commit
    assert current.outcome_receipts == receipt.outcome_receipts
    assert current.receipt_id != receipt.receipt_id
    assert accepted_view(receipt) == current


def test_baseline_uses_the_same_reducer_as_an_accepted_run() -> None:
    controls = _controls()
    receipt = HarborEvidenceService(_TraceLocator(), _OutcomeStore()).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.ACCEPTED,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        _summary(controls),
    )

    assert receipt.side is RunSide.ACCEPTED


def test_harbor_evidence_service_blocks_ambiguous_mapping_without_writing() -> None:
    class AmbiguousLocator(_TraceLocator):
        def locate(self, request: TraceMatchRequest) -> TraceMatch:
            self.requests.append(request)
            from ofw.evolution.candidate import CandidateBlockerCode

            return TraceMatch(
                trace_id=None,
                blocker=CandidateBlockerCode.TRACE_AMBIGUOUS,
            )

    controls = _controls()
    locator = AmbiguousLocator()
    store = _OutcomeStore()

    receipt = HarborEvidenceService(locator, store).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.ACCEPTED,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        _summary(controls),
    )
    assert len(receipt.blockers) == 10
    assert all(item.code == "trace_ambiguous" for item in receipt.blockers)
    assert not store.outcomes


def test_harbor_evidence_service_rejects_late_or_missing_task_partition() -> None:
    controls = _controls()
    summary = _summary(controls)
    with_exception = ExperimentSummary(
        trials=(summary.trials[1],) + summary.trials[:1] + summary.trials[2:]
    )

    with pytest.raises(CandidateFailure) as raised:
        HarborEvidenceService(_TraceLocator(), _OutcomeStore()).evaluate(
            RunEvidenceInput(
                run=_run(),
                side=RunSide.CANDIDATE,
                policy_digest=_POLICY,
                controls_digest="sha256:" + "e" * 64,
                evaluated_commit=_COMMIT,
                evaluated_tree=_TREE,
                controls=controls,
            ),
            with_exception,
        )
    assert raised.value.code is CandidateErrorCode.INVALID_RESULT


def test_accepted_view_is_idempotent_for_an_accepted_receipt() -> None:
    controls = _controls()
    receipt = HarborEvidenceService(_TraceLocator(), _OutcomeStore()).evaluate(
        RunEvidenceInput(
            run=_run(),
            side=RunSide.CANDIDATE,
            policy_digest=_POLICY,
            controls_digest="sha256:" + "e" * 64,
            evaluated_commit=_COMMIT,
            evaluated_tree=_TREE,
            controls=controls,
        ),
        _summary(controls),
    )

    accepted = accepted_view(receipt)
    assert accepted_view(accepted) is accepted
