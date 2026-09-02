"""Bounded deterministic pattern mining over compact failure diagnoses."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ofw.evaluation.failure import FailureDiagnosis, FailureEvidenceStatus, FailureType

_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_PATTERN_ID_PATTERN = r"sha256:[0-9a-f]{64}"
_ABSOLUTE_PATH = re.compile(r"(?:/[\w.+-]+){2,}")
_OPAQUE_ID = re.compile(
    r"\b(?:(?:request|trace|span|observation|score|session|run)[_-]?id|rq)"
    r"[_:/-][A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_DIGEST = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")
_NORMALIZED_CAUSE_LIMIT = 200
_ARTIFACT_LIMIT = 50

ArtifactId = Annotated[str, Field(pattern=_ARTIFACT_ID_PATTERN)]
ArtifactIds = Annotated[
    tuple[ArtifactId, ...],
    Field(min_length=1, max_length=_ARTIFACT_LIMIT),
]
PatternId = Annotated[str, Field(pattern=_PATTERN_ID_PATTERN)]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailurePatternMiningStatus(StrEnum):
    SUCCESS = "success"


class FailurePatternOrdering(StrEnum):
    OCCURRENCES_TASKS_LATEST_FINGERPRINT = "occurrences_desc_tasks_desc_latest_desc_fingerprint_asc"


class FailurePatternMiningErrorCode(StrEnum):
    INVALID_WORKSPACE = "invalid_workspace"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    INVALID_ARTIFACT = "invalid_artifact"
    READ_FAILED = "read_failed"
    INVALID_READER_RESULT = "invalid_reader_result"


class FailurePatternMiningError(Exception):
    """Typed failure while reading or reducing compact diagnosis artifacts."""

    __slots__ = ("code", "subject")

    def __init__(self, code: FailurePatternMiningErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class MineFailurePatternsInput(StrictModel):
    workspace_root: Path = Field(strict=False)
    artifact_ids: ArtifactIds

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    @field_validator("artifact_ids")
    @classmethod
    def validate_artifact_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("artifact_ids must be unique")
        return values


class FailurePatternSummary(StrictModel):
    pattern_id: PatternId
    issue_type: FailureType
    normalized_root_cause: str = Field(min_length=1, max_length=_NORMALIZED_CAUSE_LIMIT)
    occurrence_count: int = Field(strict=True, ge=1, le=_ARTIFACT_LIMIT)
    task_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=_ARTIFACT_LIMIT)
    trace_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=_ARTIFACT_LIMIT)
    artifact_ids: ArtifactIds
    first_evaluated_at: datetime
    last_evaluated_at: datetime


class FailurePatternMiningObservation(StrictModel):
    status: FailurePatternMiningStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[PatternId, ...] = Field(max_length=_ARTIFACT_LIMIT)
    ordering: FailurePatternOrdering
    source_artifact_count: int = Field(strict=True, ge=1, le=_ARTIFACT_LIMIT)
    patterns: tuple[FailurePatternSummary, ...] = Field(max_length=_ARTIFACT_LIMIT)
    inconclusive_artifact_ids: tuple[ArtifactId, ...] = Field(max_length=_ARTIFACT_LIMIT)


class FailureArtifactReader(Protocol):
    def read(
        self,
        root: Path,
        artifact_ids: tuple[str, ...],
    ) -> tuple[FailureDiagnosisRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class FailureDiagnosisRecord:
    artifact_id: str
    diagnosis: FailureDiagnosis


@dataclass(slots=True)
class _PatternAccumulator:
    pattern_id: str
    issue_type: FailureType
    normalized_root_cause: str
    occurrences: list[FailureDiagnosisRecord]


@dataclass(frozen=True, slots=True)
class FailurePatternMiningService:
    reader: FailureArtifactReader

    def mine(self, request: MineFailurePatternsInput) -> FailurePatternMiningObservation:
        diagnoses = self.reader.read(request.workspace_root, request.artifact_ids)
        _validate_reader_result(request.artifact_ids, diagnoses)
        patterns, inconclusive = _mine(diagnoses)
        summaries = tuple(sorted((_summary(pattern) for pattern in patterns), key=_sort_key))
        return FailurePatternMiningObservation(
            status=FailurePatternMiningStatus.SUCCESS,
            summary=(
                f"Mined {len(summaries)} exact failure patterns from "
                f"{len(diagnoses)} diagnosis artifacts."
            ),
            next_actions=(
                "Use repeated supported patterns to prioritize a separate harness hypothesis.",
            ),
            artifacts=tuple(pattern.pattern_id for pattern in summaries),
            ordering=FailurePatternOrdering.OCCURRENCES_TASKS_LATEST_FINGERPRINT,
            source_artifact_count=len(diagnoses),
            patterns=summaries,
            inconclusive_artifact_ids=tuple(sorted(inconclusive)),
        )


def normalize_root_cause(value: str) -> str:
    """Mask volatile path, identifier, and number runs before exact grouping."""
    normalized = _ABSOLUTE_PATH.sub("<path>", value)
    normalized = _OPAQUE_ID.sub("<id>", normalized)
    normalized = _UUID.sub("<id>", normalized)
    normalized = _HEX_DIGEST.sub("<id>", normalized)
    normalized = _DIGIT_RUN.sub("<n>", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


def failure_pattern_id(issue_type: FailureType, root_cause: str) -> str:
    normalized = normalize_root_cause(root_cause)
    identity = "\0".join(("ofw.failure-pattern/1", issue_type.value, normalized))
    return f"sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _validate_reader_result(
    requested: tuple[str, ...],
    diagnoses: tuple[FailureDiagnosisRecord, ...],
) -> None:
    returned = tuple(diagnosis.artifact_id for diagnosis in diagnoses)
    if returned != requested:
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.INVALID_READER_RESULT,
            "artifact_ids",
        )


def _mine(
    occurrences: tuple[FailureDiagnosisRecord, ...],
) -> tuple[tuple[_PatternAccumulator, ...], tuple[str, ...]]:
    patterns: list[_PatternAccumulator] = []
    inconclusive: list[str] = []
    for occurrence in occurrences:
        diagnosis = occurrence.diagnosis
        if diagnosis.evidence_status is FailureEvidenceStatus.INCONCLUSIVE:
            inconclusive.append(occurrence.artifact_id)
            continue
        _add_pattern(patterns, occurrence)
    return tuple(patterns), tuple(inconclusive)


def _add_pattern(
    patterns: list[_PatternAccumulator],
    occurrence: FailureDiagnosisRecord,
) -> None:
    diagnosis = occurrence.diagnosis
    issue_type = diagnosis.issue_type
    root_cause = diagnosis.root_cause
    if issue_type is None or root_cause is None:
        raise FailurePatternMiningError(
            FailurePatternMiningErrorCode.INVALID_ARTIFACT,
            occurrence.artifact_id,
        )
    pattern_id = failure_pattern_id(issue_type, root_cause)
    for pattern in patterns:
        if pattern.pattern_id == pattern_id:
            pattern.occurrences.append(occurrence)
            return
    # ponytail: a linear scan is bounded at 50 artifacts; add an index only if that bound grows.
    patterns.append(
        _PatternAccumulator(
            pattern_id=pattern_id,
            issue_type=issue_type,
            normalized_root_cause=normalize_root_cause(root_cause),
            occurrences=[occurrence],
        )
    )


def _summary(pattern: _PatternAccumulator) -> FailurePatternSummary:
    occurrences = sorted(
        pattern.occurrences,
        key=_evaluated_at,
    )
    return FailurePatternSummary(
        pattern_id=pattern.pattern_id,
        issue_type=pattern.issue_type,
        normalized_root_cause=pattern.normalized_root_cause[:_NORMALIZED_CAUSE_LIMIT],
        occurrence_count=len(pattern.occurrences),
        task_ids=tuple(
            sorted({occurrence.diagnosis.outcome.task_id.value for occurrence in occurrences})
        ),
        trace_ids=tuple(
            sorted({occurrence.diagnosis.outcome.trace_id.value for occurrence in occurrences})
        ),
        artifact_ids=tuple(sorted(occurrence.artifact_id for occurrence in occurrences)),
        first_evaluated_at=occurrences[0].diagnosis.outcome.evaluated_at,
        last_evaluated_at=occurrences[-1].diagnosis.outcome.evaluated_at,
    )


def _evaluated_at(occurrence: FailureDiagnosisRecord) -> datetime:
    return occurrence.diagnosis.outcome.evaluated_at


def _sort_key(summary: FailurePatternSummary) -> tuple[int, int, float, str]:
    return (
        -summary.occurrence_count,
        -len(summary.task_ids),
        -summary.last_evaluated_at.timestamp(),
        summary.pattern_id,
    )
