"""Immutable contracts for ITSM workspace preparation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

_EXPERIMENT_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_REF_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._/@-]*"
_COMMIT_PATTERN = r"[0-9a-f]{40}"


def _normalized_score(value: object) -> float:
    return _numeric_float(value, "normalized score")


def _positive_metric(value: object) -> float:
    return _numeric_float(value, "positive metric")


def _numeric_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    return float(value)


ExperimentIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=_EXPERIMENT_PATTERN),
]
GitReference = Annotated[str, Field(min_length=1, max_length=256, pattern=_REF_PATTERN)]
GoalText = Annotated[str, Field(min_length=1, max_length=2000)]
PathValue = Annotated[Path, Field(strict=False)]
TaskCount = Annotated[int, Field(strict=True, ge=1, le=500)]
IterationCount = Annotated[int, Field(strict=True, ge=1, le=100)]
DurationSeconds = Annotated[int, Field(strict=True, ge=60, le=172800)]
NormalizedScore = Annotated[
    float,
    BeforeValidator(_normalized_score),
    Field(strict=True, ge=0.0, le=1.0),
]
PositiveMetric = Annotated[
    float,
    BeforeValidator(_positive_metric),
    Field(strict=True, gt=0.0),
]
EditablePaths = Annotated[
    tuple[PathValue, ...],
    Field(strict=False, min_length=1, max_length=50),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PrepareWorkspaceInput(StrictModel):
    """Strict user-confirmed ITSM experiment configuration."""

    experiment_id: ExperimentIdentifier
    harness_root: PathValue
    base_ref: GitReference
    worktree_parent: PathValue
    benchmark_root: PathValue
    harbor_executable: PathValue
    harbor_config: PathValue
    expected_task_count: TaskCount
    editable_paths: EditablePaths
    goal: GoalText
    quality_target: NormalizedScore
    max_iterations: IterationCount
    no_improvement_limit: IterationCount
    max_cost_per_task_usd: PositiveMetric | None = None
    max_latency_seconds: PositiveMetric | None = None
    max_baseline_seconds: DurationSeconds

    @field_validator(
        "harness_root",
        "worktree_parent",
        "benchmark_root",
        "harbor_executable",
    )
    @classmethod
    def validate_absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("path must be absolute")
        return value

    @field_validator("harbor_config")
    @classmethod
    def validate_harbor_config(cls, value: Path) -> Path:
        return contained_relative_path(value, "harbor_config")

    @field_validator("editable_paths")
    @classmethod
    def validate_editable_paths(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        normalized = tuple(contained_relative_path(value, "editable_paths") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("editable_paths must be unique")
        return normalized


class PreparationPhase(StrEnum):
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class PreparationStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class PreparationErrorCode(StrEnum):
    INVALID_REPOSITORY = "invalid_repository"
    INVALID_PATH = "invalid_path"
    BASE_REF_NOT_FOUND = "base_ref_not_found"
    BRANCH_EXISTS = "branch_exists"
    WORKTREE_EXISTS = "worktree_exists"
    MANAGED_FILE_EXISTS = "managed_file_exists"
    EDITABLE_PATH_MISSING = "editable_path_missing"
    TASK_COUNT_MISMATCH = "task_count_mismatch"
    INVALID_HARBOR_CONFIG = "invalid_harbor_config"
    MISSING_ENVIRONMENT = "missing_environment"
    LAUNCH_FAILED = "launch_failed"
    REQUEST_CONFLICT = "request_conflict"
    BASELINE_TIMEOUT = "baseline_timeout"
    INVALID_BASELINE_RESULT = "invalid_baseline_result"
    PREPARATION_BUSY = "preparation_busy"
    GIT_FAILED = "git_failed"
    POLICY_CONFLICT = "policy_conflict"
    POLICY_WRITE_FAILED = "policy_write_failed"


class WorkspacePreparationObservation(StrictModel):
    status: PreparationStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(max_length=10)
    preparation_id: ExperimentIdentifier
    phase: PreparationPhase
    branch_name: str | None = Field(default=None, max_length=256)
    worktree_path: Path | None = None
    base_commit: str | None = Field(default=None, pattern=_COMMIT_PATTERN)
    initialization_commit: str | None = Field(default=None, pattern=_COMMIT_PATTERN)
    program_path: Path | None = None
    job_path: Path | None = None
    session_id: str | None = Field(default=None, max_length=199)
    terminal_trials: int | None = Field(default=None, ge=0, le=500)
    verifier_passes: int | None = Field(default=None, ge=0, le=500)
    verifier_failures: int | None = Field(default=None, ge=0, le=500)
    unverified_trials: int | None = Field(default=None, ge=0, le=500)
    unsupported_reward_trials: int | None = Field(default=None, ge=0, le=500)
    next_poll_after_seconds: int | None = Field(default=None, ge=1, le=300)
    error_code: PreparationErrorCode | None = None
    root_cause_hint: str | None = Field(default=None, max_length=256)
    retry: str | None = Field(default=None, max_length=256)
    stop_when: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True, slots=True)
class BaselineConfiguration:
    model: str
    task_ids: tuple[str, ...]
    benchmark_config_digest: str
    verifier: str
    environment: str

    @property
    def task_count(self) -> int:
        return len(self.task_ids)


@dataclass(frozen=True, slots=True)
class BaselineRun:
    experiment_id: str
    benchmark_root: Path
    harbor_executable: Path
    harbor_config: Path
    job_path: Path
    log_path: Path
    worktree_path: Path
    initialization_commit: str


@dataclass(frozen=True, slots=True)
class BaselineSummary:
    terminal_trials: int
    verifier_passes: int
    verifier_failures: int
    unverified_trials: int
    unsupported_reward_trials: int


@dataclass(frozen=True, slots=True)
class PreparedGitWorkspace:
    branch_name: str
    worktree_path: Path
    base_commit: str
    initialization_commit: str
    program_path: Path


class BaselineRunner(Protocol):
    def validate(self, request: PrepareWorkspaceInput) -> BaselineConfiguration: ...

    def start(self, run: BaselineRun) -> int: ...

    def summarize(self, run: BaselineRun) -> BaselineSummary | None: ...


class WorkspaceGateway(Protocol):
    def control_directory(self, harness_root: Path, experiment_id: str) -> Path: ...

    def prepare(
        self,
        request: PrepareWorkspaceInput,
        program: str,
        baseline: BaselineConfiguration,
    ) -> PreparedGitWorkspace: ...


class PreparationFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: PreparationErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


def contained_relative_path(value: Path, field: str) -> Path:
    _require_relative_shape(value, field)
    _require_bounded_path_text(value, field)
    return value


def _require_relative_shape(value: Path, field: str) -> None:
    if value.is_absolute() or ".." in value.parts or value == Path("."):
        raise ValueError(f"{field} must be a contained relative path")


def _require_bounded_path_text(value: Path, field: str) -> None:
    text = value.as_posix()
    if "\x00" in text or len(text.encode("utf-8")) > 1024:
        raise ValueError(f"{field} must be a bounded text path")
