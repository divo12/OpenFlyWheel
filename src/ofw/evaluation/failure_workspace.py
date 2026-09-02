"""Bounded local persistence for mined failure diagnoses."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Never, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ofw.contracts import Sha256Digest
from ofw.evaluation.failure import (
    FailureDiagnosis,
    FailureDiagnosisError,
    FailureEvidenceStatus,
    FailureType,
)
from ofw.evaluation.failure_curation import (
    FailureCuration,
    FailureCurationArtifact,
    FailureCurationErrorCode,
    FailureCurationFailure,
    FailureCurationReceipt,
    FailureSource,
)
from ofw.evaluation.failure_patterns import (
    FailureDiagnosisRecord,
    FailurePatternMiningError,
    FailurePatternMiningErrorCode,
)
from ofw.evaluation.outcome import (
    EvidenceReference,
    OutcomeEvaluation,
    OutcomeEvaluationError,
    TaskId,
    VerifierId,
    VerifierVerdict,
)
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId
from ofw.safe_file import (
    SafeFileErrorCode,
    SafeFileFailure,
    publish_idempotent,
    read_bounded,
)

_IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:@/-]*"
_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_ARTIFACT_ID = re.compile(_ARTIFACT_ID_PATTERN)
_ARTIFACT_LIMIT_BYTES = 64 * 1024
_WORKSPACE_DIRECTORY = ".workspace"
_FAILURE_DIRECTORY = "failures"
_CURATION_DIRECTORY = "failure-curations"
_IGNORE_CONTENT = "*\n"
_WORKSPACE_MARKERS = ("PROGRAM.md", "experiment_config.yaml")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=_IDENTIFIER_PATTERN),
]
EvidenceReferenceValue = Annotated[str, Field(min_length=1, max_length=1024)]
DiagnosisText = Annotated[str, Field(min_length=1, max_length=4000)]
WorkspaceRoot = Annotated[Path, Field(strict=False)]
ObservationIdentifiers = Annotated[tuple[Identifier, ...], Field(max_length=10)]
JsonObservationIdentifiers = Annotated[
    tuple[Identifier, ...],
    Field(strict=False, max_length=10),
]
OutcomeEvidence = Annotated[
    tuple[EvidenceReferenceValue, ...],
    Field(min_length=1, max_length=10),
]
JsonOutcomeEvidence = Annotated[
    tuple[EvidenceReferenceValue, ...],
    Field(strict=False, min_length=1, max_length=10),
]
JsonTimestamp = Annotated[datetime, Field(strict=False)]
JsonEvidenceStatus = Annotated[FailureEvidenceStatus, Field(strict=False)]
JsonFailureType = Annotated[FailureType, Field(strict=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailedOutcomeInput(StrictModel):
    trace_id: Identifier
    task_id: Identifier
    verifier_id: Identifier
    evaluated_at: JsonTimestamp
    score: float = Field(strict=True, ge=0.0, le=1.0)
    evidence: JsonOutcomeEvidence
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
    evidence_status: JsonEvidenceStatus
    issue_type: JsonFailureType | None
    expected_outcome: DiagnosisText
    actual_outcome: DiagnosisText
    critical_observation_id: Identifier | None
    evidence_observation_ids: JsonObservationIdentifiers
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

    @model_validator(mode="after")
    def validate_domain_contract(self) -> FailureArtifact:
        try:
            self.to_diagnosis()
        except (FailureDiagnosisError, OutcomeEvaluationError) as error:
            raise ValueError("invalid failure artifact") from error
        return self

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

    def to_diagnosis(self) -> FailureDiagnosis:
        outcome = OutcomeEvaluation(
            trace_id=TraceId(self.trace_id),
            task_id=TaskId(self.task_id),
            verifier_id=VerifierId(self.verifier_id),
            evaluated_at=self.evaluated_at,
            verdict=VerifierVerdict.FAIL,
            score=self.normalized_score,
            evidence=tuple(EvidenceReference(value) for value in self.outcome_evidence),
        )
        return FailureDiagnosis(
            outcome=outcome,
            outcome_score_id=ScoreId(self.outcome_score_id),
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


@dataclass(frozen=True, slots=True)
class FailureArtifactReceipt:
    artifact_id: str
    relative_path: Path


@dataclass(frozen=True, slots=True)
class _DirectoryChainIdentity:
    root: tuple[int, int]
    workspace: tuple[int, int]
    artifacts: tuple[int, int]


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
            artifact = FailureArtifact.from_diagnosis(artifact_id, diagnosis)
            content = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
            relative_path = _store_artifact(
                root,
                _FAILURE_DIRECTORY,
                artifact_id,
                content,
            )
            return FailureArtifactReceipt(artifact_id, relative_path)
        except (OSError, RuntimeError, UnicodeError):
            raise FailureWorkspaceFailure(
                FailureWorkspaceErrorCode.WRITE_FAILED,
                artifact_id,
            ) from None

    def read(
        self,
        root: Path,
        artifact_ids: tuple[str, ...],
    ) -> tuple[FailureDiagnosisRecord, ...]:
        try:
            return self._read(root, artifact_ids)
        except FailurePatternMiningError:
            raise
        except FailureWorkspaceFailure as error:
            raise _pattern_read_error(error) from None
        except (OSError, RuntimeError, UnicodeError):
            raise FailurePatternMiningError(
                FailurePatternMiningErrorCode.READ_FAILED,
                "failure_artifacts",
            ) from None

    def _read(
        self,
        root: Path,
        artifact_ids: tuple[str, ...],
    ) -> tuple[FailureDiagnosisRecord, ...]:
        prepared_root = _prepared_root(root)
        workspace, failures = _workspace_artifact_paths(prepared_root, _FAILURE_DIRECTORY)
        directory_identity = _existing_workspace_directories(
            prepared_root,
            workspace,
            failures,
        )
        with _artifact_directory_handle(
            prepared_root,
            _FAILURE_DIRECTORY,
            directory_identity,
        ) as directory:
            return tuple(_read_artifact(directory, artifact_id) for artifact_id in artifact_ids)


class FileFailureCurationWorkspace:
    """Read compact diagnoses and store one bounded cross-failure curation."""

    def load(self, root: Path, artifact_ids: tuple[str, ...]) -> tuple[FailureSource, ...]:
        try:
            return self._load(root, artifact_ids)
        except FailureCurationFailure:
            raise
        except FailureWorkspaceFailure as error:
            code = (
                FailureCurationErrorCode.SOURCE_INVALID
                if error.code is FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE
                else FailureCurationErrorCode.INVALID_WORKSPACE
            )
            raise FailureCurationFailure(code, error.subject) from None
        except (OSError, RuntimeError, UnicodeError):
            raise FailureCurationFailure(
                FailureCurationErrorCode.SOURCE_INVALID,
                "failure_artifacts",
            ) from None

    def store(self, root: Path, curation: FailureCuration) -> FailureCurationReceipt:
        try:
            artifact = FailureCurationArtifact.from_curation(curation)
            content = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
            relative_path = _store_artifact(
                root,
                _CURATION_DIRECTORY,
                curation.id,
                content,
            )
            return FailureCurationReceipt(curation.id, relative_path)
        except FailureWorkspaceFailure as error:
            raise FailureCurationFailure(_curation_workspace_error(error), error.subject) from None
        except (OSError, RuntimeError, UnicodeError):
            raise FailureCurationFailure(
                FailureCurationErrorCode.WRITE_FAILED,
                curation.id,
            ) from None

    def _load(
        self,
        root: Path,
        artifact_ids: tuple[str, ...],
    ) -> tuple[FailureSource, ...]:
        prepared_root = _prepared_root(root)
        workspace, failures = _workspace_artifact_paths(prepared_root, _FAILURE_DIRECTORY)
        try:
            identity = _existing_directory_identity(prepared_root, workspace, failures)
            with _artifact_directory_handle(
                prepared_root,
                _FAILURE_DIRECTORY,
                identity,
            ) as directory:
                return tuple(
                    _read_failure_source(directory, artifact_id) for artifact_id in artifact_ids
                )
        except FileNotFoundError:
            missing = next(
                (
                    artifact_id
                    for artifact_id in artifact_ids
                    if not (failures / f"{artifact_id}.json").exists()
                ),
                artifact_ids[0],
            )
            raise FailureCurationFailure(
                FailureCurationErrorCode.SOURCE_NOT_FOUND,
                missing,
            ) from None


def _observation_id(value: str | None) -> ObservationId | None:
    return None if value is None else ObservationId(value)


def _observation_value(value: ObservationId | None) -> str | None:
    return None if value is None else value.value


def _required_score(outcome: OutcomeEvaluation) -> float:
    score = outcome.score
    if score is None:
        raise FailureWorkspaceFailure(FailureWorkspaceErrorCode.WRITE_FAILED, "outcome_score")
    return score


def _read_failure_source(directory: int, artifact_id: str) -> FailureSource:
    try:
        content = _read_existing(directory, f"{artifact_id}.json", artifact_id)
        artifact = FailureArtifact.model_validate_json(content)
    except FileNotFoundError:
        raise FailureCurationFailure(
            FailureCurationErrorCode.SOURCE_NOT_FOUND,
            artifact_id,
        ) from None
    except (FailureWorkspaceFailure, ValidationError):
        raise FailureCurationFailure(
            FailureCurationErrorCode.SOURCE_INVALID,
            artifact_id,
        ) from None
    if artifact.artifact_id != artifact_id:
        raise FailureCurationFailure(
            FailureCurationErrorCode.SOURCE_INVALID,
            artifact_id,
        )
    diagnosis = artifact.to_diagnosis()
    return FailureSource(
        artifact_id=artifact.artifact_id,
        artifact_digest=Sha256Digest(f"sha256:{hashlib.sha256(content).hexdigest()}"),
        trace_id=diagnosis.outcome.trace_id,
        task_id=diagnosis.outcome.task_id,
        outcome_score_id=diagnosis.outcome_score_id,
        evidence_status=diagnosis.evidence_status,
        issue_type=diagnosis.issue_type,
        critical_observation_id=diagnosis.critical_observation_id,
    )


def _curation_workspace_error(error: FailureWorkspaceFailure) -> FailureCurationErrorCode:
    if error.code is FailureWorkspaceErrorCode.INVALID_WORKSPACE:
        return FailureCurationErrorCode.INVALID_WORKSPACE
    if error.code is FailureWorkspaceErrorCode.ARTIFACT_CONFLICT:
        return FailureCurationErrorCode.ARTIFACT_CONFLICT
    if error.code is FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE:
        return FailureCurationErrorCode.ARTIFACT_TOO_LARGE
    return FailureCurationErrorCode.WRITE_FAILED


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
    resolved = _resolve_root(root)
    if not _is_prepared_root(resolved):
        _invalid_workspace("workspace_root")
    return resolved


def _resolve_root(root: Path) -> Path:
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError):
        _invalid_workspace("workspace_root")


def _is_prepared_root(root: Path) -> bool:
    return all((root / name).is_file() for name in _WORKSPACE_MARKERS)


def _workspace_artifact_paths(root: Path, directory_name: str) -> tuple[Path, Path]:
    workspace = root / _WORKSPACE_DIRECTORY
    failures = workspace / directory_name
    _require_contained(root, workspace.resolve(strict=False))
    _require_contained(root, failures.resolve(strict=False))
    _require_directory_if_present(workspace)
    return workspace, failures


def _store_artifact(root: Path, directory_name: str, artifact_id: str, content: bytes) -> Path:
    prepared_root = _prepared_root(root)
    workspace, artifacts = _workspace_artifact_paths(prepared_root, directory_name)
    _validate_artifact_size(content)
    identity = _prepare_workspace_directories(prepared_root, workspace, artifacts)
    path = artifacts / f"{artifact_id}.json"
    with _artifact_directory_handle(prepared_root, directory_name, identity) as directory:
        _publish_or_validate(directory, path.name, content, artifact_id)
    return path.relative_to(prepared_root)


def _require_directory_if_present(path: Path) -> None:
    if path.exists() and not path.is_dir():
        _invalid_workspace(_WORKSPACE_DIRECTORY)


def _require_contained(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        _invalid_workspace(_WORKSPACE_DIRECTORY)


def _invalid_workspace(subject: str) -> Never:
    raise FailureWorkspaceFailure(
        FailureWorkspaceErrorCode.INVALID_WORKSPACE,
        subject,
    ) from None


def _validate_artifact_size(content: bytes) -> None:
    if len(content) > _ARTIFACT_LIMIT_BYTES:
        raise FailureWorkspaceFailure(
            FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE,
            str(_ARTIFACT_LIMIT_BYTES),
        )


def _prepare_workspace_directories(
    root: Path,
    workspace: Path,
    failures: Path,
) -> _DirectoryChainIdentity:
    failures.mkdir(parents=True, exist_ok=True)
    _require_contained(root, workspace.resolve(strict=True))
    _require_contained(root, failures.resolve(strict=True))
    with _directory_handle(workspace) as directory:
        _require_directory_identity(directory, workspace)
        _write_ignore_file(directory)
    return _DirectoryChainIdentity(
        root=_path_identity(root),
        workspace=_path_identity(workspace),
        artifacts=_path_identity(failures),
    )


def _existing_directory_identity(
    root: Path,
    workspace: Path,
    artifacts: Path,
) -> _DirectoryChainIdentity:
    _require_contained(root, workspace.resolve(strict=True))
    _require_contained(root, artifacts.resolve(strict=True))
    return _DirectoryChainIdentity(
        root=_path_identity(root),
        workspace=_path_identity(workspace),
        artifacts=_path_identity(artifacts),
    )


def _existing_workspace_directories(
    root: Path,
    workspace: Path,
    failures: Path,
) -> _DirectoryChainIdentity:
    if not workspace.is_dir() or not failures.is_dir():
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.ARTIFACT_NOT_FOUND,
            _FAILURE_DIRECTORY,
        )
    return _existing_directory_identity(root, workspace, failures)


def _write_ignore_file(directory: int) -> None:
    try:
        _write_new_file(directory, ".gitignore", _IGNORE_CONTENT.encode("utf-8"))
    except FileExistsError:
        return


def _publish_or_validate(
    directory: int,
    name: str,
    expected: bytes,
    artifact_id: str,
) -> None:
    try:
        publish_idempotent(
            directory,
            name,
            expected,
            maximum_bytes=_ARTIFACT_LIMIT_BYTES,
            subject=artifact_id,
        )
    except SafeFileFailure as error:
        if error.code is SafeFileErrorCode.CONFLICT:
            raise FailureWorkspaceFailure(
                FailureWorkspaceErrorCode.ARTIFACT_CONFLICT,
                artifact_id,
            ) from None
        if error.code is SafeFileErrorCode.TOO_LARGE:
            raise FailureWorkspaceFailure(
                FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE,
                artifact_id,
            ) from None
        raise OSError("unsafe failure artifact") from None


def _write_new_file(directory: int, name: str, content: bytes) -> None:
    descriptor = os.open(name, _CREATE_FILE_FLAGS, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_artifact(directory: int, artifact_id: str) -> FailureDiagnosisRecord:
    _require_artifact_id(artifact_id)
    try:
        content = _read_existing(directory, f"{artifact_id}.json", artifact_id)
    except FileNotFoundError:
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.ARTIFACT_NOT_FOUND,
            artifact_id,
        ) from None
    except FailureWorkspaceFailure as error:
        raise _pattern_read_error(error) from None
    return _parse_artifact(content, artifact_id)


def _require_artifact_id(artifact_id: str) -> None:
    if _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.INVALID_ARTIFACT,
            "artifact_id",
        )


def _parse_artifact(content: bytes, artifact_id: str) -> FailureDiagnosisRecord:
    try:
        artifact = FailureArtifact.model_validate_json(content)
        diagnosis = artifact.to_diagnosis()
    except (FailureDiagnosisError, OutcomeEvaluationError, ValidationError):
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.INVALID_ARTIFACT,
            artifact_id,
        ) from None
    if artifact.artifact_id != artifact_id:
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.INVALID_ARTIFACT,
            artifact_id,
        )
    return FailureDiagnosisRecord(artifact_id, diagnosis)


def _pattern_read_error(error: FailureWorkspaceFailure) -> FailurePatternMiningError:
    if error.code is FailureWorkspaceErrorCode.INVALID_WORKSPACE:
        code = FailurePatternMiningErrorCode.INVALID_WORKSPACE
    elif error.code is FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE:
        code = FailurePatternMiningErrorCode.INVALID_ARTIFACT
    else:
        code = FailurePatternMiningErrorCode.READ_FAILED
    return FailurePatternMiningError(code, error.subject)


def _read_existing(directory: int, name: str, artifact_id: str) -> bytes:
    try:
        return read_bounded(
            directory,
            name,
            maximum_bytes=_ARTIFACT_LIMIT_BYTES,
            subject=artifact_id,
        )
    except SafeFileFailure as error:
        if error.code is SafeFileErrorCode.TOO_LARGE:
            raise FailureWorkspaceFailure(
                FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE,
                artifact_id,
            ) from None
        raise OSError("unsafe failure artifact") from None


@contextmanager
def _directory_handle(path: Path) -> Iterator[int]:
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _child_directory_handle(parent: int, name: str) -> Iterator[int]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _artifact_directory_handle(
    root: Path,
    directory_name: str,
    expected: _DirectoryChainIdentity,
) -> Iterator[int]:
    with (
        _directory_handle(root) as root_directory,
        _child_directory_handle(root_directory, _WORKSPACE_DIRECTORY) as workspace,
        _child_directory_handle(workspace, directory_name) as artifacts,
    ):
        _require_directory_chain(
            root,
            root_directory,
            workspace,
            directory_name,
            artifacts,
            expected,
        )
        yield artifacts
        _require_directory_chain(
            root,
            root_directory,
            workspace,
            directory_name,
            artifacts,
            expected,
        )


def _require_directory_chain(
    root: Path,
    root_directory: int,
    workspace: int,
    directory_name: str,
    artifacts: int,
    expected: _DirectoryChainIdentity,
) -> None:
    _require_directory_identity(root_directory, root, expected.root)
    _require_child_identity(root_directory, _WORKSPACE_DIRECTORY, workspace, expected.workspace)
    _require_child_identity(workspace, directory_name, artifacts, expected.artifacts)


def _require_directory_identity(
    descriptor: int,
    path: Path,
    expected: tuple[int, int] | None = None,
) -> None:
    opened = _descriptor_identity(descriptor)
    current = _path_identity(path)
    if opened != current or (expected is not None and opened != expected):
        raise OSError("workspace directory changed during failure recording")


def _require_child_identity(
    parent: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    opened = _descriptor_identity(descriptor)
    current = _stat_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    if opened != current or opened != expected:
        raise OSError("workspace directory changed during failure recording")


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    return _stat_identity(os.fstat(descriptor))


def _path_identity(path: Path) -> tuple[int, int]:
    return _stat_identity(os.stat(path, follow_symlinks=False))


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino
