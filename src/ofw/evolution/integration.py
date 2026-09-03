"""Shared prepared-ITSM Harbor evidence reduction for baseline and candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ofw.evaluation.outcome import (
    EvaluatedRunBlocker,
    EvaluatedRunReceipt,
    EvaluatedTaskReceipt,
    EvidenceReference,
    OutcomeEvaluation,
    RunSide,
    TaskId,
    VerifierId,
    VerifierVerdict,
)
from ofw.evolution.candidate import (
    CandidateBlockerCode,
    CandidateErrorCode,
    CandidateExperimentRunner,
    CandidateFailure,
    CandidateOutcomeStore,
    CandidateTraceLocator,
    TraceMatchRequest,
)
from ofw.observability.langfuse.domain import TraceId
from ofw.preparation.contracts import (
    BaselineRun,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class RunEvidenceInput:
    run: ExperimentRun
    side: RunSide
    policy_digest: str
    controls_digest: str
    evaluated_commit: str
    evaluated_tree: str
    controls: ExperimentControls

    def __post_init__(self) -> None:
        _validate_run_evidence_input(self)


@dataclass(frozen=True, slots=True)
class _OutcomeReduction:
    receipts: tuple[EvaluatedTaskReceipt, ...]
    blockers: tuple[EvaluatedRunBlocker, ...]


class HarborEvidenceService:
    """Turn one complete Harbor result into a receipt without storing trace payloads."""

    def __init__(
        self,
        locator: CandidateTraceLocator,
        outcome_store: CandidateOutcomeStore,
    ) -> None:
        self._locator = locator
        self._outcome_store = outcome_store

    def evaluate(
        self, request: RunEvidenceInput, summary: ExperimentSummary
    ) -> EvaluatedRunReceipt:
        _require_exact_tasks(summary, request.controls.task_ids)
        results = tuple(self._reduce_trial(trial, request) for trial in summary.trials)
        reduction = _partition_results(results)
        return EvaluatedRunReceipt.build(
            run_id=request.run.run_id,
            side=request.side,
            policy_digest=request.policy_digest,
            controls_digest=request.controls_digest,
            evaluated_commit=request.evaluated_commit,
            evaluated_tree=request.evaluated_tree,
            task_ids=request.controls.task_ids,
            outcome_receipts=reduction.receipts,
            blockers=reduction.blockers,
        )

    def _reduce_trial(
        self,
        trial: ExperimentTrial,
        request: RunEvidenceInput,
    ) -> EvaluatedTaskReceipt | EvaluatedRunBlocker:
        result = _authoritative_result(trial)
        if isinstance(result, EvaluatedRunBlocker):
            return result
        match = self._locator.locate(
            _trace_request(trial.task_id, trial.started_at, trial.finished_at, request)
        )
        if match.trace_id is None:
            return _trace_blocker(trial, match.blocker)
        outcome = _outcome(trial, request.controls, match.trace_id, result)
        try:
            submission = self._outcome_store.store(outcome)
        except Exception:
            raise CandidateFailure(
                CandidateErrorCode.OUTCOME_STORE_FAILED,
                trial.task_id,
            ) from None
        return EvaluatedTaskReceipt(
            task_id=trial.task_id,
            trace_id=match.trace_id,
            score_id=submission.score_id.value,
            verdict=result[0],
            verifier_id=outcome.verifier_id.value,
            normalized_score=result[1],
            cost_usd=match.cost_usd,
            latency_seconds=trial.latency_seconds,
        )


class PreparedExperimentIntegration:
    """Poll one prepared Harbor run and reduce its terminal evidence."""

    def __init__(
        self,
        runner: CandidateExperimentRunner,
        evidence: HarborEvidenceService,
    ) -> None:
        self._runner = runner
        self._evidence = evidence

    def poll(self, request: RunEvidenceInput) -> EvaluatedRunReceipt | None:
        summary = self._runner.summarize(request.run)
        if summary is None:
            return None
        return self._evidence.evaluate(request, summary)


def baseline_run_for_evidence(run: BaselineRun) -> ExperimentRun:
    """Adapt the prepared baseline run to the shared Harbor evidence contract."""
    return ExperimentRun(
        run_id=run.experiment_id,
        benchmark_root=run.benchmark_root,
        harbor_executable=run.harbor_executable,
        harbor_config=run.harbor_config,
        job_path=run.job_path,
        log_path=run.log_path,
        source_root=run.worktree_path,
        release=run.initialization_commit,
        session_id=run.experiment_id,
        controls=run.controls,
    )


def accepted_view(receipt: EvaluatedRunReceipt) -> EvaluatedRunReceipt:
    """Adopt accepted candidate evidence without rerunning its Harbor job."""
    if receipt.side is RunSide.ACCEPTED:
        return receipt
    return EvaluatedRunReceipt.build(
        run_id=receipt.run_id,
        side=RunSide.ACCEPTED,
        policy_digest=receipt.policy_digest,
        controls_digest=receipt.controls_digest,
        evaluated_commit=receipt.evaluated_commit,
        evaluated_tree=receipt.evaluated_tree,
        task_ids=receipt.task_ids,
        outcome_receipts=receipt.outcome_receipts,
        blockers=receipt.blockers,
    )


def _valid_digest_pair(first: str, second: str) -> bool:
    return _DIGEST.fullmatch(first) is not None and _DIGEST.fullmatch(second) is not None


def _valid_revision_pair(first: str, second: str) -> bool:
    return _COMMIT.fullmatch(first) is not None and _COMMIT.fullmatch(second) is not None


def _validate_run_evidence_input(request: RunEvidenceInput) -> None:
    if not _valid_digest_pair(request.policy_digest, request.controls_digest):
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "run digests")
    if not _valid_revision_pair(request.evaluated_commit, request.evaluated_tree):
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "run revision")
    if request.run.release != request.evaluated_commit:
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, request.run.run_id)
    if request.run.controls != request.controls:
        raise CandidateFailure(CandidateErrorCode.CONTROLS_DRIFT, request.run.run_id)


def _partition_results(
    results: tuple[EvaluatedTaskReceipt | EvaluatedRunBlocker, ...],
) -> _OutcomeReduction:
    receipts: list[EvaluatedTaskReceipt] = []
    blockers: list[EvaluatedRunBlocker] = []
    for result in results:
        if isinstance(result, EvaluatedTaskReceipt):
            receipts.append(result)
        else:
            blockers.append(result)
    return _OutcomeReduction(tuple(receipts), tuple(blockers))


def _require_exact_tasks(summary: ExperimentSummary, task_ids: tuple[str, ...]) -> None:
    actual = tuple(trial.task_id for trial in summary.trials)
    if actual != task_ids or len(set(actual)) != len(actual):
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "task partition")


def _authoritative_result(
    trial: ExperimentTrial,
) -> tuple[VerifierVerdict, float | None] | EvaluatedRunBlocker:
    if trial.exception:
        return _blocker(trial, CandidateBlockerCode.UNVERIFIED.value, "agent_exception")
    reward = _reward_result(trial)
    if reward is not None:
        return reward
    if trial.verdict in (VerifierVerdict.ABSTAIN.value, VerifierVerdict.ERROR.value):
        return VerifierVerdict(trial.verdict), None
    return _blocker(trial, CandidateBlockerCode.UNVERIFIED.value, "missing_verifier_result")


def _reward_result(
    trial: ExperimentTrial,
) -> tuple[VerifierVerdict, float] | EvaluatedRunBlocker | None:
    if trial.reward == 1.0:
        return VerifierVerdict.PASS, 1.0
    if trial.reward == 0.0:
        return VerifierVerdict.FAIL, 0.0
    if trial.reward is not None:
        return _blocker(
            trial,
            CandidateBlockerCode.UNSUPPORTED_REWARD.value,
            "unsupported_reward",
        )
    return None


def _blocker(trial: ExperimentTrial, code: str, subject: str) -> EvaluatedRunBlocker:
    return EvaluatedRunBlocker(
        task_id=trial.task_id,
        code=code,
        subject=subject,
    )


def _trace_blocker(
    trial: ExperimentTrial,
    code: CandidateBlockerCode | None,
) -> EvaluatedRunBlocker:
    if code is None:
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, trial.task_id)
    return _blocker(trial, code.value, "trace_mapping")


def _trace_request(
    task_id: str,
    started_at: datetime,
    finished_at: datetime,
    request: RunEvidenceInput,
) -> TraceMatchRequest:
    return TraceMatchRequest(
        task_id=task_id,
        session_id=request.run.session_id,
        environment=request.controls.environment,
        release=request.run.release,
        started_at=started_at,
        finished_at=finished_at,
    )


def _outcome(
    trial: ExperimentTrial,
    controls: ExperimentControls,
    trace_id: str,
    result: tuple[VerifierVerdict, float | None],
) -> OutcomeEvaluation:
    return OutcomeEvaluation(
        trace_id=TraceId(trace_id),
        task_id=TaskId(trial.task_id),
        verifier_id=VerifierId(f"{controls.verifier}@{trial.task_checksum}"),
        evaluated_at=trial.evaluated_at,
        verdict=result[0],
        score=result[1],
        evidence=tuple(EvidenceReference(value) for value in trial.evidence),
    )
