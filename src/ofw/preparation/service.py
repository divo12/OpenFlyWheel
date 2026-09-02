"""Typed, re-entrant preparation of an isolated harness experiment workspace."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import Field

from ofw.preparation.contracts import (
    BaselineConfiguration,
    BaselineRun,
    BaselineRunner,
    BaselineSummary,
    PreparationErrorCode,
    PreparationFailure,
    PreparationPhase,
    PreparationStatus,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
    StrictModel,
    WorkspaceGateway,
    WorkspacePreparationObservation,
)
from ofw.preparation.policy import (
    ExperimentPolicyErrorCode,
    ExperimentPolicyFailure,
    FileExperimentPolicyRepository,
    build_experiment_policy,
)

_COMMIT_PATTERN = r"[0-9a-f]{40}"


class _PreparationStateWire(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    request_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    phase: PreparationPhase
    branch_name: str
    worktree_path: Path
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    initialization_commit: str = Field(pattern=_COMMIT_PATTERN)
    program_path: Path
    job_path: Path
    log_path: Path
    model: str
    process_id: int | None = Field(default=None, strict=True, ge=1)
    started_at: datetime
    deadline_at: datetime
    terminal_trials: int | None = Field(default=None, ge=0, le=500)
    verifier_passes: int | None = Field(default=None, ge=0, le=500)
    verifier_failures: int | None = Field(default=None, ge=0, le=500)
    unverified_trials: int | None = Field(default=None, ge=0, le=500)
    unsupported_reward_trials: int | None = Field(default=None, ge=0, le=500)
    error_code: PreparationErrorCode | None = None


@dataclass(frozen=True, slots=True)
class _TerminalCounts:
    terminal_trials: int | None
    verifier_passes: int | None
    verifier_failures: int | None
    unverified_trials: int | None
    unsupported_reward_trials: int | None


class WorkspacePreparationService:
    """Prepare one isolated experiment branch and poll its baseline run."""

    def __init__(
        self,
        runner: BaselineRunner,
        workspace: WorkspaceGateway,
        *,
        base_program: str,
        itsm_program: str,
    ) -> None:
        self._runner = runner
        self._workspace = workspace
        self._program = _compose_program(base_program, itsm_program)

    def prepare(self, request: PrepareWorkspaceInput) -> WorkspacePreparationObservation:
        try:
            return self._prepare(request)
        except PreparationFailure as error:
            return _failure_observation(request, error)

    def _prepare(self, request: PrepareWorkspaceInput) -> WorkspacePreparationObservation:
        harness_root = _directory(request.harness_root, "harness_root")
        _directory(request.worktree_parent, "worktree_parent")
        _directory(request.benchmark_root, "benchmark_root")
        state_directory = self._workspace.control_directory(
            harness_root,
            request.experiment_id,
        )
        state_directory.mkdir(parents=True, exist_ok=True)
        with _preparation_lock(state_directory):
            state = _read_state(state_directory)
            if state is None:
                return self._start(request, harness_root, state_directory)
            return self._poll(request, state_directory, state)

    def _start(
        self,
        request: PrepareWorkspaceInput,
        harness_root: Path,
        state_directory: Path,
    ) -> WorkspacePreparationObservation:
        configuration = self._runner.validate(request)
        digest = _request_digest(request)
        prepared = self._workspace.prepare(
            request,
            self._program,
            configuration,
        )
        _publish_policy(state_directory, request, prepared, configuration)
        job_path = request.benchmark_root / "jobs" / request.experiment_id
        log_path = state_directory / "baseline.log"
        run = _baseline_run(request, prepared, job_path, log_path)
        started_at = datetime.now(UTC)
        state = _PreparationStateWire(
            request_digest=digest,
            phase=PreparationPhase.RUNNING,
            branch_name=prepared.branch_name,
            worktree_path=prepared.worktree_path,
            base_commit=prepared.base_commit,
            initialization_commit=prepared.initialization_commit,
            program_path=prepared.program_path,
            job_path=job_path,
            log_path=log_path,
            model=configuration.model,
            process_id=None,
            started_at=started_at,
            deadline_at=started_at + timedelta(seconds=request.max_baseline_seconds),
        )
        _write_state(state_directory, state)
        try:
            process_id = self._runner.start(run)
        except PreparationFailure as error:
            failed = _terminal_state(
                state,
                phase=PreparationPhase.FAILED,
                error_code=error.code,
            )
            _write_state(state_directory, failed)
            return _persisted_failure_observation(request, failed)
        running = _state_with_process(state, process_id)
        _write_state(state_directory, running)
        return _running_observation(request, running)

    def _poll(
        self,
        request: PrepareWorkspaceInput,
        state_directory: Path,
        state: _PreparationStateWire,
    ) -> WorkspacePreparationObservation:
        if state.request_digest != _request_digest(request):
            raise PreparationFailure(
                PreparationErrorCode.REQUEST_CONFLICT,
                request.experiment_id,
            )
        terminal = _terminal_observation(request, state)
        if terminal is not None:
            return terminal
        run = _run_from_state(request, state)
        summary = self._runner.summarize(run)
        if summary is not None:
            ready = _ready_state(state, summary, request.expected_task_count)
            _write_state(state_directory, ready)
            return _ready_observation(request, ready)
        if datetime.now(UTC) <= state.deadline_at:
            return _running_observation(request, state)
        failed = _terminal_state(
            state,
            phase=PreparationPhase.FAILED,
            error_code=PreparationErrorCode.BASELINE_TIMEOUT,
        )
        _write_state(state_directory, failed)
        return _persisted_failure_observation(request, failed)


def _directory(path: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, field) from error
    if not resolved.is_dir():
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, field)
    return resolved


def _publish_policy(
    state_directory: Path,
    request: PrepareWorkspaceInput,
    prepared: PreparedGitWorkspace,
    configuration: BaselineConfiguration,
) -> None:
    try:
        FileExperimentPolicyRepository().publish(
            state_directory,
            build_experiment_policy(request, prepared, configuration),
        )
    except ExperimentPolicyFailure as error:
        code = (
            PreparationErrorCode.POLICY_CONFLICT
            if error.code is ExperimentPolicyErrorCode.POLICY_CONFLICT
            else PreparationErrorCode.POLICY_WRITE_FAILED
        )
        raise PreparationFailure(code, request.experiment_id) from None


def _compose_program(base: str, itsm: str) -> str:
    if not base.strip() or not itsm.strip():
        raise ValueError("program templates must not be empty")
    return f"{base.rstrip()}\n\n{itsm.lstrip()}".rstrip() + "\n"


def _request_digest(request: PrepareWorkspaceInput) -> str:
    return f"sha256:{hashlib.sha256(request.model_dump_json().encode()).hexdigest()}"


@contextmanager
def _preparation_lock(state_directory: Path) -> Iterator[None]:
    lock_path = state_directory / ".lock"
    try:
        lock_path.mkdir()
    except FileExistsError as error:
        raise PreparationFailure(
            PreparationErrorCode.PREPARATION_BUSY,
            state_directory.name,
        ) from error
    try:
        yield
    finally:
        lock_path.rmdir()


def _read_state(state_directory: Path) -> _PreparationStateWire | None:
    path = state_directory / "state.json"
    if not path.exists():
        return None
    try:
        return _PreparationStateWire.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "state.json",
        ) from error


def _write_state(state_directory: Path, state: _PreparationStateWire) -> None:
    path = state_directory / "state.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_directory,
        delete=False,
    ) as temporary:
        temporary.write(state.model_dump_json(indent=2) + "\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _baseline_run(
    request: PrepareWorkspaceInput,
    prepared: PreparedGitWorkspace,
    job_path: Path,
    log_path: Path,
) -> BaselineRun:
    return BaselineRun(
        experiment_id=request.experiment_id,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=request.benchmark_root / request.harbor_config,
        job_path=job_path,
        log_path=log_path,
        worktree_path=prepared.worktree_path,
        initialization_commit=prepared.initialization_commit,
    )


def _run_from_state(
    request: PrepareWorkspaceInput,
    state: _PreparationStateWire,
) -> BaselineRun:
    return BaselineRun(
        experiment_id=request.experiment_id,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=request.benchmark_root / request.harbor_config,
        job_path=state.job_path,
        log_path=state.log_path,
        worktree_path=state.worktree_path,
        initialization_commit=state.initialization_commit,
    )


def _ready_state(
    state: _PreparationStateWire,
    summary: BaselineSummary,
    expected: int,
) -> _PreparationStateWire:
    if summary.terminal_trials != expected:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "terminal trial count",
        )
    return _terminal_state(
        state,
        phase=PreparationPhase.READY,
        summary=summary,
    )


def _terminal_state(
    state: _PreparationStateWire,
    *,
    phase: PreparationPhase,
    summary: BaselineSummary | None = None,
    error_code: PreparationErrorCode | None = None,
) -> _PreparationStateWire:
    counts = _terminal_counts(summary)
    return _PreparationStateWire(
        request_digest=state.request_digest,
        phase=phase,
        branch_name=state.branch_name,
        worktree_path=state.worktree_path,
        base_commit=state.base_commit,
        initialization_commit=state.initialization_commit,
        program_path=state.program_path,
        job_path=state.job_path,
        log_path=state.log_path,
        model=state.model,
        process_id=state.process_id,
        started_at=state.started_at,
        deadline_at=state.deadline_at,
        terminal_trials=counts.terminal_trials,
        verifier_passes=counts.verifier_passes,
        verifier_failures=counts.verifier_failures,
        unverified_trials=counts.unverified_trials,
        unsupported_reward_trials=counts.unsupported_reward_trials,
        error_code=error_code,
    )


def _terminal_counts(summary: BaselineSummary | None) -> _TerminalCounts:
    if summary is None:
        return _TerminalCounts(None, None, None, None, None)
    return _TerminalCounts(
        summary.terminal_trials,
        summary.verifier_passes,
        summary.verifier_failures,
        summary.unverified_trials,
        summary.unsupported_reward_trials,
    )


def _state_with_process(
    state: _PreparationStateWire,
    process_id: int,
) -> _PreparationStateWire:
    return _PreparationStateWire(
        request_digest=state.request_digest,
        phase=state.phase,
        branch_name=state.branch_name,
        worktree_path=state.worktree_path,
        base_commit=state.base_commit,
        initialization_commit=state.initialization_commit,
        program_path=state.program_path,
        job_path=state.job_path,
        log_path=state.log_path,
        model=state.model,
        process_id=process_id,
        started_at=state.started_at,
        deadline_at=state.deadline_at,
    )


def _running_observation(
    request: PrepareWorkspaceInput,
    state: _PreparationStateWire,
) -> WorkspacePreparationObservation:
    return WorkspacePreparationObservation(
        status=PreparationStatus.WARNING,
        summary="The isolated ITSM baseline is still running.",
        next_actions=("Poll prepare_workspace with the identical request.",),
        artifacts=(str(state.worktree_path), str(state.job_path), str(state.log_path)),
        preparation_id=request.experiment_id,
        phase=PreparationPhase.RUNNING,
        branch_name=state.branch_name,
        worktree_path=state.worktree_path,
        base_commit=state.base_commit,
        initialization_commit=state.initialization_commit,
        program_path=state.program_path,
        job_path=state.job_path,
        session_id=request.experiment_id,
        next_poll_after_seconds=30,
    )


def _ready_observation(
    request: PrepareWorkspaceInput,
    state: _PreparationStateWire,
) -> WorkspacePreparationObservation:
    return WorkspacePreparationObservation(
        status=PreparationStatus.SUCCESS,
        summary="The isolated ITSM workspace and baseline are ready.",
        next_actions=("Start a fresh Codex session in the worktree and read PROGRAM.md.",),
        artifacts=(str(state.worktree_path), str(state.program_path), str(state.job_path)),
        preparation_id=request.experiment_id,
        phase=PreparationPhase.READY,
        branch_name=state.branch_name,
        worktree_path=state.worktree_path,
        base_commit=state.base_commit,
        initialization_commit=state.initialization_commit,
        program_path=state.program_path,
        job_path=state.job_path,
        session_id=request.experiment_id,
        terminal_trials=state.terminal_trials,
        verifier_passes=state.verifier_passes,
        verifier_failures=state.verifier_failures,
        unverified_trials=state.unverified_trials,
        unsupported_reward_trials=state.unsupported_reward_trials,
    )


def _persisted_failure_observation(
    request: PrepareWorkspaceInput,
    state: _PreparationStateWire,
) -> WorkspacePreparationObservation:
    code = state.error_code or PreparationErrorCode.INVALID_BASELINE_RESULT
    return WorkspacePreparationObservation(
        status=PreparationStatus.ERROR,
        summary=f"Workspace preparation stopped: {code.value}.",
        next_actions=("Follow the typed retry instruction or stop condition.",),
        artifacts=(str(state.worktree_path), str(state.job_path), str(state.log_path)),
        preparation_id=request.experiment_id,
        phase=PreparationPhase.FAILED,
        branch_name=state.branch_name,
        worktree_path=state.worktree_path,
        base_commit=state.base_commit,
        initialization_commit=state.initialization_commit,
        program_path=state.program_path,
        job_path=state.job_path,
        session_id=request.experiment_id,
        error_code=code,
        root_cause_hint=_root_cause(code),
        retry=_retry(code),
        stop_when=_stop_when(code),
    )


def _terminal_observation(
    request: PrepareWorkspaceInput,
    state: _PreparationStateWire,
) -> WorkspacePreparationObservation | None:
    if state.phase is PreparationPhase.READY:
        return _ready_observation(request, state)
    if state.phase is PreparationPhase.FAILED:
        return _persisted_failure_observation(request, state)
    return None


def _failure_observation(
    request: PrepareWorkspaceInput,
    error: PreparationFailure,
) -> WorkspacePreparationObservation:
    return WorkspacePreparationObservation(
        status=PreparationStatus.ERROR,
        summary=f"Workspace preparation stopped: {error.code.value}.",
        next_actions=("Follow the typed retry instruction or stop condition.",),
        artifacts=(),
        preparation_id=request.experiment_id,
        phase=PreparationPhase.FAILED,
        error_code=error.code,
        root_cause_hint=_root_cause(error.code),
        retry=_retry(error.code),
        stop_when=_stop_when(error.code),
    )


def _root_cause(code: PreparationErrorCode) -> str:
    if code is PreparationErrorCode.REQUEST_CONFLICT:
        return "The preparation ID is already bound to different canonical inputs."
    if code is PreparationErrorCode.TASK_COUNT_MISMATCH:
        return "The Harbor manifest task count differs from the confirmed experiment."
    if code is PreparationErrorCode.MISSING_ENVIRONMENT:
        return "One or more required credential environment variables are absent."
    if code is PreparationErrorCode.BASELINE_TIMEOUT:
        return "The Harbor job did not publish a terminal result before its deadline."
    return "A validated workspace, Git, Harbor, or result boundary rejected the request."


def _retry(code: PreparationErrorCode) -> str:
    if code is PreparationErrorCode.PREPARATION_BUSY:
        return "Poll the identical request after 30 seconds."
    if code is PreparationErrorCode.BASELINE_TIMEOUT:
        return "Inspect the bounded baseline log, then use a new experiment ID if rerunning."
    return "Correct the reported configuration boundary, then retry without forcing Git state."


def _stop_when(code: PreparationErrorCode) -> str:
    if code in (
        PreparationErrorCode.BRANCH_EXISTS,
        PreparationErrorCode.WORKTREE_EXISTS,
        PreparationErrorCode.REQUEST_CONFLICT,
    ):
        return "Stop until the existing experiment ownership is resolved explicitly."
    return "Stop if correction requires deleting, resetting, or overwriting user-owned data."
