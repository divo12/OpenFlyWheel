"""Canonical prepared-experiment policy stored outside the worktree."""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404
from contextlib import AbstractContextManager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ofw.preparation.contracts import (
    BaselineConfiguration,
    EditablePaths,
    ExperimentIdentifier,
    GoalText,
    IterationCount,
    NormalizedScore,
    PositiveMetric,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
    StrictModel,
    contained_relative_path,
)
from ofw.safe_file import (
    SafeFileErrorCode,
    SafeFileFailure,
    open_directory_chain,
    publish_idempotent,
    read_bounded,
)

_POLICY_LIMIT_BYTES = 256 * 1024
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_TASK_ID_PATTERN = r"^[^\x00]+$"
TaskIds = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=256, pattern=_TASK_ID_PATTERN)], ...],
    Field(min_length=1, max_length=500),
]


class _ExperimentPolicyContent(StrictModel):
    schema_version: Literal[1] = 1
    experiment_id: ExperimentIdentifier
    branch_name: str = Field(min_length=1, max_length=256)
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    initialization_commit: str = Field(pattern=_COMMIT_PATTERN)
    editable_paths: EditablePaths
    goal: GoalText
    quality_target: NormalizedScore
    max_iterations: IterationCount
    no_improvement_limit: IterationCount
    baseline_reused: bool = False
    max_cost_per_task_usd: PositiveMetric | None = None
    max_latency_seconds: PositiveMetric | None = None
    max_baseline_seconds: int = Field(strict=True, ge=60, le=172800)
    benchmark: Literal["itsm-bench"] = "itsm-bench"
    benchmark_config_digest: str = Field(pattern=_DIGEST_PATTERN)
    task_ids: TaskIds
    model: str = Field(min_length=1, max_length=256)
    verifier: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    concurrency: Literal[1] = 1
    max_retries: Literal[0] = 0

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("task_ids must be unique")
        return values

    @field_validator("editable_paths")
    @classmethod
    def validate_editable_paths(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        normalized = tuple(contained_relative_path(path, "editable_paths") for path in values)
        _require_unique_editable_paths(normalized)
        return normalized


class ExperimentPolicySnapshot(_ExperimentPolicyContent):
    """Immutable authority captured from validated preparation inputs and results."""

    controls_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_controls_digest(self) -> ExperimentPolicySnapshot:
        if self.controls_digest != self.recomputed_controls_digest():
            raise ValueError("controls_digest does not match canonical policy")
        return self

    def recomputed_controls_digest(self) -> str:
        content = _content_from_snapshot(self)
        return _digest(content.model_dump_json())


class ExperimentPolicyErrorCode(StrEnum):
    POLICY_SNAPSHOT_REQUIRED = "policy_snapshot_required"
    POLICY_INVALID = "policy_invalid"
    POLICY_CONFLICT = "policy_conflict"
    POLICY_TOO_LARGE = "policy_too_large"
    POLICY_WRITE_FAILED = "policy_write_failed"


class ExperimentPolicyFailure(Exception):
    """Typed sanitized policy persistence failure."""

    __slots__ = ("code", "subject")

    def __init__(self, code: ExperimentPolicyErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class FileExperimentPolicyRepository:
    """Publish and reload one canonical policy in the Git common control directory."""

    def publish(self, control_directory: Path, policy: ExperimentPolicySnapshot) -> Path:
        content = (policy.model_dump_json(indent=2) + "\n").encode("utf-8")
        try:
            with _open_control_directory(control_directory) as directory:
                publish_idempotent(
                    directory,
                    "policy.json",
                    content,
                    maximum_bytes=_POLICY_LIMIT_BYTES,
                    subject=policy.experiment_id,
                )
        except SafeFileFailure as error:
            raise _publication_failure(error, policy.experiment_id) from None
        except OSError:
            raise ExperimentPolicyFailure(
                ExperimentPolicyErrorCode.POLICY_WRITE_FAILED,
                policy.experiment_id,
            ) from None
        return control_directory / "policy.json"

    def load(self, workspace_root: Path, experiment_id: str) -> ExperimentPolicySnapshot:
        _require_experiment_id(experiment_id)
        control_directory = _control_directory(workspace_root, experiment_id)
        content = _read_policy_bytes(control_directory, experiment_id)
        try:
            policy = ExperimentPolicySnapshot.model_validate_json(content)
        except (ValidationError, ValueError, UnicodeError):
            raise ExperimentPolicyFailure(
                ExperimentPolicyErrorCode.POLICY_INVALID,
                experiment_id,
            ) from None
        if policy.experiment_id != experiment_id:
            raise ExperimentPolicyFailure(
                ExperimentPolicyErrorCode.POLICY_INVALID,
                experiment_id,
            )
        return policy


def build_experiment_policy(
    request: PrepareWorkspaceInput,
    prepared: PreparedGitWorkspace,
    baseline: BaselineConfiguration,
) -> ExperimentPolicySnapshot:
    """Derive the authoritative snapshot solely from validated preparation values."""
    content = _ExperimentPolicyContent(
        experiment_id=request.experiment_id,
        branch_name=prepared.branch_name,
        base_commit=prepared.base_commit,
        initialization_commit=prepared.initialization_commit,
        editable_paths=request.editable_paths,
        goal=request.goal,
        quality_target=request.quality_target,
        max_iterations=request.max_iterations,
        no_improvement_limit=request.no_improvement_limit,
        baseline_reused=request.reuse_existing_baseline,
        max_cost_per_task_usd=request.max_cost_per_task_usd,
        max_latency_seconds=request.max_latency_seconds,
        max_baseline_seconds=request.max_baseline_seconds,
        benchmark_config_digest=baseline.benchmark_config_digest,
        task_ids=baseline.task_ids,
        model=baseline.model,
        verifier=baseline.verifier,
        environment=baseline.environment,
    )
    return _snapshot_from_content(content)


def _content_from_snapshot(policy: ExperimentPolicySnapshot) -> _ExperimentPolicyContent:
    return _ExperimentPolicyContent(
        experiment_id=policy.experiment_id,
        branch_name=policy.branch_name,
        base_commit=policy.base_commit,
        initialization_commit=policy.initialization_commit,
        editable_paths=policy.editable_paths,
        goal=policy.goal,
        quality_target=policy.quality_target,
        max_iterations=policy.max_iterations,
        no_improvement_limit=policy.no_improvement_limit,
        baseline_reused=policy.baseline_reused,
        max_cost_per_task_usd=policy.max_cost_per_task_usd,
        max_latency_seconds=policy.max_latency_seconds,
        max_baseline_seconds=policy.max_baseline_seconds,
        benchmark_config_digest=policy.benchmark_config_digest,
        task_ids=policy.task_ids,
        model=policy.model,
        verifier=policy.verifier,
        environment=policy.environment,
    )


def _snapshot_from_content(content: _ExperimentPolicyContent) -> ExperimentPolicySnapshot:
    return ExperimentPolicySnapshot(
        experiment_id=content.experiment_id,
        branch_name=content.branch_name,
        base_commit=content.base_commit,
        initialization_commit=content.initialization_commit,
        editable_paths=content.editable_paths,
        goal=content.goal,
        quality_target=content.quality_target,
        max_iterations=content.max_iterations,
        no_improvement_limit=content.no_improvement_limit,
        baseline_reused=content.baseline_reused,
        max_cost_per_task_usd=content.max_cost_per_task_usd,
        max_latency_seconds=content.max_latency_seconds,
        max_baseline_seconds=content.max_baseline_seconds,
        benchmark_config_digest=content.benchmark_config_digest,
        task_ids=content.task_ids,
        model=content.model,
        verifier=content.verifier,
        environment=content.environment,
        controls_digest=_digest(content.model_dump_json()),
    )


def _control_directory(workspace_root: Path, experiment_id: str) -> Path:
    return experiment_control_directory(workspace_root, experiment_id)


def experiment_control_directory(workspace_root: Path, experiment_id: str) -> Path:
    """Return the experiment directory in Git's common control area."""
    _require_experiment_id(experiment_id)
    result = subprocess.run(
        ("git", "-C", str(workspace_root), "rev-parse", "--git-common-dir"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExperimentPolicyFailure(ExperimentPolicyErrorCode.POLICY_INVALID, experiment_id)
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = workspace_root / common
    return common / "ofw" / "preparations" / experiment_id


def _open_control_directory(control_directory: Path) -> AbstractContextManager[int]:
    common = control_directory.parents[2]
    return open_directory_chain(
        common,
        ("ofw", "preparations", control_directory.name),
        create=False,
    )


def _read_policy_bytes(control_directory: Path, experiment_id: str) -> bytes:
    try:
        with _open_control_directory(control_directory) as directory:
            return read_bounded(
                directory,
                "policy.json",
                maximum_bytes=_POLICY_LIMIT_BYTES,
                subject=experiment_id,
            )
    except FileNotFoundError:
        raise ExperimentPolicyFailure(
            ExperimentPolicyErrorCode.POLICY_SNAPSHOT_REQUIRED,
            experiment_id,
        ) from None
    except SafeFileFailure as error:
        raise _read_failure(error, experiment_id) from None
    except OSError:
        raise ExperimentPolicyFailure(
            ExperimentPolicyErrorCode.POLICY_INVALID,
            experiment_id,
        ) from None


def _require_experiment_id(experiment_id: str) -> None:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", experiment_id) is None:
        raise ExperimentPolicyFailure(ExperimentPolicyErrorCode.POLICY_INVALID, "experiment_id")


def _publication_failure(
    error: SafeFileFailure,
    experiment_id: str,
) -> ExperimentPolicyFailure:
    if error.code is SafeFileErrorCode.CONFLICT:
        code = ExperimentPolicyErrorCode.POLICY_CONFLICT
    elif error.code is SafeFileErrorCode.TOO_LARGE:
        code = ExperimentPolicyErrorCode.POLICY_TOO_LARGE
    else:
        code = ExperimentPolicyErrorCode.POLICY_WRITE_FAILED
    return ExperimentPolicyFailure(code, experiment_id)


def _read_failure(error: SafeFileFailure, experiment_id: str) -> ExperimentPolicyFailure:
    code = (
        ExperimentPolicyErrorCode.POLICY_TOO_LARGE
        if error.code is SafeFileErrorCode.TOO_LARGE
        else ExperimentPolicyErrorCode.POLICY_INVALID
    )
    return ExperimentPolicyFailure(code, experiment_id)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _require_unique_editable_paths(paths: tuple[Path, ...]) -> None:
    if len(set(paths)) != len(paths):
        raise ValueError("editable_paths must be unique")
