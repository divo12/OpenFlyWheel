"""Bounded Harbor gateway for the ITSM baseline preparation workflow."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ofw.preparation.contracts import (
    BaselineConfiguration,
    BaselineRun,
    BaselineSummary,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
    PreparationErrorCode,
    PreparationFailure,
    PrepareWorkspaceInput,
)

_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_RESULT_BYTES = 8 * 1024 * 1024
_MAX_ADAPTER_BYTES = 512 * 1024
_AGENT_NAME = "agents.ofw_hermes:OfwHermes"
_SOURCE_ENVIRONMENT_NAME = "OFW_HERMES_SOURCE"


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class _HarborAgentWire(_WireModel):
    name: str = Field(min_length=1, max_length=256)
    model_name: str = Field(min_length=1, max_length=256)


class _HarborTaskWire(_WireModel):
    path: str = Field(min_length=1, max_length=256)


class _HarborConfigWire(_WireModel):
    agents: tuple[_HarborAgentWire, ...] = Field(min_length=1, max_length=1)
    tasks: tuple[_HarborTaskWire, ...] = Field(min_length=1, max_length=500)


class _HarborJobResultWire(_WireModel):
    finished_at: str | None
    n_total_trials: int = Field(ge=1, le=500)


class _HarborRewardsWire(_WireModel):
    reward: float | None = None


class _HarborVerifierResultWire(_WireModel):
    rewards: _HarborRewardsWire | None = None
    verdict: str | None = None


class _HarborExecutionWire(_WireModel):
    started_at: datetime
    finished_at: datetime


class _HarborVerifierWire(_WireModel):
    finished_at: datetime


class _HarborTrialResultWire(_WireModel):
    task_id: str | None = None
    task_checksum: str | None = None
    exception_info: JsonValue | None = None
    agent_execution: _HarborExecutionWire | None = None
    verifier: _HarborVerifierWire | None = None
    verifier_result: _HarborVerifierResultWire | None = None


@dataclass(frozen=True, slots=True)
class _Credentials:
    openai_api_key: str
    openai_base_url: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str


class HarborExperimentRunner:
    """Validate, launch, and normalize one deterministic Harbor experiment."""

    def validate(
        self,
        benchmark_root: Path,
        harbor_executable: Path,
        harbor_config: Path,
        *,
        require_credentials: bool = True,
    ) -> ExperimentControls:
        _executable(harbor_executable)
        config_path = _contained(benchmark_root, harbor_config)
        config, config_content = _parse_config(config_path)
        agent = config.agents[0]
        if agent.name != _AGENT_NAME:
            raise PreparationFailure(
                PreparationErrorCode.INVALID_HARBOR_CONFIG,
                "agent",
            )
        task_ids = tuple(task.path for task in config.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise PreparationFailure(
                PreparationErrorCode.INVALID_HARBOR_CONFIG,
                "tasks",
            )
        _validate_source_adapter(benchmark_root)
        if require_credentials:
            _credentials()
        return ExperimentControls(
            model=agent.model_name,
            task_ids=task_ids,
            benchmark_config_digest=(
                f"sha256:{hashlib.sha256(config_content.encode('utf-8')).hexdigest()}"
            ),
            verifier="itsm-bench",
            environment="itsm-bench",
            concurrency=1,
            max_retries=0,
        )

    def start(self, run: ExperimentRun) -> int:
        if run.job_path.exists():
            raise PreparationFailure(PreparationErrorCode.LAUNCH_FAILED, "job_path")
        _require_run_controls(self, run)
        command = (
            str(_executable(run.harbor_executable)),
            "run",
            "--config",
            str(run.harbor_config),
            "--job-name",
            run.run_id,
            "--jobs-dir",
            str(run.job_path.parent),
            "--n-concurrent",
            str(run.controls.concurrency),
            "--max-retries",
            str(run.controls.max_retries),
            "--yes",
        )
        run.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with run.log_path.open("ab") as log_stream:
                process = subprocess.Popen(  # nosec B603
                    command,
                    cwd=run.benchmark_root,
                    env=_process_environment(run),
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as error:
            raise PreparationFailure(
                PreparationErrorCode.LAUNCH_FAILED,
                "harbor",
            ) from error
        return process.pid

    def summarize(self, run: ExperimentRun) -> ExperimentSummary | None:
        root = _finished_job_result(run.job_path)
        if root is None:
            return None
        trials = _experiment_trials(run)
        _validate_experiment_trials(root.n_total_trials, trials, run.controls.task_ids)
        return ExperimentSummary(trials)

    def cancel(self, run: ExperimentRun, process_id: int | None) -> None:
        del run
        if process_id is None:
            return
        try:
            os.killpg(process_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as error:
            raise PreparationFailure(PreparationErrorCode.LAUNCH_FAILED, "cancel") from error


def _validate_experiment_trials(
    expected_count: int,
    trials: tuple[ExperimentTrial, ...],
    expected_task_ids: tuple[str, ...],
) -> None:
    if len(trials) > expected_count:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "trial count",
        )
    if len(trials) != expected_count:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "terminal trial count",
        )
    if tuple(trial.task_id for trial in trials) != expected_task_ids:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "task ids",
        )


class HarborBaselineRunner:
    """Compatibility adapter preserving baseline preparation behavior."""

    def __init__(self) -> None:
        self._runner = HarborExperimentRunner()

    def validate(self, request: PrepareWorkspaceInput) -> BaselineConfiguration:
        controls = self._runner.validate(
            request.benchmark_root,
            request.harbor_executable,
            request.harbor_config,
            require_credentials=False,
        )
        if len(controls.task_ids) != request.expected_task_count:
            raise PreparationFailure(
                PreparationErrorCode.TASK_COUNT_MISMATCH,
                str(len(controls.task_ids)),
            )
        _credentials()
        return BaselineConfiguration(
            model=controls.model,
            task_ids=controls.task_ids,
            benchmark_config_digest=controls.benchmark_config_digest,
            verifier=controls.verifier,
            environment=controls.environment,
        )

    def start(self, run: BaselineRun) -> int:
        return self._runner.start(_baseline_experiment_run(run, run.controls))

    def summarize(self, run: BaselineRun) -> BaselineSummary | None:
        root = _finished_job_result(run.job_path)
        if root is None:
            return None
        trials = _trial_results(run.job_path)
        if len(trials) > root.n_total_trials:
            raise PreparationFailure(
                PreparationErrorCode.INVALID_BASELINE_RESULT,
                "trial count",
            )
        return _baseline_summary(root.n_total_trials, trials)


def _baseline_summary(
    total_trials: int,
    trials: tuple[_HarborTrialResultWire, ...],
) -> BaselineSummary:
    passes = sum(_is_pass(trial) for trial in trials)
    failures = sum(_is_failure(trial) for trial in trials)
    unsupported = sum(_has_unsupported_reward(trial) for trial in trials)
    return BaselineSummary(
        terminal_trials=total_trials,
        verifier_passes=passes,
        verifier_failures=failures,
        unverified_trials=total_trials - passes - failures,
        unsupported_reward_trials=unsupported,
    )


def _finished_job_result(job_path: Path) -> _HarborJobResultWire | None:
    path = job_path / "result.json"
    if not path.exists():
        return None
    result = _parse_job_result(path)
    if result.finished_at is None:
        return None
    return result


def _executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, "harbor_executable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, "harbor_executable")
    return resolved


def _contained(root: Path, relative: Path) -> Path:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, "harbor_config") from error
    if not resolved.is_file():
        raise PreparationFailure(PreparationErrorCode.INVALID_PATH, "harbor_config")
    return resolved


def _parse_config(path: Path) -> tuple[_HarborConfigWire, str]:
    try:
        content = _bounded_text(path, _MAX_CONFIG_BYTES)
        return _HarborConfigWire.model_validate_json(content), content
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_HARBOR_CONFIG,
            path.name,
        ) from error


def _validate_source_adapter(benchmark_root: Path) -> None:
    path = benchmark_root / "agents/ofw_hermes.py"
    try:
        source = _bounded_text(path, _MAX_ADAPTER_BYTES)
    except (OSError, ValueError) as error:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_HARBOR_CONFIG,
            "agents/ofw_hermes.py",
        ) from error
    if _SOURCE_ENVIRONMENT_NAME not in source:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_HARBOR_CONFIG,
            _SOURCE_ENVIRONMENT_NAME,
        )


def _parse_job_result(path: Path) -> _HarborJobResultWire:
    try:
        return _HarborJobResultWire.model_validate_json(_bounded_text(path, _MAX_RESULT_BYTES))
    except (OSError, ValidationError, ValueError) as error:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "job result",
        ) from error


def _parse_trial_result(path: Path) -> _HarborTrialResultWire:
    try:
        return _HarborTrialResultWire.model_validate_json(_bounded_text(path, _MAX_RESULT_BYTES))
    except (OSError, ValidationError, ValueError) as error:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "trial result",
        ) from error


def _bounded_text(path: Path, maximum_bytes: int) -> str:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ValueError("file exceeds byte bound")
    return path.read_text(encoding="utf-8")


def _trial_results(job_path: Path) -> tuple[_HarborTrialResultWire, ...]:
    paths = tuple(
        sorted(
            (child / "result.json" for child in job_path.iterdir() if child.is_dir()),
            key=_result_sort_key,
        )
    )
    return tuple(_parse_trial_result(path) for path in paths if path.exists())


def _experiment_trials(run: ExperimentRun) -> tuple[ExperimentTrial, ...]:
    trials: list[ExperimentTrial] = []
    for directory in sorted(
        (child for child in run.job_path.iterdir() if child.is_dir()),
        key=_path_name,
    ):
        result_path = directory / "result.json"
        if result_path.exists():
            trials.append(_experiment_trial(run, directory.name, _parse_trial_result(result_path)))
    return _ordered_trials(tuple(trials), run.controls.task_ids)


def _path_name(path: Path) -> str:
    return path.name


def _ordered_trials(
    trials: tuple[ExperimentTrial, ...],
    task_ids: tuple[str, ...],
) -> tuple[ExperimentTrial, ...]:
    ordered: list[ExperimentTrial] = []
    for task_id in task_ids:
        matching = _matching_trials(trials, task_id)
        if len(matching) != 1:
            raise PreparationFailure(PreparationErrorCode.INVALID_BASELINE_RESULT, "task ids")
        ordered.append(matching[0])
    if len(ordered) != len(trials):
        raise PreparationFailure(PreparationErrorCode.INVALID_BASELINE_RESULT, "task ids")
    return tuple(ordered)


def _matching_trials(
    trials: tuple[ExperimentTrial, ...],
    task_id: str,
) -> tuple[ExperimentTrial, ...]:
    return tuple(trial for trial in trials if trial.task_id == task_id)


def _experiment_trial(
    run: ExperimentRun,
    directory_name: str,
    wire: _HarborTrialResultWire,
) -> ExperimentTrial:
    task_name, task_checksum, execution, verifier_wire = _required_trial_fields(
        wire,
        directory_name,
    )
    verifier = wire.verifier_result
    reward = None if verifier is None or verifier.rewards is None else verifier.rewards.reward
    verdict = None if verifier is None else verifier.verdict
    try:
        return ExperimentTrial(
            task_id=task_name,
            task_checksum=task_checksum,
            exception=wire.exception_info is not None,
            verdict=verdict,
            reward=reward,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            evaluated_at=verifier_wire.finished_at,
            evidence=(
                f"harbor://{run.run_id}/{directory_name}/result.json",
                f"harbor://{run.run_id}/{directory_name}/verifier",
            ),
        )
    except ValueError:
        raise PreparationFailure(
            PreparationErrorCode.INVALID_BASELINE_RESULT,
            "trial timestamps",
        ) from None


def _required_trial_fields(
    wire: _HarborTrialResultWire,
    directory_name: str,
) -> tuple[str, str, _HarborExecutionWire, _HarborVerifierWire]:
    if wire.task_id is None:
        raise _invalid_trial(directory_name)
    if wire.task_checksum is None:
        raise _invalid_trial(directory_name)
    if wire.agent_execution is None:
        raise _invalid_trial(directory_name)
    if wire.verifier is None:
        raise _invalid_trial(directory_name)
    return wire.task_id, wire.task_checksum, wire.agent_execution, wire.verifier


def _invalid_trial(directory_name: str) -> PreparationFailure:
    return PreparationFailure(
        PreparationErrorCode.INVALID_BASELINE_RESULT,
        directory_name,
    )


def _result_sort_key(path: Path) -> str:
    return path.parent.name


def _reward(trial: _HarborTrialResultWire) -> float | None:
    verifier = trial.verifier_result
    if trial.exception_info is not None or verifier is None or verifier.rewards is None:
        return None
    return verifier.rewards.reward


def _is_pass(trial: _HarborTrialResultWire) -> bool:
    return _reward(trial) == 1.0


def _is_failure(trial: _HarborTrialResultWire) -> bool:
    return _reward(trial) == 0.0


def _has_unsupported_reward(trial: _HarborTrialResultWire) -> bool:
    reward = _reward(trial)
    return reward is not None and reward not in (0.0, 1.0)


def _credentials() -> _Credentials:
    return _Credentials(
        openai_api_key=_required_any("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"),
        openai_base_url=_required_any("OPENAI_BASE_URL", "AZURE_OPENAI_BASE_URL"),
        langfuse_public_key=_required_any(
            "HERMES_LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_PUBLIC_KEY",
        ),
        langfuse_secret_key=_required_any(
            "HERMES_LANGFUSE_SECRET_KEY",
            "LANGFUSE_SECRET_KEY",
        ),
        langfuse_base_url=_required_any(
            "HERMES_LANGFUSE_BASE_URL",
            "LANGFUSE_BASE_URL",
        ),
    )


def _required_any(primary: str, fallback: str) -> str:
    value = os.environ.get(primary) or os.environ.get(fallback)
    if value is None or not value.strip():
        raise PreparationFailure(
            PreparationErrorCode.MISSING_ENVIRONMENT,
            f"{primary}|{fallback}",
        )
    return value.strip()


def _process_environment(run: ExperimentRun) -> dict[str, str]:
    credentials = _credentials()
    environment = dict(os.environ)
    environment.update(
        {
            "OPENAI_API_KEY": credentials.openai_api_key,
            "OPENAI_BASE_URL": credentials.openai_base_url,
            "HERMES_LANGFUSE_PUBLIC_KEY": credentials.langfuse_public_key,
            "HERMES_LANGFUSE_SECRET_KEY": credentials.langfuse_secret_key,
            "HERMES_LANGFUSE_BASE_URL": credentials.langfuse_base_url,
            "HERMES_LANGFUSE_ENV": run.controls.environment,
            "HERMES_LANGFUSE_RELEASE": run.release,
            "HERMES_LANGFUSE_SESSION_ID": run.session_id,
            _SOURCE_ENVIRONMENT_NAME: str(run.source_root),
            "PYTHONPATH": _python_path(run.benchmark_root, environment.get("PYTHONPATH")),
        }
    )
    return environment


def _require_run_controls(runner: HarborExperimentRunner, run: ExperimentRun) -> None:
    relative_config = _relative_run_config(run.benchmark_root, run.harbor_config)
    actual = runner.validate(
        run.benchmark_root,
        run.harbor_executable,
        relative_config,
    )
    if actual != run.controls:
        raise PreparationFailure(PreparationErrorCode.INVALID_HARBOR_CONFIG, "controls")


def _relative_run_config(benchmark_root: Path, harbor_config: Path) -> Path:
    try:
        return harbor_config.resolve(strict=True).relative_to(benchmark_root.resolve(strict=True))
    except (OSError, ValueError):
        raise PreparationFailure(PreparationErrorCode.INVALID_HARBOR_CONFIG, "controls") from None


def _baseline_experiment_run(
    run: BaselineRun,
    controls: ExperimentControls,
) -> ExperimentRun:
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
        controls=controls,
    )


def _python_path(root: Path, existing: str | None) -> str:
    if existing is None or not existing:
        return str(root)
    return f"{root}{os.pathsep}{existing}"
