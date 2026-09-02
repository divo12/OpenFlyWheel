"""Re-entrant candidate worktree sealing, execution, and outcome recording."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from ofw.evaluation.outcome import (
    EvidenceReference,
    OutcomeEvaluation,
    TaskId,
    VerifierId,
    VerifierVerdict,
)
from ofw.evolution.candidate import (
    CandidateBlocker,
    CandidateBlockerCode,
    CandidateErrorCode,
    CandidateExecutionInput,
    CandidateExecutionObservation,
    CandidateExperimentRunner,
    CandidateFailure,
    CandidateHypothesisRepository,
    CandidateId,
    CandidateOutcomeReceipt,
    CandidateOutcomeStore,
    CandidatePhase,
    CandidateStatus,
    CandidateTraceLocator,
    CandidateWorkspace,
    CandidateWorkspaceGateway,
    TraceMatchRequest,
    candidate_policy_digest,
)
from ofw.evolution.hypothesis import HarnessHypothesis, HypothesisFailure, StrictModel
from ofw.observability.langfuse.domain import TraceId
from ofw.preparation.contracts import (
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
    PreparationErrorCode,
    PreparationFailure,
)
from ofw.preparation.policy import ExperimentPolicyFailure, ExperimentPolicySnapshot


class _CandidateState(StrictModel):
    schema_version: Literal[1] = 1
    request_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    phase: CandidatePhase
    source_commit: str = Field(pattern=r"[0-9a-f]{40}")
    policy_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    controls_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    worktree_path: Path
    candidate_id: str | None = Field(default=None, pattern=r"sha256:[0-9a-f]{64}")
    candidate_tree: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    candidate_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    job_path: Path | None = None
    log_path: Path | None = None
    process_id: int | None = Field(default=None, ge=1)
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    outcome_receipts: tuple[CandidateOutcomeReceipt, ...] = Field(max_length=500)
    blockers: tuple[CandidateBlocker, ...] = Field(max_length=500)


@dataclass(frozen=True, slots=True)
class _OutcomeReduction:
    receipts: tuple[CandidateOutcomeReceipt, ...]
    blockers: tuple[CandidateBlocker, ...]


class CandidateExecutionService:
    def __init__(
        self,
        *,
        workspace: CandidateWorkspaceGateway,
        hypotheses: CandidateHypothesisRepository,
        runner: CandidateExperimentRunner,
        trace_locator: CandidateTraceLocator,
        outcome_store: CandidateOutcomeStore,
    ) -> None:
        self._workspace = workspace
        self._hypotheses = hypotheses
        self._runner = runner
        self._trace_locator = trace_locator
        self._outcome_store = outcome_store

    def execute(self, request: CandidateExecutionInput) -> CandidateExecutionObservation:
        try:
            return self._execute(request)
        except CandidateFailure as error:
            return _failure_observation(request, error)
        except PreparationFailure as error:
            return _failure_observation(request, _runner_failure(error))

    def _execute(self, request: CandidateExecutionInput) -> CandidateExecutionObservation:
        policy, hypothesis = self._authority(request)
        control = self._workspace.control_directory(
            request.workspace_root,
            request.hypothesis_id,
        )
        control.mkdir(parents=True, exist_ok=True)
        with _candidate_lock(control):
            state = _read_state(control)
            if state is None:
                return self._prepare(request, policy, hypothesis, control)
            _validate_state(request, policy, state)
            self._workspace.validate_accepted(request.workspace_root, policy, hypothesis)
            if state.phase is CandidatePhase.COMPLETE:
                return _complete_observation(request, state)
            if state.phase is CandidatePhase.RUNNING:
                return self._poll(request, policy, hypothesis, control, state)
            return self._launch(request, policy, hypothesis, control, state)

    def _authority(
        self,
        request: CandidateExecutionInput,
    ) -> tuple[ExperimentPolicySnapshot, HarnessHypothesis]:
        try:
            policy = self._hypotheses.load_policy(request.workspace_root, request.experiment_id)
            hypothesis = self._hypotheses.load(request.workspace_root, request.hypothesis_id)
        except (ExperimentPolicyFailure, HypothesisFailure):
            raise CandidateFailure(CandidateErrorCode.STALE_POLICY, request.experiment_id) from None
        if hypothesis.experiment_id != policy.experiment_id:
            raise CandidateFailure(CandidateErrorCode.STALE_POLICY, request.hypothesis_id)
        if hypothesis.source_commit != policy.initialization_commit:
            raise CandidateFailure(CandidateErrorCode.STALE_COMMIT, request.hypothesis_id)
        return policy, hypothesis

    def _prepare(
        self,
        request: CandidateExecutionInput,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
        control: Path,
    ) -> CandidateExecutionObservation:
        prepared = self._workspace.prepare(
            request.workspace_root,
            request.worktree_parent,
            policy,
            hypothesis,
        )
        state = _CandidateState(
            request_digest=_request_digest(request),
            phase=CandidatePhase.EDITING,
            source_commit=prepared.source_commit,
            policy_digest=candidate_policy_digest(policy),
            controls_digest=policy.controls_digest,
            worktree_path=prepared.worktree_path,
            outcome_receipts=(),
            blockers=(),
        )
        _write_state(control, state)
        return _editing_observation(request, state)

    def _launch(
        self,
        request: CandidateExecutionInput,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
        control: Path,
        state: _CandidateState,
    ) -> CandidateExecutionObservation:
        workspace = _git_workspace(request, state)
        tree = self._workspace.inspect(workspace, policy, hypothesis)
        controls = self._validated_controls(request, policy)
        candidate_id = CandidateId.build(
            policy_digest=state.policy_digest,
            hypothesis_id=request.hypothesis_id,
            source_commit=state.source_commit,
            candidate_tree=tree.tree_id,
            controls_digest=state.controls_digest,
        )
        committed = self._workspace.commit(
            workspace,
            tree,
            candidate_id,
            request.experiment_id,
        )
        run = _new_run(request, control, workspace, controls, candidate_id, committed.commit)
        started_at = datetime.now(UTC)
        running = _running_state(
            state,
            candidate_id,
            tree.tree_id,
            committed.commit,
            run,
            started_at,
            None,
            policy.max_baseline_seconds,
        )
        _write_state(control, running)
        process_id = self._runner.start(run)
        launched = _running_state(
            state,
            candidate_id,
            tree.tree_id,
            committed.commit,
            run,
            started_at,
            process_id,
            policy.max_baseline_seconds,
        )
        _write_state(control, launched)
        return _running_observation(request, launched)

    def _poll(
        self,
        request: CandidateExecutionInput,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
        control: Path,
        state: _CandidateState,
    ) -> CandidateExecutionObservation:
        del hypothesis
        controls = self._validated_controls(request, policy)
        run = _run_from_state(request, state, controls)
        summary = self._runner.summarize(run)
        if summary is None:
            _require_before_deadline(state)
            return _running_observation(request, state)
        reduction = _record_outcomes(
            summary,
            run,
            controls,
            self._trace_locator,
            self._outcome_store,
        )
        complete = _complete_state(state, reduction)
        _write_state(control, complete)
        return _complete_observation(request, complete)

    def _validated_controls(
        self,
        request: CandidateExecutionInput,
        policy: ExperimentPolicySnapshot,
    ) -> ExperimentControls:
        actual = self._runner.validate(
            request.benchmark_root,
            request.harbor_executable,
            request.harbor_config,
        )
        if actual != _policy_controls(policy):
            raise CandidateFailure(CandidateErrorCode.CONTROLS_DRIFT, policy.experiment_id)
        return actual


def _record_outcomes(
    summary: ExperimentSummary,
    run: ExperimentRun,
    controls: ExperimentControls,
    trace_locator: CandidateTraceLocator,
    outcome_store: CandidateOutcomeStore,
) -> _OutcomeReduction:
    receipts: list[CandidateOutcomeReceipt] = []
    blockers: list[CandidateBlocker] = []
    for trial in summary.trials:
        result = _authoritative_result(trial)
        if isinstance(result, CandidateBlocker):
            blockers.append(result)
            continue
        match = trace_locator.locate(_trace_request(trial, run, controls))
        if match.trace_id is None:
            blockers.append(_trace_blocker(trial, match.blocker))
            continue
        outcome = _outcome(trial, controls, match.trace_id, result)
        try:
            submission = outcome_store.store(outcome)
        except Exception:
            raise CandidateFailure(
                CandidateErrorCode.OUTCOME_STORE_FAILED,
                trial.task_id,
            ) from None
        receipts.append(
            CandidateOutcomeReceipt(
                task_id=trial.task_id,
                trace_id=match.trace_id,
                score_id=submission.score_id.value,
                verdict=result[0],
            )
        )
    return _OutcomeReduction(tuple(receipts), tuple(blockers))


def _authoritative_result(
    trial: ExperimentTrial,
) -> tuple[VerifierVerdict, float | None] | CandidateBlocker:
    if trial.exception:
        return _blocker(trial, CandidateBlockerCode.UNVERIFIED, "agent_exception")
    reward = _reward_result(trial)
    if reward is not None:
        return reward
    return _verdict_result(trial)


def _reward_result(
    trial: ExperimentTrial,
) -> tuple[VerifierVerdict, float] | CandidateBlocker | None:
    if trial.reward == 1.0:
        return VerifierVerdict.PASS, 1.0
    if trial.reward == 0.0:
        return VerifierVerdict.FAIL, 0.0
    if trial.reward is not None:
        return _blocker(trial, CandidateBlockerCode.UNSUPPORTED_REWARD, str(trial.reward))
    return None


def _verdict_result(
    trial: ExperimentTrial,
) -> tuple[VerifierVerdict, None] | CandidateBlocker:
    if trial.verdict in (VerifierVerdict.ABSTAIN.value, VerifierVerdict.ERROR.value):
        return VerifierVerdict(trial.verdict), None
    return _blocker(trial, CandidateBlockerCode.UNVERIFIED, "missing_verifier_result")


def _trace_request(
    trial: ExperimentTrial,
    run: ExperimentRun,
    controls: ExperimentControls,
) -> TraceMatchRequest:
    return TraceMatchRequest(
        task_id=trial.task_id,
        session_id=run.session_id,
        environment=controls.environment,
        release=run.release,
        started_at=trial.started_at,
        finished_at=trial.finished_at,
    )


def _trace_blocker(
    trial: ExperimentTrial,
    code: CandidateBlockerCode | None,
) -> CandidateBlocker:
    if code is None:
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, trial.task_id)
    return _blocker(trial, code, "trace_mapping")


def _blocker(
    trial: ExperimentTrial,
    code: CandidateBlockerCode,
    subject: str,
) -> CandidateBlocker:
    return CandidateBlocker(task_id=trial.task_id, code=code, subject=subject)


def _outcome(
    trial: ExperimentTrial,
    controls: ExperimentControls,
    trace_id: str,
    result: tuple[VerifierVerdict, float | None],
) -> OutcomeEvaluation:
    verdict, score = result
    return OutcomeEvaluation(
        trace_id=TraceId(trace_id),
        task_id=TaskId(trial.task_id),
        verifier_id=VerifierId(f"{controls.verifier}@{trial.task_checksum}"),
        evaluated_at=trial.evaluated_at,
        verdict=verdict,
        score=score,
        evidence=tuple(EvidenceReference(value) for value in trial.evidence),
    )


def _policy_controls(policy: ExperimentPolicySnapshot) -> ExperimentControls:
    return ExperimentControls(
        model=policy.model,
        task_ids=policy.task_ids,
        benchmark_config_digest=policy.benchmark_config_digest,
        verifier=policy.verifier,
        environment=policy.environment,
        concurrency=policy.concurrency,
        max_retries=policy.max_retries,
    )


def _new_run(
    request: CandidateExecutionInput,
    control: Path,
    workspace: CandidateWorkspace,
    controls: ExperimentControls,
    candidate_id: CandidateId,
    candidate_commit: str,
) -> ExperimentRun:
    run_id = f"ofw-candidate-{candidate_id.value.removeprefix('sha256:')[:24]}"
    return ExperimentRun(
        run_id=run_id,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=request.benchmark_root / request.harbor_config,
        job_path=request.benchmark_root / "jobs" / run_id,
        log_path=control / "candidate.log",
        source_root=workspace.worktree_path,
        release=candidate_commit,
        session_id=candidate_id.value,
        controls=controls,
    )


def _run_from_state(
    request: CandidateExecutionInput,
    state: _CandidateState,
    controls: ExperimentControls,
) -> ExperimentRun:
    candidate_id = state.candidate_id
    candidate_commit = state.candidate_commit
    job_path = state.job_path
    log_path = state.log_path
    if candidate_id is None or candidate_commit is None or job_path is None or log_path is None:
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, request.hypothesis_id)
    return ExperimentRun(
        run_id=job_path.name,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=request.benchmark_root / request.harbor_config,
        job_path=job_path,
        log_path=log_path,
        source_root=state.worktree_path,
        release=candidate_commit,
        session_id=candidate_id,
        controls=controls,
    )


def _git_workspace(
    request: CandidateExecutionInput,
    state: _CandidateState,
) -> CandidateWorkspace:
    return CandidateWorkspace(
        accepted_root=request.workspace_root.resolve(),
        worktree_path=state.worktree_path,
        source_commit=state.source_commit,
    )


def _running_state(
    previous: _CandidateState,
    candidate_id: CandidateId,
    tree: str,
    commit: str,
    run: ExperimentRun,
    started_at: datetime,
    process_id: int | None,
    timeout_seconds: int,
) -> _CandidateState:
    return _CandidateState(
        request_digest=previous.request_digest,
        phase=CandidatePhase.RUNNING,
        source_commit=previous.source_commit,
        policy_digest=previous.policy_digest,
        controls_digest=previous.controls_digest,
        worktree_path=previous.worktree_path,
        candidate_id=candidate_id.value,
        candidate_tree=tree,
        candidate_commit=commit,
        job_path=run.job_path,
        log_path=run.log_path,
        process_id=process_id,
        started_at=started_at,
        deadline_at=started_at + timedelta(seconds=timeout_seconds),
        outcome_receipts=(),
        blockers=(),
    )


def _complete_state(state: _CandidateState, reduction: _OutcomeReduction) -> _CandidateState:
    return _CandidateState(
        request_digest=state.request_digest,
        phase=CandidatePhase.COMPLETE,
        source_commit=state.source_commit,
        policy_digest=state.policy_digest,
        controls_digest=state.controls_digest,
        worktree_path=state.worktree_path,
        candidate_id=state.candidate_id,
        candidate_tree=state.candidate_tree,
        candidate_commit=state.candidate_commit,
        job_path=state.job_path,
        log_path=state.log_path,
        process_id=state.process_id,
        started_at=state.started_at,
        deadline_at=state.deadline_at,
        outcome_receipts=reduction.receipts,
        blockers=reduction.blockers,
    )


def _validate_state(
    request: CandidateExecutionInput,
    policy: ExperimentPolicySnapshot,
    state: _CandidateState,
) -> None:
    if state.request_digest != _request_digest(request):
        raise CandidateFailure(CandidateErrorCode.REQUEST_CONFLICT, request.hypothesis_id)
    if state.policy_digest != candidate_policy_digest(policy):
        raise CandidateFailure(CandidateErrorCode.STALE_POLICY, request.experiment_id)
    if state.controls_digest != policy.controls_digest:
        raise CandidateFailure(CandidateErrorCode.CONTROLS_DRIFT, request.experiment_id)


def _require_before_deadline(state: _CandidateState) -> None:
    if state.deadline_at is not None and datetime.now(UTC) > state.deadline_at:
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "candidate_timeout")


def _editing_observation(
    request: CandidateExecutionInput,
    state: _CandidateState,
) -> CandidateExecutionObservation:
    return CandidateExecutionObservation(
        status=CandidateStatus.WARNING,
        summary="The isolated candidate worktree is ready for the hypothesis edit.",
        next_actions=(
            "Edit only the exact hypothesis targets, then call execute_candidate again.",
        ),
        artifacts=(str(state.worktree_path),),
        phase=CandidatePhase.EDITING,
        experiment_id=request.experiment_id,
        hypothesis_id=request.hypothesis_id,
        source_commit=state.source_commit,
        worktree_path=state.worktree_path,
        outcome_receipts=(),
        blockers=(),
    )


def _running_observation(
    request: CandidateExecutionInput,
    state: _CandidateState,
) -> CandidateExecutionObservation:
    return CandidateExecutionObservation(
        status=CandidateStatus.WARNING,
        summary="The deterministic candidate Harbor run is still running.",
        next_actions=("Poll execute_candidate with the identical request.",),
        artifacts=_artifacts(state),
        phase=CandidatePhase.RUNNING,
        experiment_id=request.experiment_id,
        hypothesis_id=request.hypothesis_id,
        source_commit=state.source_commit,
        candidate_id=state.candidate_id,
        candidate_tree=state.candidate_tree,
        candidate_commit=state.candidate_commit,
        worktree_path=state.worktree_path,
        job_path=state.job_path,
        session_id=state.candidate_id,
        outcome_receipts=(),
        blockers=(),
        next_poll_after_seconds=30,
    )


def _complete_observation(
    request: CandidateExecutionInput,
    state: _CandidateState,
) -> CandidateExecutionObservation:
    passes = sum(item.verdict is VerifierVerdict.PASS for item in state.outcome_receipts)
    failures = sum(item.verdict is VerifierVerdict.FAIL for item in state.outcome_receipts)
    terminal = len(state.outcome_receipts) + len(state.blockers)
    return CandidateExecutionObservation(
        status=CandidateStatus.SUCCESS,
        summary="The candidate run is complete with authoritative outcome receipts.",
        next_actions=("Retain the candidate and outcome receipts for the admission gate.",),
        artifacts=_artifacts(state),
        phase=CandidatePhase.COMPLETE,
        experiment_id=request.experiment_id,
        hypothesis_id=request.hypothesis_id,
        source_commit=state.source_commit,
        candidate_id=state.candidate_id,
        candidate_tree=state.candidate_tree,
        candidate_commit=state.candidate_commit,
        worktree_path=state.worktree_path,
        job_path=state.job_path,
        session_id=state.candidate_id,
        terminal_trials=terminal,
        verifier_passes=passes,
        verifier_failures=failures,
        unverified_trials=len(state.blockers),
        outcome_receipts=state.outcome_receipts,
        blockers=state.blockers,
    )


def _failure_observation(
    request: CandidateExecutionInput,
    error: CandidateFailure,
) -> CandidateExecutionObservation:
    return CandidateExecutionObservation(
        status=CandidateStatus.ERROR,
        summary=f"Candidate execution stopped: {error.code.value}.",
        next_actions=("Correct the typed boundary failure without forcing Git state.",),
        artifacts=(),
        phase=CandidatePhase.FAILED,
        experiment_id=request.experiment_id,
        hypothesis_id=request.hypothesis_id,
        outcome_receipts=(),
        blockers=(),
        error_code=error.code,
    )


def _artifacts(state: _CandidateState) -> tuple[str, ...]:
    values = (
        state.candidate_id,
        state.candidate_commit,
        None if state.job_path is None else str(state.job_path),
    )
    return tuple(value for value in values if value is not None)


def _request_digest(request: CandidateExecutionInput) -> str:
    digest = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@contextmanager
def _candidate_lock(control: Path) -> Iterator[None]:
    lock = control / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise CandidateFailure(CandidateErrorCode.REQUEST_CONFLICT, control.name) from None
    try:
        yield
    finally:
        lock.rmdir()


def _read_state(control: Path) -> _CandidateState | None:
    path = control / "state.json"
    if not path.exists():
        return None
    try:
        return _CandidateState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "state.json") from None


def _write_state(control: Path, state: _CandidateState) -> None:
    path = control / "state.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=control,
        delete=False,
    ) as temporary:
        temporary.write(state.model_dump_json(indent=2) + "\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _runner_failure(error: PreparationFailure) -> CandidateFailure:
    if error.code is PreparationErrorCode.MISSING_ENVIRONMENT:
        code = CandidateErrorCode.MISSING_ENVIRONMENT
    elif error.code is PreparationErrorCode.LAUNCH_FAILED:
        code = CandidateErrorCode.LAUNCH_FAILED
    else:
        code = CandidateErrorCode.INVALID_RESULT
    return CandidateFailure(code, error.subject)
