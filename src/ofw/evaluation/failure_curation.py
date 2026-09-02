"""Typed, evidence-bound grouping of recorded failure diagnoses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ofw.contracts import ComponentKind, Sha256Digest
from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.outcome import TaskId
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId

_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_PATTERN_KEY_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_TEXT_PATTERN = r"[^\x00]+"

ArtifactIdentifier = Annotated[str, Field(pattern=_ARTIFACT_ID_PATTERN)]
DigestValue = Annotated[str, Field(pattern=r"sha256:[0-9a-f]{64}")]
PatternKey = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=_PATTERN_KEY_PATTERN),
]
TitleText = Annotated[str, Field(min_length=1, max_length=160, pattern=_TEXT_PATTERN)]
ExplanationText = Annotated[str, Field(min_length=1, max_length=1000, pattern=_TEXT_PATTERN)]
SourceArtifactIds = Annotated[
    tuple[ArtifactIdentifier, ...],
    Field(min_length=1, max_length=50),
]
GroupArtifactIds = Annotated[
    tuple[ArtifactIdentifier, ...],
    Field(min_length=2, max_length=20),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailureGroupInput(StrictModel):
    pattern_key: PatternKey
    title: TitleText
    mechanism: ExplanationText
    prevention: ExplanationText
    target_component: ComponentKind
    failure_artifact_ids: GroupArtifactIds

    @field_validator("title", "mechanism", "prevention")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized

    @field_validator("failure_artifact_ids")
    @classmethod
    def validate_unique_artifacts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("group failure artifacts must be unique")
        return values


class DeferredFailureInput(StrictModel):
    failure_artifact_id: ArtifactIdentifier
    reason: ExplanationText

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class RecordFailureCurationInput(StrictModel):
    workspace_root: Path = Field(strict=False)
    source_artifact_ids: SourceArtifactIds
    groups: tuple[FailureGroupInput, ...] = Field(max_length=25)
    deferred: tuple[DeferredFailureInput, ...] = Field(max_length=50)

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    @field_validator("source_artifact_ids")
    @classmethod
    def validate_unique_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("source failure artifacts must be unique")
        return values

    @model_validator(mode="after")
    def validate_partition(self) -> RecordFailureCurationInput:
        _require_unique_pattern_keys(self.groups)
        _require_complete_partition(
            self.source_artifact_ids,
            _assigned_artifact_ids(self.groups, self.deferred),
        )
        return self


@dataclass(frozen=True, slots=True)
class FailureSource:
    artifact_id: str
    artifact_digest: Sha256Digest
    trace_id: TraceId
    task_id: TaskId
    outcome_score_id: ScoreId
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None
    critical_observation_id: ObservationId | None


@dataclass(frozen=True, slots=True)
class FailureGroupMember:
    artifact_id: str
    artifact_digest: Sha256Digest
    trace_id: TraceId
    task_id: TaskId
    outcome_score_id: ScoreId
    critical_observation_id: ObservationId


@dataclass(frozen=True, slots=True)
class FailureGroup:
    id: str
    pattern_key: str
    title: str
    mechanism: str
    prevention: str
    target_component: ComponentKind
    issue_type: FailureType
    members: tuple[FailureGroupMember, ...]


@dataclass(frozen=True, slots=True)
class DeferredFailure:
    source: FailureSource
    reason: str


@dataclass(frozen=True, slots=True)
class FailureCuration:
    id: str
    source_artifact_ids: tuple[str, ...]
    groups: tuple[FailureGroup, ...]
    deferred: tuple[DeferredFailure, ...]


@dataclass(frozen=True, slots=True)
class FailureCurationReceipt:
    curation_id: str
    relative_path: Path


class FailureCurationErrorCode(StrEnum):
    INVALID_WORKSPACE = "invalid_workspace"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_INVALID = "source_invalid"
    UNSUPPORTED_SOURCE = "unsupported_source"
    MIXED_ISSUE_TYPES = "mixed_issue_types"
    INSUFFICIENT_RECURRENCE = "insufficient_recurrence"
    ARTIFACT_CONFLICT = "artifact_conflict"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    WRITE_FAILED = "write_failed"


class FailureCurationFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: FailureCurationErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class FailureCurationStatus(StrEnum):
    SUCCESS = "success"


class FailureCurationObservation(StrictModel):
    status: FailureCurationStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(min_length=2, max_length=2)
    curation_id: ArtifactIdentifier
    relative_path: Path
    source_failure_count: int = Field(strict=True, ge=1, le=50)
    group_count: int = Field(strict=True, ge=0, le=25)
    deferred_count: int = Field(strict=True, ge=0, le=50)


class FailureGroupMemberArtifact(StrictModel):
    artifact_id: ArtifactIdentifier
    artifact_digest: DigestValue
    trace_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    outcome_score_id: str = Field(min_length=1, max_length=256)
    critical_observation_id: str = Field(min_length=1, max_length=256)

    @classmethod
    def from_member(cls, member: FailureGroupMember) -> FailureGroupMemberArtifact:
        return cls(
            artifact_id=member.artifact_id,
            artifact_digest=member.artifact_digest.value,
            trace_id=member.trace_id.value,
            task_id=member.task_id.value,
            outcome_score_id=member.outcome_score_id.value,
            critical_observation_id=member.critical_observation_id.value,
        )


class FailureGroupArtifact(StrictModel):
    group_id: ArtifactIdentifier
    pattern_key: PatternKey
    title: TitleText
    mechanism: ExplanationText
    prevention: ExplanationText
    target_component: ComponentKind
    issue_type: FailureType
    members: tuple[FailureGroupMemberArtifact, ...] = Field(min_length=2, max_length=20)

    @classmethod
    def from_group(cls, group: FailureGroup) -> FailureGroupArtifact:
        return cls(
            group_id=group.id,
            pattern_key=group.pattern_key,
            title=group.title,
            mechanism=group.mechanism,
            prevention=group.prevention,
            target_component=group.target_component,
            issue_type=group.issue_type,
            members=tuple(FailureGroupMemberArtifact.from_member(item) for item in group.members),
        )


class FailureSourceArtifact(StrictModel):
    artifact_id: ArtifactIdentifier
    artifact_digest: DigestValue
    trace_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    outcome_score_id: str = Field(min_length=1, max_length=256)
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None
    critical_observation_id: str | None = Field(default=None, min_length=1, max_length=256)

    @classmethod
    def from_source(cls, source: FailureSource) -> FailureSourceArtifact:
        observation_id = source.critical_observation_id
        return cls(
            artifact_id=source.artifact_id,
            artifact_digest=source.artifact_digest.value,
            trace_id=source.trace_id.value,
            task_id=source.task_id.value,
            outcome_score_id=source.outcome_score_id.value,
            evidence_status=source.evidence_status,
            issue_type=source.issue_type,
            critical_observation_id=None if observation_id is None else observation_id.value,
        )


class DeferredFailureArtifact(StrictModel):
    source: FailureSourceArtifact
    reason: ExplanationText

    @classmethod
    def from_deferred(cls, deferred: DeferredFailure) -> DeferredFailureArtifact:
        return cls(
            source=FailureSourceArtifact.from_source(deferred.source),
            reason=deferred.reason,
        )


class FailureCurationArtifact(StrictModel):
    schema_version: Literal[1] = 1
    curation_id: ArtifactIdentifier
    source_artifact_ids: SourceArtifactIds
    groups: tuple[FailureGroupArtifact, ...] = Field(max_length=25)
    deferred: tuple[DeferredFailureArtifact, ...] = Field(max_length=50)

    @classmethod
    def from_curation(cls, curation: FailureCuration) -> FailureCurationArtifact:
        return cls(
            curation_id=curation.id,
            source_artifact_ids=curation.source_artifact_ids,
            groups=tuple(FailureGroupArtifact.from_group(group) for group in curation.groups),
            deferred=tuple(
                DeferredFailureArtifact.from_deferred(item) for item in curation.deferred
            ),
        )


class FailureCurationGateway(Protocol):
    def load(self, root: Path, artifact_ids: tuple[str, ...]) -> tuple[FailureSource, ...]: ...

    def store(self, root: Path, curation: FailureCuration) -> FailureCurationReceipt: ...


@dataclass(frozen=True, slots=True)
class FailureCurationService:
    gateway: FailureCurationGateway

    def record(self, request: RecordFailureCurationInput) -> FailureCurationObservation:
        curation = self.build(request)
        receipt = self.gateway.store(request.workspace_root, curation)
        return FailureCurationObservation(
            status=FailureCurationStatus.SUCCESS,
            summary=_summary(curation),
            next_actions=_next_actions(curation),
            artifacts=(str(receipt.relative_path), receipt.curation_id),
            curation_id=receipt.curation_id,
            relative_path=receipt.relative_path,
            source_failure_count=len(curation.source_artifact_ids),
            group_count=len(curation.groups),
            deferred_count=len(curation.deferred),
        )

    def build(self, request: RecordFailureCurationInput) -> FailureCuration:
        sources = self.gateway.load(request.workspace_root, request.source_artifact_ids)
        _validate_loaded_sources(request.source_artifact_ids, sources)
        groups = tuple(sorted((_group(item, sources) for item in request.groups), key=_group_key))
        deferred = tuple(
            sorted(
                (
                    DeferredFailure(
                        _find_source(item.failure_artifact_id, sources),
                        item.reason,
                    )
                    for item in request.deferred
                ),
                key=_deferred_key,
            )
        )
        source_ids = tuple(sorted(request.source_artifact_ids))
        return FailureCuration(
            id=_curation_id(source_ids, groups, deferred),
            source_artifact_ids=source_ids,
            groups=groups,
            deferred=deferred,
        )


def _validate_loaded_sources(
    requested: tuple[str, ...],
    sources: tuple[FailureSource, ...],
) -> None:
    loaded = tuple(source.artifact_id for source in sources)
    if len(set(loaded)) != len(loaded):
        raise FailureCurationFailure(FailureCurationErrorCode.SOURCE_INVALID, "source_artifacts")
    _reject_unrequested_sources(requested, loaded)
    _reject_missing_sources(requested, loaded)


def _group(item: FailureGroupInput, sources: tuple[FailureSource, ...]) -> FailureGroup:
    selected = tuple(
        _find_source(artifact_id, sources) for artifact_id in item.failure_artifact_ids
    )
    _require_supported_sources(selected)
    issue_type = _shared_issue_type(selected, item.pattern_key)
    _require_distinct_tasks(selected, item.pattern_key)
    members = tuple(sorted((_member(source) for source in selected), key=_member_key))
    return FailureGroup(
        id=_group_id(item, issue_type, members),
        pattern_key=item.pattern_key,
        title=item.title,
        mechanism=item.mechanism,
        prevention=item.prevention,
        target_component=item.target_component,
        issue_type=issue_type,
        members=members,
    )


def _require_supported_sources(selected: tuple[FailureSource, ...]) -> None:
    unsupported = next((source for source in selected if not _is_supported(source)), None)
    if unsupported is not None:
        raise FailureCurationFailure(
            FailureCurationErrorCode.UNSUPPORTED_SOURCE,
            unsupported.artifact_id,
        )


def _shared_issue_type(
    selected: tuple[FailureSource, ...],
    pattern_key: str,
) -> FailureType:
    issue_type = selected[0].issue_type
    if issue_type is None:
        raise FailureCurationFailure(
            FailureCurationErrorCode.UNSUPPORTED_SOURCE,
            selected[0].artifact_id,
        )
    if any(source.issue_type is not issue_type for source in selected[1:]):
        raise FailureCurationFailure(
            FailureCurationErrorCode.MIXED_ISSUE_TYPES,
            pattern_key,
        )
    return issue_type


def _require_distinct_tasks(selected: tuple[FailureSource, ...], pattern_key: str) -> None:
    if len({source.task_id for source in selected}) < 2:
        raise FailureCurationFailure(
            FailureCurationErrorCode.INSUFFICIENT_RECURRENCE,
            pattern_key,
        )


def _reject_unrequested_sources(requested: tuple[str, ...], loaded: tuple[str, ...]) -> None:
    unexpected = next((artifact_id for artifact_id in loaded if artifact_id not in requested), None)
    if unexpected is not None:
        raise FailureCurationFailure(FailureCurationErrorCode.SOURCE_INVALID, unexpected)


def _reject_missing_sources(requested: tuple[str, ...], loaded: tuple[str, ...]) -> None:
    missing = next((artifact_id for artifact_id in requested if artifact_id not in loaded), None)
    if missing is not None:
        raise FailureCurationFailure(FailureCurationErrorCode.SOURCE_NOT_FOUND, missing)


def _require_unique_pattern_keys(groups: tuple[FailureGroupInput, ...]) -> None:
    pattern_keys = tuple(group.pattern_key for group in groups)
    if len(set(pattern_keys)) != len(pattern_keys):
        raise ValueError("failure pattern keys must be unique")


def _assigned_artifact_ids(
    groups: tuple[FailureGroupInput, ...],
    deferred: tuple[DeferredFailureInput, ...],
) -> tuple[str, ...]:
    grouped = tuple(artifact_id for group in groups for artifact_id in group.failure_artifact_ids)
    return (*grouped, *(item.failure_artifact_id for item in deferred))


def _require_complete_partition(sources: tuple[str, ...], assigned: tuple[str, ...]) -> None:
    if len(set(assigned)) != len(assigned):
        raise ValueError("each source failure must be assigned once")
    if set(assigned) != set(sources):
        raise ValueError("curation must partition every source failure")


def _find_source(artifact_id: str, sources: tuple[FailureSource, ...]) -> FailureSource:
    # ponytail: linear lookup is bounded at 50; add an index only if that limit grows.
    source = next((item for item in sources if item.artifact_id == artifact_id), None)
    if source is None:
        raise FailureCurationFailure(FailureCurationErrorCode.SOURCE_NOT_FOUND, artifact_id)
    return source


def _is_supported(source: FailureSource) -> bool:
    return (
        source.evidence_status is FailureEvidenceStatus.SUPPORTED
        and source.issue_type is not None
        and source.critical_observation_id is not None
    )


def _member(source: FailureSource) -> FailureGroupMember:
    observation_id = source.critical_observation_id
    if observation_id is None:
        raise FailureCurationFailure(
            FailureCurationErrorCode.UNSUPPORTED_SOURCE,
            source.artifact_id,
        )
    return FailureGroupMember(
        artifact_id=source.artifact_id,
        artifact_digest=source.artifact_digest,
        trace_id=source.trace_id,
        task_id=source.task_id,
        outcome_score_id=source.outcome_score_id,
        critical_observation_id=observation_id,
    )


def _group_id(
    item: FailureGroupInput,
    issue_type: FailureType,
    members: tuple[FailureGroupMember, ...],
) -> str:
    identity = "\0".join(
        (
            "ofw.failure-group",
            item.pattern_key,
            item.title,
            item.mechanism,
            item.prevention,
            item.target_component.value,
            issue_type.value,
            *(f"{member.artifact_id}:{member.artifact_digest.value}" for member in members),
        )
    )
    return str(uuid5(NAMESPACE_URL, identity))


def _curation_id(
    source_ids: tuple[str, ...],
    groups: tuple[FailureGroup, ...],
    deferred: tuple[DeferredFailure, ...],
) -> str:
    identity = "\0".join(
        (
            "ofw.failure-curation",
            *source_ids,
            *(group.id for group in groups),
            *(
                f"{item.source.artifact_id}:{item.source.artifact_digest.value}:{item.reason}"
                for item in deferred
            ),
        )
    )
    return str(uuid5(NAMESPACE_URL, identity))


def _member_key(member: FailureGroupMember) -> tuple[str, str, str]:
    return member.task_id.value, member.trace_id.value, member.artifact_id


def _group_key(group: FailureGroup) -> tuple[str, str]:
    return group.pattern_key, group.id


def _deferred_key(item: DeferredFailure) -> str:
    return item.source.artifact_id


def _summary(curation: FailureCuration) -> str:
    group_word = "group" if len(curation.groups) == 1 else "groups"
    deferred_word = "failure" if len(curation.deferred) == 1 else "failures"
    return (
        f"Stored {_quantity(len(curation.groups))} evidence-bound failure {group_word} "
        f"and {_quantity(len(curation.deferred))} deferred {deferred_word}."
    )


def _next_actions(curation: FailureCuration) -> tuple[str, ...]:
    if curation.groups:
        return ("Use one recorded group to form the next harness hypothesis.",)
    return ("Do not form a harness hypothesis until a repeated supported pattern exists.",)


def _quantity(value: int) -> str:
    return "one" if value == 1 else str(value)
