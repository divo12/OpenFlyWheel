"""Bounded local persistence for mined failure diagnoses."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ofw.evaluation.failure import FailureDiagnosis, FailureEvidenceStatus, FailureType
from ofw.evaluation.outcome import OutcomeEvaluation, TaskId, VerifierId
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId
from ofw.runtime import EvidenceReference, VerifierVerdict

_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:@/-]*"
_ARTIFACT_ID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ARTIFACT_LIMIT_BYTES = 64 * 1024
_WORKSPACE_DIRECTORY = ".workspace"
_FAILURE_DIRECTORY = "failures"
_IGNORE_CONTENT = "*\n"

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=_IDENTIFIER_PATTERN),
]
EvidenceReferenceValue = Annotated[str, Field(min_length=1, max_length=1024)]
DiagnosisText = Annotated[str, Field(min_length=1, max_length=4000)]
WorkspaceRoot = Annotated[Path, Field(strict=False)]
ObservationIdentifiers = Annotated[tuple[Identifier, ...], Field(max_length=10)]
OutcomeEvidence = Annotated[
    tuple[EvidenceReferenceValue, ...],
    Field(min_length=1, max_length=10),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailedOutcomeInput(StrictModel):
    trace_id: Identifier
    task_id: Identifier
    verifier_id: Identifier
    evaluated_at: datetime
    score: float = Field(strict=True, ge=0.0, le=1.0)
    evidence: OutcomeEvidence
    outcome_score_id: Identifier

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("evaluated_at must be UTC")
        return value

    def to_outcome(self) -> OutcomeEvaluation:
        return OutcomeEvaluation(
            trace_id=TraceId(self.trace_id),
            task_id=TaskId(self.task_id),
            verifier_id=VerifierId(self.verifier_id),
            evaluated_at=self.evaluated_at,
            verdict=VerifierVerdict.FAIL,
            score=self.score,
            evidence=tuple(EvidenceReference(value) for value in self.evidence),
        )


class RecordFailureInput(StrictModel):
    workspace_root: WorkspaceRoot
    outcome: FailedOutcomeInput
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None
    expected_outcome: DiagnosisText
    actual_outcome: DiagnosisText
    critical_observation_id: Identifier | None
    evidence_observation_ids: ObservationIdentifiers
    root_cause: DiagnosisText | None
    counterfactual_action: DiagnosisText | None
    inconclusive_reason: DiagnosisText | None

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    def to_diagnosis(self) -> FailureDiagnosis:
        return FailureDiagnosis(
            outcome=self.outcome.to_outcome(),
            outcome_score_id=ScoreId(self.outcome.outcome_score_id),
            evidence_status=self.evidence_status,
            issue_type=self.issue_type,
            expected_outcome=self.expected_outcome,
            actual_outcome=self.actual_outcome,
            critical_observation_id=_observation_id(self.critical_observation_id),
            evidence_observation_ids=tuple(
                ObservationId(value) for value in self.evidence_observation_ids
            ),
            root_cause=self.root_cause,
            counterfactual_action=self.counterfactual_action,
            inconclusive_reason=self.inconclusive_reason,
        )


class FailureRecordStatus(StrEnum):
    SUCCESS = "success"


class FailureRecordObservation(StrictModel):
    status: FailureRecordStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(min_length=2, max_length=2)
    trace_id: Identifier
    task_id: Identifier
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    relative_path: Path
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None


class FailureArtifact(StrictModel):
    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    trace_id: Identifier
    task_id: Identifier
    verifier_id: Identifier
    evaluated_at: datetime
    normalized_score: float = Field(strict=True, ge=0.0, le=1.0)
    outcome_score_id: Identifier
    outcome_evidence: OutcomeEvidence
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None
    expected_outcome: DiagnosisText
    actual_outcome: DiagnosisText
    critical_observation_id: Identifier | None
    evidence_observation_ids: ObservationIdentifiers
    root_cause: DiagnosisText | None
    counterfactual_action: DiagnosisText | None
    inconclusive_reason: DiagnosisText | None

    @classmethod
    def from_diagnosis(
        cls,
        artifact_id: str,
        diagnosis: FailureDiagnosis,
    ) -> FailureArtifact:
        outcome = diagnosis.outcome
        return cls(
            artifact_id=artifact_id,
            trace_id=outcome.trace_id.value,
            task_id=outcome.task_id.value,
            verifier_id=outcome.verifier_id.value,
            evaluated_at=outcome.evaluated_at,
            normalized_score=_required_score(outcome),
            outcome_score_id=diagnosis.outcome_score_id.value,
            outcome_evidence=tuple(reference.value for reference in outcome.evidence),
            evidence_status=diagnosis.evidence_status,
            issue_type=diagnosis.issue_type,
            expected_outcome=diagnosis.expected_outcome,
            actual_outcome=diagnosis.actual_outcome,
            critical_observation_id=_observation_value(diagnosis.critical_observation_id),
            evidence_observation_ids=tuple(
                observation_id.value for observation_id in diagnosis.evidence_observation_ids
            ),
            root_cause=diagnosis.root_cause,
            counterfactual_action=diagnosis.counterfactual_action,
            inconclusive_reason=diagnosis.inconclusive_reason,
        )

@dataclass(frozen=True, slots=True)
class FailureArtifactReceipt:
    artifact_id: str
    relative_path: Path


class FailureWorkspaceErrorCode(StrEnum):
    INVALID_WORKSPACE = "invalid_workspace"
    ARTIFACT_CONFLICT = "artifact_conflict"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    WRITE_FAILED = "write_failed"


class FailureWorkspaceFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: FailureWorkspaceErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class FailureWorkspace(Protocol):
    def store(self, root: Path, diagnosis: FailureDiagnosis) -> FailureArtifactReceipt: ...


@dataclass(frozen=True, slots=True)
class FailureWorkspaceService:
    workspace: FailureWorkspace

    def record(self, request: RecordFailureInput) -> FailureRecordObservation:
        diagnosis = request.to_diagnosis()
        receipt = self.workspace.store(request.workspace_root, diagnosis)
        return FailureRecordObservation(
            status=FailureRecordStatus.SUCCESS,
            summary="Stored one failure diagnosis in the local workspace.",
            next_actions=("Retain the artifact path for failure analysis.",),
            artifacts=(str(receipt.relative_path), receipt.artifact_id),
            trace_id=diagnosis.outcome.trace_id.value,
            task_id=diagnosis.outcome.task_id.value,
            artifact_id=receipt.artifact_id,
            relative_path=receipt.relative_path,
            evidence_status=diagnosis.evidence_status,
            issue_type=diagnosis.issue_type,
        )


class FileFailureWorkspace:
    """Store compact diagnoses under a prepared harness's ignored runtime workspace."""

    def store(self, root: Path, diagnosis: FailureDiagnosis) -> FailureArtifactReceipt:
        artifact_id = _artifact_id(diagnosis)
        try:
            return self._store(root, diagnosis, artifact_id)
        except FailureWorkspaceFailure:
            raise
        except (OSError, RuntimeError, UnicodeError):
            raise FailureWorkspaceFailure(
                FailureWorkspaceErrorCode.WRITE_FAILED,
                artifact_id,
            ) from None

    def _store(
        self,
        root: Path,
        diagnosis: FailureDiagnosis,
        artifact_id: str,
    ) -> FailureArtifactReceipt:
        prepared_root = _prepared_root(root)
        workspace, failures = _workspace_paths(prepared_root)
        artifact = FailureArtifact.from_diagnosis(artifact_id, diagnosis)
        text = artifact.model_dump_json(indent=2) + "\n"
        _validate_artifact_size(text)
        _prepare_workspace_directories(prepared_root, workspace, failures)
        path = failures / f"{artifact_id}.json"
        receipt = FailureArtifactReceipt(artifact_id, path.relative_to(prepared_root))
        if path.exists():
            _validate_existing(path, text, artifact_id)
            return receipt
        # ponytail: single-writer workspace; add per-artifact locks if concurrent miners appear.
        _atomic_write(path, text)
        return receipt


def _observation_id(value: str | None) -> ObservationId | None:
    return None if value is None else ObservationId(value)


def _observation_value(value: ObservationId | None) -> str | None:
    return None if value is None else value.value


def _required_score(outcome: OutcomeEvaluation) -> float:
    score = outcome.score
    if score is None:
        raise FailureWorkspaceFailure(FailureWorkspaceErrorCode.WRITE_FAILED, "outcome_score")
    return score


def _artifact_id(diagnosis: FailureDiagnosis) -> str:
    identity = "\0".join(
        (
            "ofw.failure",
            diagnosis.outcome.trace_id.value,
            diagnosis.outcome_score_id.value,
        )
    )
    return str(uuid5(NAMESPACE_URL, identity))


def _prepared_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.INVALID_WORKSPACE,
            "workspace_root",
        ) from None
    markers = (resolved / "PROGRAM.md", resolved / "experiment_config.yaml")
    if not resolved.is_dir() or any(not marker.is_file() for marker in markers):
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.INVALID_WORKSPACE,
            "workspace_root",
        )
    return resolved


def _workspace_paths(root: Path) -> tuple[Path, Path]:
    workspace = root / _WORKSPACE_DIRECTORY
    failures = workspace / _FAILURE_DIRECTORY
    _require_contained(root, workspace.resolve(strict=False))
    _require_contained(root, failures.resolve(strict=False))
    if workspace.exists() and not workspace.is_dir():
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.INVALID_WORKSPACE,
            _WORKSPACE_DIRECTORY,
        )
    return workspace, failures


def _require_contained(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.INVALID_WORKSPACE,
            _WORKSPACE_DIRECTORY,
        ) from None


def _validate_artifact_size(text: str) -> None:
    if len(text.encode()) > _ARTIFACT_LIMIT_BYTES:
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE,
            str(_ARTIFACT_LIMIT_BYTES),
        )


def _prepare_workspace_directories(root: Path, workspace: Path, failures: Path) -> None:
    failures.mkdir(parents=True, exist_ok=True)
    _require_contained(root, workspace.resolve(strict=True))
    _require_contained(root, failures.resolve(strict=True))
    ignore_path = workspace / ".gitignore"
    _require_contained(workspace, ignore_path.resolve(strict=False))
    if not ignore_path.exists():
        _atomic_write(ignore_path, _IGNORE_CONTENT)


def _validate_existing(path: Path, expected: str, artifact_id: str) -> None:
    if path.stat().st_size > _ARTIFACT_LIMIT_BYTES:
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE,
            artifact_id,
        )
    if path.read_text(encoding="utf-8") != expected:
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.ARTIFACT_CONFLICT,
            artifact_id,
        )


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".ofw-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
