"""Bounded Harbor gateway for the ITSM baseline preparation workflow."""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ofw.preparation.contracts import (
    BaselineConfiguration,
    BaselineRun,
    BaselineSummary,
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


class _HarborTrialResultWire(_WireModel):
    exception_info: JsonValue | None = None
    verifier_result: _HarborVerifierResultWire | None = None


@dataclass(frozen=True, slots=True)
class _Credentials:
    openai_api_key: str
    openai_base_url: str
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str


class HarborBaselineRunner:
    """Launch one sequential ITSM Harbor job and parse its bounded results."""

    def validate(self, request: PrepareWorkspaceInput) -> BaselineConfiguration:
        _executable(request.harbor_executable)
        config_path = _contained(request.benchmark_root, request.harbor_config)
        config, config_content = _parse_config(config_path)
        if len(config.tasks) != request.expected_task_count:
            raise PreparationFailure(
                PreparationErrorCode.TASK_COUNT_MISMATCH,
                str(len(config.tasks)),
            )
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
        _validate_source_adapter(request.benchmark_root)
        _credentials()
        return BaselineConfiguration(
            model=agent.model_name,
            task_ids=task_ids,
            benchmark_config_digest=(
                f"sha256:{hashlib.sha256(config_content.encode('utf-8')).hexdigest()}"
            ),
            verifier="itsm-bench",
            environment="itsm-bench",
        )

    def start(self, run: BaselineRun) -> int:
        if run.job_path.exists():
            raise PreparationFailure(PreparationErrorCode.LAUNCH_FAILED, "job_path")
        command = (
            str(_executable(run.harbor_executable)),
            "run",
            "--config",
            str(run.harbor_config),
            "--job-name",
            run.experiment_id,
            "--jobs-dir",
            str(run.job_path.parent),
            "--n-concurrent",
            "1",
            "--max-retries",
            "0",
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


def _process_environment(run: BaselineRun) -> dict[str, str]:
    credentials = _credentials()
    environment = dict(os.environ)
    environment.update(
        {
            "OPENAI_API_KEY": credentials.openai_api_key,
            "OPENAI_BASE_URL": credentials.openai_base_url,
            "HERMES_LANGFUSE_PUBLIC_KEY": credentials.langfuse_public_key,
            "HERMES_LANGFUSE_SECRET_KEY": credentials.langfuse_secret_key,
            "HERMES_LANGFUSE_BASE_URL": credentials.langfuse_base_url,
            "HERMES_LANGFUSE_ENV": "itsm-bench",
            "HERMES_LANGFUSE_RELEASE": run.initialization_commit,
            "HERMES_LANGFUSE_SESSION_ID": run.experiment_id,
            _SOURCE_ENVIRONMENT_NAME: str(run.worktree_path),
            "PYTHONPATH": _python_path(run.benchmark_root, environment.get("PYTHONPATH")),
        }
    )
    return environment


def _python_path(root: Path, existing: str | None) -> str:
    if existing is None or not existing:
        return str(root)
    return f"{root}{os.pathsep}{existing}"
