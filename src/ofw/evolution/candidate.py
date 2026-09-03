"""Immutable candidate-execution contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, field_validator

from ofw.evaluation.langfuse import OutcomeScoreSubmission
from ofw.evaluation.outcome import OutcomeEvaluation, VerifierVerdict
from ofw.evolution.hypothesis import HarnessHypothesis, StrictModel
from ofw.preparation.contracts import (
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    PathValue,
    contained_relative_path,
)
from ofw.preparation.policy import ExperimentPolicySnapshot

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")


class _CandidateIdentity(StrictModel):
    schema_version: Literal[1] = 1
    policy_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    hypothesis_id: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    source_commit: str = Field(pattern=r"[0-9a-f]{40}")
    candidate_tree: str = Field(pattern=r"[0-9a-f]{40}")
    controls_digest: str = Field(pattern=r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class CandidateId:
    value: str

    def __post_init__(self) -> None:
        if _DIGEST_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid candidate id")

    def __str__(self) -> str:
        return self.value

    @classmethod
    def build(
        cls,
        *,
        policy_digest: str,
        hypothesis_id: str,
        source_commit: str,
        candidate_tree: str,
        controls_digest: str,
    ) -> CandidateId:
        identity = _CandidateIdentity(
            policy_digest=policy_digest,
            hypothesis_id=hypothesis_id,
            source_commit=source_commit,
            candidate_tree=candidate_tree,
            controls_digest=controls_digest,
        )
        digest = hashlib.sha256(identity.model_dump_json().encode("utf-8")).hexdigest()
        return cls(f"sha256:{digest}")


def candidate_policy_digest(policy: ExperimentPolicySnapshot) -> str:
    digest = hashlib.sha256(policy.model_dump_json().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class CandidateErrorCode(StrEnum):
    INVALID_WORKSPACE = "invalid_workspace"
    WORKTREE_EXISTS = "worktree_exists"
    STALE_COMMIT = "stale_commit"
    STALE_POLICY = "stale_policy"
    EMPTY_CANDIDATE = "empty_candidate"
    OUT_OF_SCOPE = "out_of_scope"
    UNSAFE_PATH = "unsafe_path"
    MANAGED_PATH = "managed_path"
    CREDENTIAL_PATH = "credential_path"
    CONTROLS_DRIFT = "controls_drift"
    REQUEST_CONFLICT = "request_conflict"
    MISSING_ENVIRONMENT = "missing_environment"
    LAUNCH_FAILED = "launch_failed"
    CANDIDATE_TIMEOUT = "candidate_timeout"
    INVALID_RESULT = "invalid_result"
    OUTCOME_STORE_FAILED = "outcome_store_failed"
    GIT_FAILED = "git_failed"


class CandidateFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: CandidateErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    accepted_root: Path
    worktree_path: Path
    source_commit: str


@dataclass(frozen=True, slots=True)
class CandidateTree:
    tree_id: str
    changed_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CandidateCommit:
    commit: str


class CandidatePhase(StrEnum):
    EDITING = "editing"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class CandidateBlockerCode(StrEnum):
    TRACE_NOT_FOUND = "trace_not_found"
    TRACE_AMBIGUOUS = "trace_ambiguous"
    UNVERIFIED = "unverified"
    UNSUPPORTED_REWARD = "unsupported_reward"


class CandidateExecutionInput(StrictModel):
    workspace_root: PathValue
    worktree_parent: PathValue
    benchmark_root: PathValue
    harbor_executable: PathValue
    harbor_config: PathValue
    experiment_id: str = Field(pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*", max_length=80)
    hypothesis_id: str = Field(pattern=r"sha256:[0-9a-f]{64}")

    @field_validator(
        "workspace_root",
        "worktree_parent",
        "benchmark_root",
        "harbor_executable",
    )
    @classmethod
    def validate_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("path must be absolute")
        return value

    @field_validator("harbor_config")
    @classmethod
    def validate_config(cls, value: Path) -> Path:
        return contained_relative_path(value, "harbor_config")


class CandidateOutcomeReceipt(StrictModel):
    task_id: str = Field(min_length=1, max_length=256)
    trace_id: str = Field(min_length=1, max_length=256)
    score_id: str = Field(min_length=1, max_length=256)
    verdict: VerifierVerdict


class CandidateBlocker(StrictModel):
    task_id: str = Field(min_length=1, max_length=256)
    code: CandidateBlockerCode
    subject: str = Field(min_length=1, max_length=256)


class CandidateExecutionObservation(StrictModel):
    status: CandidateStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(max_length=10)
    phase: CandidatePhase
    experiment_id: str = Field(min_length=1, max_length=80)
    hypothesis_id: str = Field(pattern=r"sha256:[0-9a-f]{64}")
    source_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    candidate_id: str | None = Field(default=None, pattern=r"sha256:[0-9a-f]{64}")
    candidate_tree: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    candidate_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    worktree_path: Path | None = None
    job_path: Path | None = None
    session_id: str | None = Field(default=None, max_length=199)
    terminal_trials: int | None = Field(default=None, ge=0, le=500)
    verifier_passes: int | None = Field(default=None, ge=0, le=500)
    verifier_failures: int | None = Field(default=None, ge=0, le=500)
    unverified_trials: int | None = Field(default=None, ge=0, le=500)
    outcome_receipts: tuple[CandidateOutcomeReceipt, ...] = Field(max_length=500)
    blockers: tuple[CandidateBlocker, ...] = Field(max_length=500)
    next_poll_after_seconds: int | None = Field(default=None, ge=1, le=300)
    error_code: CandidateErrorCode | None = None


@dataclass(frozen=True, slots=True)
class TraceMatchRequest:
    task_id: str
    session_id: str
    environment: str
    release: str
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class TraceMatch:
    trace_id: str | None
    blocker: CandidateBlockerCode | None

    def __post_init__(self) -> None:
        if (self.trace_id is None) == (self.blocker is None):
            raise ValueError("trace match requires exactly one result")
        if self.trace_id is not None and (
            len(self.trace_id) > 256 or _IDENTIFIER_PATTERN.fullmatch(self.trace_id) is None
        ):
            raise CandidateFailure(CandidateErrorCode.INVALID_RESULT, "trace_id")


class CandidateWorkspaceGateway(Protocol):
    def control_directory(self, root: Path, hypothesis_id: str) -> Path: ...

    def prepare(
        self,
        accepted_root: Path,
        worktree_parent: Path,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> CandidateWorkspace: ...

    def inspect(
        self,
        workspace: CandidateWorkspace,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> CandidateTree: ...

    def commit(
        self,
        workspace: CandidateWorkspace,
        tree: CandidateTree,
        candidate_id: CandidateId,
        experiment_id: str,
    ) -> CandidateCommit: ...

    def validate_accepted(
        self,
        root: Path,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> None: ...


class CandidateHypothesisRepository(Protocol):
    def load_policy(self, root: Path, experiment_id: str) -> ExperimentPolicySnapshot: ...

    def load(self, root: Path, hypothesis_id: str) -> HarnessHypothesis: ...


class CandidateExperimentRunner(Protocol):
    def validate(
        self,
        benchmark_root: Path,
        harbor_executable: Path,
        harbor_config: Path,
    ) -> ExperimentControls: ...

    def start(self, run: ExperimentRun) -> int: ...

    def summarize(self, run: ExperimentRun) -> ExperimentSummary | None: ...


class CandidateTraceLocator(Protocol):
    def locate(self, request: TraceMatchRequest) -> TraceMatch: ...


class CandidateOutcomeStore(Protocol):
    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission: ...
