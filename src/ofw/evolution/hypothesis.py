"""Immutable evidence-backed hypothesis contract and recording service."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ofw.contracts import ComponentKind
from ofw.evaluation.failure_curation import FailureCurationArtifact, FailureGroupArtifact
from ofw.evaluation.failure_patterns import (
    FailurePatternMiningError,
    FailurePatternMiningObservation,
    FailurePatternMiningService,
    MineFailurePatternsInput,
)
from ofw.preparation.contracts import contained_relative_path
from ofw.preparation.policy import (
    ExperimentPolicyErrorCode,
    ExperimentPolicyFailure,
    ExperimentPolicySnapshot,
)

_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_PATTERN_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
_HYPOTHESIS_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_EXPERIMENT_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_TASK_ID_PATTERN = r"^[^\x00]+$"
HypothesisTextValue = Annotated[str, Field(min_length=1, max_length=4000)]
ArtifactIdsValue = Annotated[
    tuple[Annotated[str, Field(pattern=_ARTIFACT_ID_PATTERN)], ...],
    Field(strict=False, min_length=1, max_length=50),
]
RelativePathsValue = Annotated[
    tuple[Annotated[Path, Field(strict=False)], ...],
    Field(strict=False, min_length=1, max_length=50),
]
TaskIdsValue = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=256, pattern=_TASK_ID_PATTERN)], ...],
    Field(strict=False, min_length=1, max_length=50),
]
RiskTaskIdsValue = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=256, pattern=_TASK_ID_PATTERN)], ...],
    Field(strict=False, max_length=50),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FailurePatternReferenceInput(StrictModel):
    pattern_id: str = Field(pattern=_PATTERN_ID_PATTERN)
    diagnosis_artifact_ids: ArtifactIdsValue

    @field_validator("diagnosis_artifact_ids")
    @classmethod
    def validate_unique_artifacts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("diagnosis_artifact_ids must be unique")
        return values


class HarnessChangeTargetInput(StrictModel):
    component_kind: Annotated[ComponentKind, Field(strict=False)]
    relative_paths: RelativePathsValue

    @field_validator("relative_paths")
    @classmethod
    def validate_paths(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        normalized = tuple(contained_relative_path(value, "relative_paths") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("relative_paths must be unique")
        return normalized


class RecordHypothesisInput(StrictModel):
    workspace_root: Path = Field(strict=False)
    experiment_id: str = Field(min_length=1, max_length=80, pattern=_EXPERIMENT_PATTERN)
    source_commit: str = Field(pattern=_COMMIT_PATTERN)
    curation_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    curation_group_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    predicted_task_ids: TaskIdsValue
    at_risk_task_ids: RiskTaskIdsValue = ()
    patterns: tuple[FailurePatternReferenceInput, ...] = Field(
        strict=False,
        min_length=1,
        max_length=50,
    )
    statement: HypothesisTextValue
    rationale: HypothesisTextValue
    target: HarnessChangeTargetInput
    expected_effect: HypothesisTextValue
    regression_risks: tuple[HypothesisTextValue, ...] = Field(
        strict=False,
        max_length=10,
    )

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    @field_validator("statement", "rationale", "expected_effect")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalized_text(value)

    @field_validator("regression_risks")
    @classmethod
    def validate_risks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized_text(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("regression_risks must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_evidence_bound(self) -> RecordHypothesisInput:
        pattern_ids = tuple(pattern.pattern_id for pattern in self.patterns)
        artifact_ids = tuple(
            artifact_id
            for pattern in self.patterns
            for artifact_id in pattern.diagnosis_artifact_ids
        )
        _require_unique_patterns(pattern_ids)
        _require_bounded_unique_artifacts(artifact_ids)
        _require_disjoint_task_predictions(self.predicted_task_ids, self.at_risk_task_ids)
        return self


@dataclass(frozen=True, slots=True)
class HypothesisId:
    value: str

    def __post_init__(self) -> None:
        if _HYPOTHESIS_ID_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid hypothesis id")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class FailurePatternReference:
    pattern_id: str
    diagnosis_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarnessChangeTarget:
    component_kind: ComponentKind
    relative_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class HarnessHypothesis:
    id: HypothesisId
    experiment_id: str
    source_commit: str
    curation_id: str
    curation_group_id: str
    predicted_task_ids: tuple[str, ...]
    at_risk_task_ids: tuple[str, ...]
    patterns: tuple[FailurePatternReference, ...]
    statement: str
    rationale: str
    target: HarnessChangeTarget
    expected_effect: str
    regression_risks: tuple[str, ...]


class _HypothesisContent(StrictModel):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=1, max_length=80, pattern=_EXPERIMENT_PATTERN)
    source_commit: str = Field(pattern=_COMMIT_PATTERN)
    curation_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    curation_group_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    predicted_task_ids: TaskIdsValue
    at_risk_task_ids: RiskTaskIdsValue = ()
    patterns: tuple[FailurePatternReferenceInput, ...] = Field(min_length=1, max_length=50)
    statement: HypothesisTextValue
    rationale: HypothesisTextValue
    target: HarnessChangeTargetInput
    expected_effect: HypothesisTextValue
    regression_risks: tuple[HypothesisTextValue, ...] = Field(max_length=10)


class HypothesisArtifact(_HypothesisContent):
    hypothesis_id: str = Field(pattern=_PATTERN_ID_PATTERN)

    @classmethod
    def from_hypothesis(cls, hypothesis: HarnessHypothesis) -> HypothesisArtifact:
        return cls(
            hypothesis_id=hypothesis.id.value,
            experiment_id=hypothesis.experiment_id,
            source_commit=hypothesis.source_commit,
            curation_id=hypothesis.curation_id,
            curation_group_id=hypothesis.curation_group_id,
            predicted_task_ids=hypothesis.predicted_task_ids,
            at_risk_task_ids=hypothesis.at_risk_task_ids,
            patterns=tuple(
                FailurePatternReferenceInput(
                    pattern_id=pattern.pattern_id,
                    diagnosis_artifact_ids=pattern.diagnosis_artifact_ids,
                )
                for pattern in hypothesis.patterns
            ),
            statement=hypothesis.statement,
            rationale=hypothesis.rationale,
            target=HarnessChangeTargetInput(
                component_kind=hypothesis.target.component_kind,
                relative_paths=hypothesis.target.relative_paths,
            ),
            expected_effect=hypothesis.expected_effect,
            regression_risks=hypothesis.regression_risks,
        )


class HypothesisStatus(StrEnum):
    SUCCESS = "success"


class HypothesisObservation(StrictModel):
    status: HypothesisStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(min_length=2, max_length=2)
    hypothesis_id: str = Field(pattern=_PATTERN_ID_PATTERN)
    experiment_id: str = Field(min_length=1, max_length=80, pattern=_EXPERIMENT_PATTERN)
    source_commit: str = Field(pattern=_COMMIT_PATTERN)
    curation_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    curation_group_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    predicted_task_ids: TaskIdsValue
    at_risk_task_ids: RiskTaskIdsValue = ()
    relative_path: Path
    pattern_count: int = Field(strict=True, ge=1, le=50)
    diagnosis_count: int = Field(strict=True, ge=1, le=50)
    target_paths: RelativePathsValue


class HypothesisErrorCode(StrEnum):
    POLICY_SNAPSHOT_REQUIRED = "policy_snapshot_required"
    POLICY_INVALID = "policy_invalid"
    STALE_POLICY = "stale_policy"
    STALE_COMMIT = "stale_commit"
    DIRTY_WORKSPACE = "dirty_workspace"
    CURATION_NOT_FOUND = "curation_not_found"
    CURATION_INVALID = "curation_invalid"
    CURATION_GROUP_NOT_FOUND = "curation_group_not_found"
    CURATION_EVIDENCE_MISMATCH = "curation_evidence_mismatch"
    PATTERN_EVIDENCE_MISMATCH = "pattern_evidence_mismatch"
    INCONCLUSIVE_EVIDENCE = "inconclusive_evidence"
    TARGET_NOT_EDITABLE = "target_not_editable"
    INVALID_TARGET = "invalid_target"
    HYPOTHESIS_CONFLICT = "hypothesis_conflict"
    HYPOTHESIS_TOO_LARGE = "hypothesis_too_large"
    WRITE_FAILED = "write_failed"


class HypothesisFailure(Exception):
    """Typed sanitized hypothesis rejection."""

    __slots__ = ("code", "subject")

    def __init__(self, code: HypothesisErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class HypothesisGateway(Protocol):
    def load_policy(self, root: Path, experiment_id: str) -> ExperimentPolicySnapshot: ...

    def load_curation(self, root: Path, curation_id: str) -> FailureCurationArtifact: ...

    def validate_workspace(
        self,
        root: Path,
        policy: ExperimentPolicySnapshot,
        source_commit: str,
        paths: tuple[Path, ...],
    ) -> None: ...

    def store(self, root: Path, hypothesis: HarnessHypothesis) -> Path: ...


@dataclass(frozen=True, slots=True)
class HypothesisService:
    pattern_miner: FailurePatternMiningService
    repository: HypothesisGateway

    def record(self, request: RecordHypothesisInput) -> HypothesisObservation:
        policy = _load_policy(self.repository, request)
        _validate_policy(request, policy)
        paths = tuple(sorted(request.target.relative_paths, key=Path.as_posix))
        _validate_editable_paths(paths, policy.editable_paths)
        self.repository.validate_workspace(
            request.workspace_root,
            policy,
            request.source_commit,
            paths,
        )
        group = _load_curation_group(self.repository, request)
        patterns = _canonical_patterns(request.patterns)
        _validate_curated_evidence(request, group, patterns)
        mined = _mine_patterns(self.pattern_miner, request.workspace_root, patterns)
        _validate_mined_patterns(patterns, mined)
        hypothesis = _hypothesis(request, patterns, paths)
        relative_path = self.repository.store(request.workspace_root, hypothesis)
        return _observation(hypothesis, relative_path)


def _load_policy(
    repository: HypothesisGateway,
    request: RecordHypothesisInput,
) -> ExperimentPolicySnapshot:
    try:
        return repository.load_policy(request.workspace_root, request.experiment_id)
    except ExperimentPolicyFailure as error:
        code = (
            HypothesisErrorCode.POLICY_SNAPSHOT_REQUIRED
            if error.code is ExperimentPolicyErrorCode.POLICY_SNAPSHOT_REQUIRED
            else HypothesisErrorCode.POLICY_INVALID
        )
        raise HypothesisFailure(code, request.experiment_id) from None


def _load_curation_group(
    repository: HypothesisGateway,
    request: RecordHypothesisInput,
) -> FailureGroupArtifact:
    curation = repository.load_curation(request.workspace_root, request.curation_id)
    group = next(
        (item for item in curation.groups if item.group_id == request.curation_group_id),
        None,
    )
    if group is None:
        raise HypothesisFailure(
            HypothesisErrorCode.CURATION_GROUP_NOT_FOUND,
            request.curation_group_id,
        )
    return group


def _validate_policy(
    request: RecordHypothesisInput,
    policy: ExperimentPolicySnapshot,
) -> None:
    if request.source_commit != policy.initialization_commit:
        raise HypothesisFailure(HypothesisErrorCode.STALE_COMMIT, request.experiment_id)


def _validate_editable_paths(paths: tuple[Path, ...], editable: tuple[Path, ...]) -> None:
    for path in paths:
        if path not in editable:
            raise HypothesisFailure(HypothesisErrorCode.TARGET_NOT_EDITABLE, path.as_posix())


def _canonical_patterns(
    patterns: tuple[FailurePatternReferenceInput, ...],
) -> tuple[FailurePatternReference, ...]:
    return tuple(
        FailurePatternReference(
            pattern.pattern_id,
            tuple(sorted(pattern.diagnosis_artifact_ids)),
        )
        for pattern in sorted(patterns, key=_pattern_reference_id)
    )


def _validate_curated_evidence(
    request: RecordHypothesisInput,
    group: FailureGroupArtifact,
    patterns: tuple[FailurePatternReference, ...],
) -> None:
    if _declared_artifact_ids(patterns) != _curated_artifact_ids(group):
        raise HypothesisFailure(
            HypothesisErrorCode.CURATION_EVIDENCE_MISMATCH,
            request.curation_group_id,
        )
    if request.target.component_kind is not group.target_component:
        raise HypothesisFailure(
            HypothesisErrorCode.CURATION_EVIDENCE_MISMATCH,
            request.curation_group_id,
        )


def _declared_artifact_ids(
    patterns: tuple[FailurePatternReference, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            artifact_id
            for pattern in patterns
            for artifact_id in pattern.diagnosis_artifact_ids
        )
    )


def _curated_artifact_ids(group: FailureGroupArtifact) -> tuple[str, ...]:
    return tuple(sorted(member.artifact_id for member in group.members))


def _require_disjoint_task_predictions(
    predicted: tuple[str, ...],
    at_risk: tuple[str, ...],
) -> None:
    if set(predicted) & set(at_risk):
        raise ValueError("predicted_task_ids and at_risk_task_ids must be disjoint")


def _pattern_reference_id(pattern: FailurePatternReferenceInput) -> str:
    return pattern.pattern_id


def _mine_patterns(
    service: FailurePatternMiningService,
    root: Path,
    patterns: tuple[FailurePatternReference, ...],
) -> FailurePatternMiningObservation:
    artifact_ids = tuple(
        sorted(
            artifact_id
            for pattern in patterns
            for artifact_id in pattern.diagnosis_artifact_ids
        )
    )
    try:
        request = MineFailurePatternsInput(workspace_root=root, artifact_ids=artifact_ids)
        return service.mine(request)
    except FailurePatternMiningError as error:
        raise HypothesisFailure(
            HypothesisErrorCode.PATTERN_EVIDENCE_MISMATCH,
            error.subject,
        ) from None


def _validate_mined_patterns(
    declared: tuple[FailurePatternReference, ...],
    mined: FailurePatternMiningObservation,
) -> None:
    if mined.inconclusive_artifact_ids:
        raise HypothesisFailure(
            HypothesisErrorCode.INCONCLUSIVE_EVIDENCE,
            mined.inconclusive_artifact_ids[0],
        )
    if len(declared) != len(mined.patterns):
        raise HypothesisFailure(HypothesisErrorCode.PATTERN_EVIDENCE_MISMATCH, "patterns")
    for reference in declared:
        _validate_pattern_reference(reference, mined)


def _validate_pattern_reference(
    reference: FailurePatternReference,
    mined: FailurePatternMiningObservation,
) -> None:
    matching = tuple(item for item in mined.patterns if item.pattern_id == reference.pattern_id)
    if len(matching) != 1 or matching[0].artifact_ids != reference.diagnosis_artifact_ids:
        raise HypothesisFailure(
            HypothesisErrorCode.PATTERN_EVIDENCE_MISMATCH,
            reference.pattern_id,
        )


def _hypothesis(
    request: RecordHypothesisInput,
    patterns: tuple[FailurePatternReference, ...],
    paths: tuple[Path, ...],
) -> HarnessHypothesis:
    target = HarnessChangeTarget(request.target.component_kind, paths)
    risks = tuple(sorted(request.regression_risks))
    content = _content(request, patterns, target, risks)
    hypothesis_id = HypothesisId(
        f"sha256:{hashlib.sha256(content.model_dump_json().encode('utf-8')).hexdigest()}"
    )
    return HarnessHypothesis(
        hypothesis_id,
        request.experiment_id,
        request.source_commit,
        request.curation_id,
        request.curation_group_id,
        request.predicted_task_ids,
        request.at_risk_task_ids,
        patterns,
        request.statement,
        request.rationale,
        target,
        request.expected_effect,
        risks,
    )


def _content(
    request: RecordHypothesisInput,
    patterns: tuple[FailurePatternReference, ...],
    target: HarnessChangeTarget,
    risks: tuple[str, ...],
) -> _HypothesisContent:
    return _HypothesisContent(
        experiment_id=request.experiment_id,
        source_commit=request.source_commit,
        curation_id=request.curation_id,
        curation_group_id=request.curation_group_id,
        predicted_task_ids=request.predicted_task_ids,
        at_risk_task_ids=request.at_risk_task_ids,
        patterns=tuple(
            FailurePatternReferenceInput(
                pattern_id=pattern.pattern_id,
                diagnosis_artifact_ids=pattern.diagnosis_artifact_ids,
            )
            for pattern in patterns
        ),
        statement=request.statement,
        rationale=request.rationale,
        target=HarnessChangeTargetInput(
            component_kind=target.component_kind,
            relative_paths=target.relative_paths,
        ),
        expected_effect=request.expected_effect,
        regression_risks=risks,
    )


def _observation(hypothesis: HarnessHypothesis, relative_path: Path) -> HypothesisObservation:
    diagnosis_count = sum(len(pattern.diagnosis_artifact_ids) for pattern in hypothesis.patterns)
    return HypothesisObservation(
        status=HypothesisStatus.SUCCESS,
        summary="Recorded one evidence-backed hypothesis for the prepared experiment.",
        next_actions=("Stop before candidate editing and retain this hypothesis receipt.",),
        artifacts=(str(relative_path), hypothesis.id.value),
        hypothesis_id=hypothesis.id.value,
        experiment_id=hypothesis.experiment_id,
        source_commit=hypothesis.source_commit,
        curation_id=hypothesis.curation_id,
        curation_group_id=hypothesis.curation_group_id,
        predicted_task_ids=hypothesis.predicted_task_ids,
        at_risk_task_ids=hypothesis.at_risk_task_ids,
        relative_path=relative_path,
        pattern_count=len(hypothesis.patterns),
        diagnosis_count=diagnosis_count,
        target_paths=hypothesis.target.relative_paths,
    )


def _require_unique_patterns(pattern_ids: tuple[str, ...]) -> None:
    if len(set(pattern_ids)) != len(pattern_ids):
        raise ValueError("pattern_ids must be unique")


def _require_bounded_unique_artifacts(artifact_ids: tuple[str, ...]) -> None:
    if len(artifact_ids) > 50 or len(set(artifact_ids)) != len(artifact_ids):
        raise ValueError("diagnosis artifacts must be globally unique and bounded")


def _normalized_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("hypothesis text must not be blank")
    return normalized
