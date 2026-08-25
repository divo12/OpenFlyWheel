"""Evidence-backed failure mining over full Langfuse trajectories."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.observability.langfuse.contracts import CollectionError
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionResult,
    ObservationContent,
    ObservationContentField,
    ObservationContentHit,
    ObservationContentMatch,
    ObservationContentQuery,
    ObservationId,
    ObservationRecord,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.store import CollectionStore


class FailureSourceKind(StrEnum):
    HUMAN_FEEDBACK = "human_feedback"
    USER_CORRECTION = "user_correction"
    TRUSTED_SCORE = "trusted_score"
    DOWNSTREAM_FAILURE = "downstream_failure"
    INCIDENT = "incident"
    ROLLBACK = "rollback"
    REOPENED_WORK = "reopened_work"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    AGENT_ERROR = "agent_error"


class EnvironmentSourceKind(StrEnum):
    RECORDED_STATE = "recorded_state"
    AUDIT_LOG = "audit_log"
    PRODUCTION_API = "production_api"
    DETERMINISTIC_CHECK = "deterministic_check"


class EvidenceKind(StrEnum):
    TRAJECTORY = "trajectory"
    ENVIRONMENT = "environment"
    PRODUCTION_SIGNAL = "production_signal"


class CompletionStatus(StrEnum):
    COMPLETED = "completed"
    NOT_COMPLETED = "not_completed"
    UNKNOWN = "unknown"


class MiningVerdict(StrEnum):
    CONFIRMED_FAILURE = "confirmed_failure"
    NO_FAILURE = "no_failure"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class MiningInvalidReason(StrEnum):
    REVISION_MISMATCH = "revision_mismatch"
    TRACE_NOT_FOUND = "trace_not_found"
    CORRUPT_TRACE = "corrupt_trace"
    JUDGE_OUTPUT = "judge_output"


class ToolStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    ERROR = "error"


class ToolAction(StrEnum):
    SEARCH_TRAJECTORY = "search_trajectory"
    READ_TRAJECTORY = "read_trajectory"
    VERIFY_ENVIRONMENT = "verify_environment"
    RETURN_VERDICT = "return_verdict"


@dataclass(frozen=True, slots=True)
class FailureSourceId:
    value: str

    def __post_init__(self) -> None:
        _require_identifier(self.value)


@dataclass(frozen=True, slots=True)
class EnvironmentSourceId:
    value: str

    def __post_init__(self) -> None:
        _require_identifier(self.value)


@dataclass(frozen=True, slots=True)
class EnvironmentCheckId:
    value: str

    def __post_init__(self) -> None:
        _require_identifier(self.value)


@dataclass(frozen=True, slots=True)
class EvidenceRecordId:
    value: str

    def __post_init__(self) -> None:
        _require_identifier(self.value)


@dataclass(frozen=True, slots=True)
class Confidence:
    value: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or not 0.0 <= self.value <= 1.0:
            raise ValueError("confidence must be finite and between zero and one")


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    kind: EvidenceKind
    record_id: EvidenceRecordId
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class FailureSource:
    id: FailureSourceId
    kind: FailureSourceKind
    trace_id: TraceId
    observed_at: datetime
    summary: str
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        _require_text(self.summary, "failure source summary")
        if not self.evidence:
            raise ValueError("failure source requires evidence")


@dataclass(frozen=True, slots=True)
class EnvironmentCheck:
    id: EnvironmentCheckId
    required_outcome: str

    def __post_init__(self) -> None:
        _require_text(self.required_outcome, "required outcome")


@dataclass(frozen=True, slots=True)
class EnvironmentSource:
    id: EnvironmentSourceId
    kind: EnvironmentSourceKind
    summary: str
    checks: tuple[EnvironmentCheck, ...]

    def __post_init__(self) -> None:
        _require_text(self.summary, "environment source summary")
        if not self.checks or len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("environment source requires unique checks")


@dataclass(frozen=True, slots=True)
class MiningNomination:
    trace_id: TraceId
    user_job: str
    sources: tuple[FailureSource, ...]
    environment_sources: tuple[EnvironmentSource, ...]

    def __post_init__(self) -> None:
        _require_text(self.user_job, "user job")
        if not self.sources:
            raise ValueError("mining nomination requires a failure source")
        if any(source.trace_id != self.trace_id for source in self.sources):
            raise ValueError("failure source trace does not match nomination")
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("failure source ids must be unique")
        if len({source.id for source in self.environment_sources}) != len(
            self.environment_sources
        ):
            raise ValueError("environment source ids must be unique")


@dataclass(frozen=True, slots=True)
class TraceMiningCase:
    revision_id: HarnessRevisionId
    trace_id: TraceId
    trace_digest: Sha256Digest
    observation_ids: tuple[ObservationId, ...]
    user_job: str
    sources: tuple[FailureSource, ...]
    environment_sources: tuple[EnvironmentSource, ...]


@dataclass(frozen=True, slots=True)
class EnvironmentCheckRequest:
    source_id: EnvironmentSourceId
    check_id: EnvironmentCheckId


@dataclass(frozen=True, slots=True)
class EnvironmentVerification:
    status: CompletionStatus
    observed_state: str | None
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if self.status is CompletionStatus.UNKNOWN:
            return
        if self.observed_state is None or not self.observed_state.strip() or not self.evidence:
            raise ValueError("known environment state requires observation and evidence")
        if any(item.kind is not EvidenceKind.ENVIRONMENT for item in self.evidence):
            raise ValueError("environment verification requires environment evidence")


@dataclass(frozen=True, slots=True)
class CompletionCheck:
    check_id: EnvironmentCheckId
    required_outcome: str
    agent_claim: str | None
    observed_state: str | None
    status: CompletionStatus
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class FailureMiningResult:
    revision_id: HarnessRevisionId
    trace_id: TraceId
    trace_digest: Sha256Digest | None
    verdict: MiningVerdict
    user_job: str
    source_ids: tuple[FailureSourceId, ...]
    completion_checks: tuple[CompletionCheck, ...]
    trajectory_evidence: tuple[EvidenceReference, ...]
    environment_evidence: tuple[EvidenceReference, ...]
    confidence: Confidence
    unresolved_questions: tuple[str, ...]
    invalid_reason: MiningInvalidReason | None

    def __post_init__(self) -> None:
        if self.verdict is MiningVerdict.INVALID:
            if self.invalid_reason is None:
                raise ValueError("invalid result requires a reason")
            return
        if self.invalid_reason is not None or self.trace_digest is None:
            raise ValueError("valid result cannot carry an invalid reason")
        if not self.completion_checks:
            raise ValueError("mining result requires completion checks")
        if self.verdict is MiningVerdict.CONFIRMED_FAILURE:
            if (
                not any(
                    check.status is CompletionStatus.NOT_COMPLETED
                    for check in self.completion_checks
                )
                or not self.trajectory_evidence
                or not self.environment_evidence
            ):
                raise ValueError(
                    "confirmed failure requires failed completion, trajectory, "
                    "and environment evidence"
                )
        elif self.verdict is MiningVerdict.NO_FAILURE:
            if (
                any(
                    check.status is not CompletionStatus.COMPLETED
                    for check in self.completion_checks
                )
                or not self.trajectory_evidence
                or not self.environment_evidence
            ):
                raise ValueError(
                    "no-failure verdict requires completed checks and supporting evidence"
                )
        elif not self.unresolved_questions and not any(
            check.status is CompletionStatus.UNKNOWN for check in self.completion_checks
        ):
            raise ValueError("ambiguous verdict requires an unresolved question")


@dataclass(frozen=True, slots=True)
class FailureMiningRun:
    revision_id: HarnessRevisionId
    collection_digest: Sha256Digest
    results: tuple[FailureMiningResult, ...]


@dataclass(frozen=True, slots=True)
class TrajectorySearchRequest:
    text: str
    field: ObservationContentField
    limit: int

    def __post_init__(self) -> None:
        _require_text(self.text, "trajectory search text")
        if not 1 <= self.limit <= 100:
            raise ValueError("trajectory search limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class TrajectorySearchResult:
    status: ToolStatus
    summary: str
    hits: tuple[ObservationContentHit, ...]
    next_actions: tuple[ToolAction, ...]
    artifacts: tuple[EvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class TrajectoryPageRequest:
    cursor: ObservationId | None
    limit: int

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 100:
            raise ValueError("trajectory page limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class TrajectoryObservation:
    record: ObservationRecord
    input_content: ObservationContent | None
    output_content: ObservationContent | None


@dataclass(frozen=True, slots=True)
class TrajectoryPageResult:
    status: ToolStatus
    summary: str
    observations: tuple[TrajectoryObservation, ...]
    next_cursor: ObservationId | None
    next_actions: tuple[ToolAction, ...]
    artifacts: tuple[EvidenceReference, ...]


@dataclass(frozen=True, slots=True)
class EnvironmentCheckResult:
    status: ToolStatus
    summary: str
    verification: EnvironmentVerification | None
    next_actions: tuple[ToolAction, ...]
    artifacts: tuple[EvidenceReference, ...]


class EnvironmentVerifier(Protocol):
    def verify(
        self,
        request: EnvironmentCheckRequest,
        source: EnvironmentSource,
        check: EnvironmentCheck,
    ) -> EnvironmentVerification: ...


class HermesJudge(Protocol):
    def investigate(
        self,
        case: TraceMiningCase,
        tools: MiningTools,
    ) -> FailureMiningResult: ...


@dataclass(slots=True)
class MiningTools:
    case: TraceMiningCase
    collection: CollectionResult
    environment: EnvironmentVerifier
    _issued_evidence: list[EvidenceReference] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _read_trajectory_evidence: list[EvidenceReference] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    @property
    def issued_evidence(self) -> tuple[EvidenceReference, ...]:
        return tuple(self._issued_evidence)

    @property
    def read_trajectory_evidence(self) -> tuple[EvidenceReference, ...]:
        return tuple(self._read_trajectory_evidence)

    def search_trajectory(self, request: TrajectorySearchRequest) -> TrajectorySearchResult:
        store = CollectionStore(self.collection.store_path)
        try:
            hits = store.search_content(
                self.collection.observation_sync_id,
                ObservationContentQuery(
                    text=request.text,
                    match=ObservationContentMatch.TOKEN_PHRASE,
                    field=request.field,
                    trace_id=self.case.trace_id,
                    limit=request.limit,
                    maximum_excerpt_characters=1000,
                ),
            )
        except CollectionError:
            return TrajectorySearchResult(
                ToolStatus.ERROR,
                "Trajectory search failed.",
                (),
                (ToolAction.RETURN_VERDICT,),
                (),
            )
        finally:
            store.close()
        artifacts = tuple(
            EvidenceReference(
                EvidenceKind.TRAJECTORY,
                EvidenceRecordId(hit.observation_id.value),
                hit.reference.digest,
            )
            for hit in hits
        )
        self._issued_evidence.extend(artifacts)
        return TrajectorySearchResult(
            ToolStatus.OK if hits else ToolStatus.NOT_FOUND,
            f"Found {len(hits)} matching trajectory segments.",
            hits,
            (ToolAction.READ_TRAJECTORY,),
            artifacts,
        )

    def read_trajectory(self, request: TrajectoryPageRequest) -> TrajectoryPageResult:
        store = CollectionStore(self.collection.store_path)
        try:
            # ponytail: collection-wide scan is simplest for local v0; add a paged
            # trace SQL query if production profiles show this O(pages * records) path.
            observations = tuple(
                observation
                for observation in store.observations(self.collection.observation_sync_id)
                if observation.trace_id == self.case.trace_id
            )
            start = _page_start(observations, request.cursor)
            if start is None:
                return TrajectoryPageResult(
                    ToolStatus.BLOCKED,
                    "Cursor is outside the nominated trace.",
                    (),
                    None,
                    (ToolAction.RETURN_VERDICT,),
                    (),
                )
            selected = observations[start : start + request.limit]
            views = tuple(_read_observation(store, self.collection, item) for item in selected)
        except CollectionError:
            return TrajectoryPageResult(
                ToolStatus.ERROR,
                "Trajectory content could not be read.",
                (),
                None,
                (ToolAction.RETURN_VERDICT,),
                (),
            )
        finally:
            store.close()
        has_more = start + len(selected) < len(observations)
        next_cursor = observations[start + len(selected)].id if has_more else None
        artifacts = tuple(
            EvidenceReference(
                EvidenceKind.TRAJECTORY,
                EvidenceRecordId(observation.record.id.value),
                observation.record.digest,
            )
            for observation in views
        )
        self._issued_evidence.extend(artifacts)
        self._read_trajectory_evidence.extend(artifacts)
        return TrajectoryPageResult(
            ToolStatus.OK,
            f"Read {len(views)} ordered trajectory observations.",
            views,
            next_cursor,
            (
                (ToolAction.READ_TRAJECTORY,)
                if next_cursor is not None
                else (ToolAction.VERIFY_ENVIRONMENT, ToolAction.RETURN_VERDICT)
            ),
            artifacts,
        )

    def verify_environment(self, request: EnvironmentCheckRequest) -> EnvironmentCheckResult:
        selected = _find_environment_check(self.case, request)
        if selected is None:
            return EnvironmentCheckResult(
                ToolStatus.BLOCKED,
                "Environment check is not declared for this mining case.",
                None,
                (ToolAction.RETURN_VERDICT,),
                (),
            )
        source, check = selected
        verification = self.environment.verify(request, source, check)
        self._issued_evidence.extend(verification.evidence)
        return EnvironmentCheckResult(
            (
                ToolStatus.UNAVAILABLE
                if verification.status is CompletionStatus.UNKNOWN
                else ToolStatus.OK
            ),
            "Environment state is unavailable."
            if verification.status is CompletionStatus.UNKNOWN
            else "Environment state verified.",
            verification,
            (ToolAction.RETURN_VERDICT,),
            verification.evidence,
        )


@dataclass(frozen=True, slots=True)
class Mine:
    revision: HarnessRevision
    collection: CollectionResult
    nominations: tuple[MiningNomination, ...]
    judge: HermesJudge
    environment: EnvironmentVerifier

    def __post_init__(self) -> None:
        if not self.nominations:
            raise ValueError("mine requires at least one nomination")

    def run(self) -> FailureMiningRun:
        results = tuple(self._mine(nomination) for nomination in self.nominations)
        return FailureMiningRun(self.revision.id, self.collection.snapshot_digest, results)

    def _mine(self, nomination: MiningNomination) -> FailureMiningResult:
        trace = next(
            (item for item in self.collection.traces if item.id == nomination.trace_id),
            None,
        )
        if self.collection.revision_id != self.revision.id:
            return _invalid(
                self.revision.id,
                nomination,
                trace,
                MiningInvalidReason.REVISION_MISMATCH,
            )
        if trace is None:
            return _invalid(self.revision.id, nomination, None, MiningInvalidReason.TRACE_NOT_FOUND)
        if not _trace_is_complete(self.collection, trace):
            return _invalid(self.revision.id, nomination, trace, MiningInvalidReason.CORRUPT_TRACE)
        case = TraceMiningCase(
            self.revision.id,
            trace.id,
            trace.digest,
            trace.observation_ids,
            nomination.user_job,
            nomination.sources,
            nomination.environment_sources,
        )
        tools = MiningTools(case, self.collection, self.environment)
        result = self.judge.investigate(case, tools)
        if not _judge_result_matches(
            case,
            result,
            tools.issued_evidence,
            tools.read_trajectory_evidence,
        ):
            return _invalid(self.revision.id, nomination, trace, MiningInvalidReason.JUDGE_OUTPUT)
        return result


def _trace_is_complete(collection: CollectionResult, trace: TraceRecord) -> bool:
    if trace.attribution is not AttributionLevel.EXACT or trace.gaps:
        return False
    store = CollectionStore(collection.store_path)
    try:
        observations = tuple(
            observation
            for observation in store.observations(collection.observation_sync_id)
            if observation.trace_id == trace.id
        )
        for observation in observations:
            _read_observation(store, collection, observation)
    except CollectionError:
        return False
    finally:
        store.close()
    return (
        bool(observations)
        and tuple(observation.id for observation in observations) == trace.observation_ids
    )


def _judge_result_matches(
    case: TraceMiningCase,
    result: FailureMiningResult,
    issued_evidence: tuple[EvidenceReference, ...],
    read_trajectory_evidence: tuple[EvidenceReference, ...],
) -> bool:
    observation_ids = {evidence.record_id.value for evidence in result.trajectory_evidence}
    read_observation_ids = {
        ObservationId(evidence.record_id.value) for evidence in read_trajectory_evidence
    }
    allowed_checks = {
        check.id
        for source in case.environment_sources
        for check in source.checks
    }
    completion_evidence = tuple(
        evidence for check in result.completion_checks for evidence in check.evidence
    )
    return (
        result.revision_id == case.revision_id
        and result.trace_id == case.trace_id
        and result.trace_digest == case.trace_digest
        and result.source_ids == tuple(source.id for source in case.sources)
        and read_observation_ids == set(case.observation_ids)
        and all(item.kind is EvidenceKind.TRAJECTORY for item in result.trajectory_evidence)
        and all(item in result.trajectory_evidence for item in read_trajectory_evidence)
        and observation_ids.issubset({item.value for item in case.observation_ids})
        and all(item.kind is EvidenceKind.ENVIRONMENT for item in result.environment_evidence)
        and all(item.kind is EvidenceKind.ENVIRONMENT for item in completion_evidence)
        and all(item in issued_evidence for item in result.trajectory_evidence)
        and all(item in issued_evidence for item in result.environment_evidence)
        and all(item in issued_evidence for item in completion_evidence)
        and all(check.check_id in allowed_checks for check in result.completion_checks)
    )


def _find_environment_check(
    case: TraceMiningCase,
    request: EnvironmentCheckRequest,
) -> tuple[EnvironmentSource, EnvironmentCheck] | None:
    for source in case.environment_sources:
        if source.id != request.source_id:
            continue
        for check in source.checks:
            if check.id == request.check_id:
                return source, check
    return None


def _page_start(
    observations: tuple[ObservationRecord, ...],
    cursor: ObservationId | None,
) -> int | None:
    if cursor is None:
        return 0
    for index, observation in enumerate(observations):
        if observation.id == cursor:
            return index
    return None


def _read_observation(
    store: CollectionStore,
    collection: CollectionResult,
    observation: ObservationRecord,
) -> TrajectoryObservation:
    input_content = (
        None
        if observation.input_content is None
        else store.read_content(collection.observation_sync_id, observation.input_content)
    )
    output_content = (
        None
        if observation.output_content is None
        else store.read_content(collection.observation_sync_id, observation.output_content)
    )
    return TrajectoryObservation(observation, input_content, output_content)


def _invalid(
    revision_id: HarnessRevisionId,
    nomination: MiningNomination,
    trace: TraceRecord | None,
    reason: MiningInvalidReason,
) -> FailureMiningResult:
    return FailureMiningResult(
        revision_id=revision_id,
        trace_id=nomination.trace_id,
        trace_digest=None if trace is None else trace.digest,
        verdict=MiningVerdict.INVALID,
        user_job=nomination.user_job,
        source_ids=tuple(source.id for source in nomination.sources),
        completion_checks=(),
        trajectory_evidence=(),
        environment_evidence=(),
        confidence=Confidence(1.0),
        unresolved_questions=(),
        invalid_reason=reason,
    )


def _require_identifier(value: str) -> None:
    if not value or not value.isascii() or any(character.isspace() for character in value):
        raise ValueError("identifier must be non-empty ASCII without whitespace")


def _require_text(value: str, name: str) -> None:
    if not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be non-empty text")
