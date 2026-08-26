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
    SEARCH_PRIOR_TRAJECTORIES = "search_prior_trajectories"
    READ_TRAJECTORY = "read_trajectory"
    VERIFY_ENVIRONMENT = "verify_environment"
    ADAPT = "adapt"
    RETURN_VERDICT = "return_verdict"


class ConstraintKind(StrEnum):
    TIME = "time"
    RESOURCE = "resource"
    NETWORK = "network"
    ACCESS = "access"
    POLICY = "policy"


class ToolAccess(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


class FailureBehaviorKind(StrEnum):
    OUTCOME_MISMATCH = "outcome_mismatch"
    FALSE_COMPLETION = "false_completion"
    REQUIRED_ACTION_OMITTED = "required_action_omitted"
    FORBIDDEN_STATE_CHANGE = "forbidden_state_change"
    UNRECOVERED_ACTION_FAILURE = "unrecovered_action_failure"
    NO_PROGRESS_LOOP = "no_progress_loop"
    ABANDONED_BEFORE_COMPLETION = "abandoned_before_completion"


class FailurePhase(StrEnum):
    ACTION = "action"
    RECOVERY = "recovery"
    COMPLETION = "completion"
    VERIFICATION = "verification"


class RecoveryStatus(StrEnum):
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"


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
class TaskId:
    value: str

    def __post_init__(self) -> None:
        _require_identifier(self.value)


@dataclass(frozen=True, slots=True)
class ToolName:
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
class RequiredOutcome:
    check_id: EnvironmentCheckId
    source_id: EnvironmentSourceId
    description: str

    def __post_init__(self) -> None:
        _require_text(self.description, "required outcome description")


@dataclass(frozen=True, slots=True)
class TaskConstraint:
    kind: ConstraintKind
    description: str

    def __post_init__(self) -> None:
        _require_text(self.description, "task constraint description")


@dataclass(frozen=True, slots=True)
class MiningTask:
    id: TaskId
    intent: str
    required_outcomes: tuple[RequiredOutcome, ...]
    constraints: tuple[TaskConstraint, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.intent, "task intent")
        if not self.required_outcomes:
            raise ValueError("mining task requires a required outcome")
        if len({item.check_id for item in self.required_outcomes}) != len(
            self.required_outcomes
        ):
            raise ValueError("required outcome ids must be unique")


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: ToolName
    access: ToolAccess


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
        if not self.evidence or any(
            item.kind is not EvidenceKind.PRODUCTION_SIGNAL for item in self.evidence
        ):
            raise ValueError("failure source requires production-signal evidence")


@dataclass(frozen=True, slots=True)
class EnvironmentSource:
    id: EnvironmentSourceId
    kind: EnvironmentSourceKind
    summary: str

    def __post_init__(self) -> None:
        _require_text(self.summary, "environment source summary")


@dataclass(frozen=True, slots=True)
class MiningNomination:
    trace_id: TraceId
    task: MiningTask
    sources: tuple[FailureSource, ...]
    environment_sources: tuple[EnvironmentSource, ...]
    available_tools: tuple[ToolCapability, ...] = ()
    initial_state_evidence: tuple[EvidenceReference, ...] = ()

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("mining nomination requires a failure source")
        if any(source.trace_id != self.trace_id for source in self.sources):
            raise ValueError("failure source trace does not match nomination")
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("failure source ids must be unique")
        source_ids = {source.id for source in self.environment_sources}
        if len(source_ids) != len(self.environment_sources):
            raise ValueError("environment source ids must be unique")
        if any(outcome.source_id not in source_ids for outcome in self.task.required_outcomes):
            raise ValueError("required outcome references an undeclared environment source")
        if len({tool.name for tool in self.available_tools}) != len(self.available_tools):
            raise ValueError("tool capabilities must be unique")
        if any(
            item.kind is not EvidenceKind.ENVIRONMENT
            for item in self.initial_state_evidence
        ):
            raise ValueError("initial state requires environment evidence")


@dataclass(frozen=True, slots=True)
class MiningContext:
    revision_id: HarnessRevisionId
    trace_id: TraceId
    trace_digest: Sha256Digest
    observation_ids: tuple[ObservationId, ...]
    session_id: str | None
    environment_name: str | None
    release: str | None
    available_tools: tuple[ToolCapability, ...]
    environment_sources: tuple[EnvironmentSource, ...]
    initial_state_evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if not self.observation_ids or len(set(self.observation_ids)) != len(
            self.observation_ids
        ):
            raise ValueError("mining context requires unique observation ids")


@dataclass(frozen=True, slots=True)
class BehaviorObservation:
    kind: FailureBehaviorKind
    phase: FailurePhase
    first_observation_id: ObservationId
    last_observation_id: ObservationId | None
    recovery_status: RecoveryStatus
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if not self.evidence or any(
            item.kind is not EvidenceKind.TRAJECTORY for item in self.evidence
        ):
            raise ValueError("failure behavior observation requires trajectory evidence")


@dataclass(frozen=True, slots=True)
class FailureBehavior:
    primary: FailureBehaviorKind
    summary: str
    observations: tuple[BehaviorObservation, ...]

    def __post_init__(self) -> None:
        _require_text(self.summary, "failure behavior summary")
        if not self.observations:
            raise ValueError("failure behavior requires observations")
        if not any(item.kind is self.primary for item in self.observations):
            raise ValueError("primary failure behavior must appear in observations")


@dataclass(frozen=True, slots=True)
class TraceMiningCase:
    task: MiningTask
    context: MiningContext
    sources: tuple[FailureSource, ...]

    @property
    def trace_id(self) -> TraceId:
        return self.context.trace_id


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
            if self.observed_state is not None or self.evidence:
                raise ValueError("unknown environment state cannot carry evidence")
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
    task: MiningTask
    context: MiningContext | None
    verdict: MiningVerdict
    source_ids: tuple[FailureSourceId, ...]
    completion_checks: tuple[CompletionCheck, ...]
    failure_behavior: FailureBehavior | None
    trajectory_evidence: tuple[EvidenceReference, ...]
    environment_evidence: tuple[EvidenceReference, ...]
    confidence: Confidence
    unresolved_questions: tuple[str, ...]
    invalid_reason: MiningInvalidReason | None

    def __post_init__(self) -> None:
        if self.verdict is MiningVerdict.INVALID:
            if self.invalid_reason is None or self.failure_behavior is not None:
                raise ValueError("invalid result requires a reason and no failure behavior")
            return
        if self.context is None or self.invalid_reason is not None:
            raise ValueError("valid result requires context and no invalid reason")
        if not self.completion_checks:
            raise ValueError("mining result requires completion checks")
        self._validate_checks()
        self._validate_behavior()
        if self.verdict is MiningVerdict.CONFIRMED_FAILURE:
            if (
                self.failure_behavior is None
                or not any(
                    check.status is CompletionStatus.NOT_COMPLETED
                    for check in self.completion_checks
                )
                or not self.trajectory_evidence
                or not self.environment_evidence
            ):
                raise ValueError(
                    "confirmed failure requires behavior, failed completion, trajectory, "
                    "and environment evidence"
                )
        elif self.verdict is MiningVerdict.NO_FAILURE:
            if (
                self.failure_behavior is not None
                or any(
                    check.status is not CompletionStatus.COMPLETED
                    for check in self.completion_checks
                )
                or not self.trajectory_evidence
                or not self.environment_evidence
            ):
                raise ValueError(
                    "no-failure verdict requires completed checks, no failure behavior, "
                    "and supporting evidence"
                )
        elif (
            self.failure_behavior is not None
            or not self.unresolved_questions
            and not any(
                check.status is CompletionStatus.UNKNOWN for check in self.completion_checks
            )
        ):
            raise ValueError("ambiguous verdict requires no behavior and an unresolved question")

    def _validate_checks(self) -> None:
        outcomes = {item.check_id: item for item in self.task.required_outcomes}
        if len(self.completion_checks) != len(outcomes):
            raise ValueError("completion checks must cover every required outcome")
        for check in self.completion_checks:
            outcome = outcomes.get(check.check_id)
            if outcome is None or check.required_outcome != outcome.description:
                raise ValueError("completion check does not match the task")

    def _validate_behavior(self) -> None:
        if self.context is None or self.failure_behavior is None:
            return
        ids = set(self.context.observation_ids)
        evidence = set(self.trajectory_evidence)
        for observation in self.failure_behavior.observations:
            if (
                observation.first_observation_id not in ids
                or observation.last_observation_id is not None
                and observation.last_observation_id not in ids
                or not set(observation.evidence).issubset(evidence)
            ):
                raise ValueError("failure behavior observation is outside the grounded context")


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


@dataclass(frozen=True, slots=True)
class AdaptationRequest:
    kinds: tuple[FailureSourceKind, ...]
    limit: int

    def __post_init__(self) -> None:
        if not self.kinds or len(set(self.kinds)) != len(self.kinds):
            raise ValueError("adaptation requires unique signal kinds")
        if not 1 <= self.limit <= 100:
            raise ValueError("adaptation limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class AdaptationResult:
    status: ToolStatus
    summary: str
    signals: tuple[FailureSource, ...]
    next_actions: tuple[ToolAction, ...]


class EnvironmentVerifier(Protocol):
    def verify(
        self,
        request: EnvironmentCheckRequest,
        source: EnvironmentSource,
        outcome: RequiredOutcome,
    ) -> EnvironmentVerification: ...


class FailureJudge(Protocol):
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
    production_signals: tuple[FailureSource, ...]
    _issued_evidence: list[EvidenceReference] = field(default_factory=list, init=False)
    _read_trajectory_evidence: list[EvidenceReference] = field(
        default_factory=list, init=False
    )

    @property
    def issued_evidence(self) -> tuple[EvidenceReference, ...]:
        return tuple(self._issued_evidence)

    @property
    def read_trajectory_evidence(self) -> tuple[EvidenceReference, ...]:
        return tuple(self._read_trajectory_evidence)

    def search_trajectory(self, request: TrajectorySearchRequest) -> TrajectorySearchResult:
        return self._search(request, self.case.trace_id, False)

    def search_prior_trajectories(
        self, request: TrajectorySearchRequest
    ) -> TrajectorySearchResult:
        return self._search(request, None, True)

    def _search(
        self,
        request: TrajectorySearchRequest,
        trace_id: TraceId | None,
        prior_only: bool,
    ) -> TrajectorySearchResult:
        store = CollectionStore(self.collection.store_path)
        try:
            limit = 100 if prior_only else request.limit
            hits = self._search_phrase(store, request, trace_id, request.text, limit)
            if not hits:
                found: list[ObservationContentHit] = []
                for token in _search_tokens(request.text):
                    for hit in self._search_phrase(store, request, trace_id, token, limit):
                        if hit not in found:
                            found.append(hit)
                    if len(found) >= limit:
                        break
                hits = tuple(found[:limit])
            hits = tuple(_focus_hit(store, self.collection, hit, request.text) for hit in hits)
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
        if prior_only:
            hits = tuple(hit for hit in hits if hit.trace_id != self.case.trace_id)[
                : request.limit
            ]
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

    def _search_phrase(
        self,
        store: CollectionStore,
        request: TrajectorySearchRequest,
        trace_id: TraceId | None,
        text: str,
        limit: int,
    ) -> tuple[ObservationContentHit, ...]:
        return store.search_content(
            self.collection.observation_sync_id,
            ObservationContentQuery(
                text=text,
                match=ObservationContentMatch.TOKEN_PHRASE,
                field=request.field,
                trace_id=trace_id,
                limit=limit,
                maximum_excerpt_characters=1000,
            ),
        )

    def read_trajectory(self, request: TrajectoryPageRequest) -> TrajectoryPageResult:
        store = CollectionStore(self.collection.store_path)
        try:
            # ponytail: collection scan is simplest for local v0; add a trace SQL query
            # if production profiles show this O(pages * records) path matters.
            observations = tuple(
                item
                for item in store.observations(self.collection.observation_sync_id)
                if item.trace_id == self.case.trace_id
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
                else (
                    ToolAction.SEARCH_PRIOR_TRAJECTORIES,
                    ToolAction.VERIFY_ENVIRONMENT,
                    ToolAction.ADAPT,
                    ToolAction.RETURN_VERDICT,
                )
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
        source, outcome = selected
        verification = self.environment.verify(request, source, outcome)
        self._issued_evidence.extend(verification.evidence)
        return EnvironmentCheckResult(
            ToolStatus.UNAVAILABLE
            if verification.status is CompletionStatus.UNKNOWN
            else ToolStatus.OK,
            "Environment state is unavailable."
            if verification.status is CompletionStatus.UNKNOWN
            else "Environment state verified.",
            verification,
            (ToolAction.ADAPT, ToolAction.RETURN_VERDICT),
            verification.evidence,
        )

    def adapt(self, request: AdaptationRequest) -> AdaptationResult:
        kinds = set(request.kinds)
        signals = tuple(
            signal for signal in self.production_signals if signal.kind in kinds
        )[: request.limit]
        return AdaptationResult(
            ToolStatus.OK if signals else ToolStatus.NOT_FOUND,
            f"Found {len(signals)} human or production calibration signals.",
            signals,
            (
                ToolAction.SEARCH_PRIOR_TRAJECTORIES
                if signals
                else ToolAction.RETURN_VERDICT,
            ),
        )


@dataclass(frozen=True, slots=True)
class Mine:
    revision: HarnessRevision
    collection: CollectionResult
    nominations: tuple[MiningNomination, ...]
    judge: FailureJudge
    environment: EnvironmentVerifier

    def __post_init__(self) -> None:
        if not self.nominations:
            raise ValueError("mine requires at least one nomination")

    def run(self) -> FailureMiningRun:
        signals = tuple(source for item in self.nominations for source in item.sources)
        results = tuple(self._mine(nomination, signals) for nomination in self.nominations)
        return FailureMiningRun(self.revision.id, self.collection.snapshot_digest, results)

    def _mine(
        self,
        nomination: MiningNomination,
        production_signals: tuple[FailureSource, ...],
    ) -> FailureMiningResult:
        trace = next(
            (item for item in self.collection.traces if item.id == nomination.trace_id),
            None,
        )
        if self.collection.revision_id != self.revision.id:
            return _invalid(nomination, MiningInvalidReason.REVISION_MISMATCH)
        if trace is None:
            return _invalid(nomination, MiningInvalidReason.TRACE_NOT_FOUND)
        if not _trace_is_complete(self.collection, trace):
            return _invalid(nomination, MiningInvalidReason.CORRUPT_TRACE)
        context = MiningContext(
            revision_id=self.revision.id,
            trace_id=trace.id,
            trace_digest=trace.digest,
            observation_ids=trace.observation_ids,
            session_id=trace.session_id,
            environment_name=trace.environment,
            release=trace.release,
            available_tools=nomination.available_tools,
            environment_sources=nomination.environment_sources,
            initial_state_evidence=nomination.initial_state_evidence,
        )
        case = TraceMiningCase(nomination.task, context, nomination.sources)
        tools = MiningTools(case, self.collection, self.environment, production_signals)
        result = self.judge.investigate(case, tools)
        if not _judge_result_matches(
            case,
            result,
            tools.issued_evidence,
            tools.read_trajectory_evidence,
        ):
            return _invalid(nomination, MiningInvalidReason.JUDGE_OUTPUT)
        return result


def _trace_is_complete(collection: CollectionResult, trace: TraceRecord) -> bool:
    if trace.attribution is not AttributionLevel.EXACT or trace.gaps:
        return False
    store = CollectionStore(collection.store_path)
    try:
        observations = tuple(
            item
            for item in store.observations(collection.observation_sync_id)
            if item.trace_id == trace.id
        )
        for observation in observations:
            _read_observation(store, collection, observation)
    except CollectionError:
        return False
    finally:
        store.close()
    return bool(observations) and tuple(item.id for item in observations) == trace.observation_ids


def _judge_result_matches(
    case: TraceMiningCase,
    result: FailureMiningResult,
    issued_evidence: tuple[EvidenceReference, ...],
    read_trajectory_evidence: tuple[EvidenceReference, ...],
) -> bool:
    if result.context is None:
        return False
    read_ids = {ObservationId(item.record_id.value) for item in read_trajectory_evidence}
    behavior_evidence = (
        ()
        if result.failure_behavior is None
        else tuple(
            evidence
            for observation in result.failure_behavior.observations
            for evidence in observation.evidence
        )
    )
    completion_evidence = tuple(
        evidence for check in result.completion_checks for evidence in check.evidence
    )
    return (
        result.task == case.task
        and result.context == case.context
        and result.source_ids == tuple(source.id for source in case.sources)
        and read_ids == set(case.context.observation_ids)
        and all(item.kind is EvidenceKind.TRAJECTORY for item in result.trajectory_evidence)
        and all(item in result.trajectory_evidence for item in read_trajectory_evidence)
        and all(item.kind is EvidenceKind.ENVIRONMENT for item in result.environment_evidence)
        and all(item.kind is EvidenceKind.ENVIRONMENT for item in completion_evidence)
        and all(item in issued_evidence for item in result.trajectory_evidence)
        and all(item in issued_evidence for item in result.environment_evidence)
        and all(item in issued_evidence for item in completion_evidence)
        and all(item in issued_evidence for item in behavior_evidence)
    )


def _find_environment_check(
    case: TraceMiningCase,
    request: EnvironmentCheckRequest,
) -> tuple[EnvironmentSource, RequiredOutcome] | None:
    source = next(
        (
            item
            for item in case.context.environment_sources
            if item.id == request.source_id
        ),
        None,
    )
    outcome = next(
        (
            item
            for item in case.task.required_outcomes
            if item.source_id == request.source_id and item.check_id == request.check_id
        ),
        None,
    )
    return None if source is None or outcome is None else (source, outcome)


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


def _search_tokens(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for word in text.split():
        token = "".join(character for character in word if character.isalnum() or character in "-_")
        if len(token) >= 3 and token.casefold() not in {item.casefold() for item in tokens}:
            tokens.append(token)
    return tuple(tokens)


def _focus_hit(
    store: CollectionStore,
    collection: CollectionResult,
    hit: ObservationContentHit,
    query: str,
) -> ObservationContentHit:
    content = store.read_content(collection.observation_sync_id, hit.reference).text
    lowered = content.casefold()
    positions = tuple(
        lowered.find(candidate.casefold())
        for candidate in (query, *_search_tokens(query))
    )
    position = next((item for item in positions if item >= 0), 0)
    start = max(0, position - 300)
    return ObservationContentHit(
        hit.observation_id,
        hit.trace_id,
        hit.field,
        hit.reference,
        content[start : start + 1000],
    )


def _invalid(
    nomination: MiningNomination,
    reason: MiningInvalidReason,
) -> FailureMiningResult:
    return FailureMiningResult(
        task=nomination.task,
        context=None,
        verdict=MiningVerdict.INVALID,
        source_ids=tuple(source.id for source in nomination.sources),
        completion_checks=(),
        failure_behavior=None,
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
