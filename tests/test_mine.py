"""Failure mining over complete Langfuse trajectories and verified final state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ofw.contracts import (
    GitCommit,
    HarnessRevision,
    HarnessRevisionId,
    HarnessSchemaVersion,
    RepositorySnapshot,
    Sha256Digest,
)
from ofw.mine import (
    AdaptationRequest,
    BehaviorObservation,
    CompletionCheck,
    CompletionStatus,
    Confidence,
    ConstraintKind,
    EnvironmentCheckId,
    EnvironmentCheckRequest,
    EnvironmentSource,
    EnvironmentSourceId,
    EnvironmentSourceKind,
    EnvironmentVerification,
    EvidenceKind,
    EvidenceRecordId,
    EvidenceReference,
    FailureBehavior,
    FailureBehaviorKind,
    FailureMiningResult,
    FailurePhase,
    FailureSource,
    FailureSourceId,
    FailureSourceKind,
    Mine,
    MiningContext,
    MiningInvalidReason,
    MiningNomination,
    MiningTask,
    MiningTools,
    MiningVerdict,
    RecoveryStatus,
    RequiredOutcome,
    TaskConstraint,
    TaskId,
    ToolAccess,
    ToolCapability,
    ToolName,
    ToolStatus,
    TraceMiningCase,
    TrajectoryPageRequest,
    TrajectorySearchRequest,
)
from ofw.observability.langfuse.contracts import LangfuseConnectionId, TraceWindow
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapabilityReason,
    CollectionResult,
    CollectionSyncId,
    JsonDocument,
    ObservationContent,
    ObservationContentField,
    ObservationContentReference,
    ObservationId,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    ProjectId,
    ScorePage,
    TraceGap,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.store import CollectionStore

NOW = datetime(2026, 8, 25, tzinfo=UTC)
TRACE_ID = TraceId("trace-1")
CHECK_ID = EnvironmentCheckId("ticket-closed")
SOURCE_ID = EnvironmentSourceId("itsm-production")


def _digest(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{value}")


def _revision(tmp_path: Path, value: str = "revision-1") -> HarnessRevision:
    return HarnessRevision(
        schema_version=HarnessSchemaVersion.V1,
        id=HarnessRevisionId(value),
        harness_name="support-agent",
        root=tmp_path,
        repository=RepositorySnapshot(GitCommit("abc123"), False, None),
        components=(),
        observability=None,
        runtime=None,
        canary_digest=None,
    )


def _observation(
    revision: HarnessRevision,
    index: int,
    name: str,
    output: str,
    *,
    level: str | None = None,
) -> tuple[ObservationRecord, ObservationContent]:
    reference = ObservationContentReference.for_text(output)
    observation_id = ObservationId(f"observation-{index}")
    return (
        ObservationRecord(
            id=observation_id,
            trace_id=TRACE_ID,
            start_time=NOW + timedelta(seconds=index),
            end_time=NOW + timedelta(seconds=index, milliseconds=100),
            project_id=ProjectId("project-1"),
            parent_observation_id=None if index == 0 else ObservationId("observation-0"),
            type=ObservationType.AGENT if index in (0, 3) else ObservationType.TOOL,
            is_root=index == 0,
            name=name,
            level=None,
            version="v1",
            environment="production",
            user_id="user-1",
            session_id="session-1",
            created_at=NOW + timedelta(seconds=index),
            updated_at=NOW + timedelta(seconds=index, milliseconds=100),
            metadata=JsonDocument(f'{{"revision":"{revision.id}"}}'),
            usage=None,
            costs=None,
            total_cost=None,
            tags=(),
            release=str(revision.id),
            trace_name="close-ticket",
            raw=JsonDocument(f'{{"name":"{name}","output":"{output}"}}'),
            digest=_digest(f"observation-{index}"),
            status_message=level,
            output_content=reference,
        ),
        ObservationContent(reference, output),
    )


def _collection(
    tmp_path: Path,
    revision: HarnessRevision,
    outputs: tuple[str, ...],
    *,
    attribution: AttributionLevel = AttributionLevel.EXACT,
    gaps: tuple[TraceGap, ...] = (),
) -> CollectionResult:
    named = tuple(
        _observation(
            revision,
            index,
            "agent" if index in (0, len(outputs) - 1) else "update-ticket",
            output,
            level="failed" if "failed" in output else None,
        )
        for index, output in enumerate(outputs)
    )
    observations = tuple(item[0] for item in named)
    contents = tuple(item[1] for item in named)
    observation_sync_id = CollectionSyncId("observations-mine")
    score_sync_id = CollectionSyncId("scores-mine")
    store_path = tmp_path / "collection.sqlite"
    store = CollectionStore(store_path)
    try:
        store.commit_observation_page(
            "connection-1",
            observation_sync_id,
            ObservationPage(observations, None, contents),
        )
        store.commit_score_page("connection-1", score_sync_id, ScorePage((), None))
    finally:
        store.close()
    trace = TraceRecord(
        id=TRACE_ID,
        observation_ids=tuple(observation.id for observation in observations),
        root_observation_ids=(observations[0].id,),
        score_ids=(),
        session_id="session-1",
        environment="production",
        release=str(revision.id),
        attribution=attribution,
        gaps=gaps,
        digest=_digest("trace-1"),
    )
    return CollectionResult(
        revision_id=revision.id,
        connection_id=LangfuseConnectionId("connection-1"),
        window=TraceWindow(NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)),
        observation_sync_id=observation_sync_id,
        score_sync_id=score_sync_id,
        traces=(trace,),
        observation_count=len(observations),
        score_count=0,
        gap_count=len(gaps),
        snapshot_digest=_digest("collection"),
        capability=(
            CollectionCapabilityReason.READY
            if attribution is AttributionLevel.EXACT and not gaps
            else CollectionCapabilityReason.INCOMPLETE_TRACE
        ),
        store_path=store_path,
    )


def _evidence(kind: EvidenceKind, record_id: str, digest: str) -> EvidenceReference:
    return EvidenceReference(kind, EvidenceRecordId(record_id), _digest(digest))


def _nomination(kind: FailureSourceKind) -> MiningNomination:
    source = FailureSource(
        id=FailureSourceId("signal-1"),
        kind=kind,
        trace_id=TRACE_ID,
        observed_at=NOW,
        summary="The ticket may still be open.",
        evidence=(_evidence(EvidenceKind.PRODUCTION_SIGNAL, "signal-1", "signal-1"),),
    )
    environment = EnvironmentSource(
        id=SOURCE_ID,
        kind=EnvironmentSourceKind.PRODUCTION_API,
        summary="Read-only ITSM production state.",
    )
    return MiningNomination(
        trace_id=TRACE_ID,
        task=_task(),
        sources=(source,),
        environment_sources=(environment,),
        available_tools=(
            ToolCapability(ToolName("update-ticket"), ToolAccess.MUTATING),
        ),
    )


def _task() -> MiningTask:
    return MiningTask(
        id=TaskId("close-ticket"),
        intent="Close the customer ticket.",
        required_outcomes=(
            RequiredOutcome(
                check_id=CHECK_ID,
                source_id=SOURCE_ID,
                description="The ticket is closed.",
            ),
        ),
        constraints=(
            TaskConstraint(
                kind=ConstraintKind.POLICY,
                description="Do not claim completion without verifying the ticket state.",
            ),
        ),
    )


def _context(observation_ids: tuple[ObservationId, ...]) -> MiningContext:
    return MiningContext(
        revision_id=HarnessRevisionId("revision-1"),
        trace_id=TRACE_ID,
        trace_digest=_digest("trace-1"),
        observation_ids=observation_ids,
        session_id="session-1",
        environment_name="production",
        release="revision-1",
        available_tools=(
            ToolCapability(ToolName("search_trajectory"), ToolAccess.READ_ONLY),
            ToolCapability(ToolName("read_trajectory"), ToolAccess.READ_ONLY),
            ToolCapability(ToolName("verify_environment"), ToolAccess.READ_ONLY),
        ),
        environment_sources=(
            EnvironmentSource(
                id=SOURCE_ID,
                kind=EnvironmentSourceKind.PRODUCTION_API,
                summary="Read-only ITSM production state.",
            ),
        ),
        initial_state_evidence=(),
    )


@dataclass(frozen=True, slots=True)
class RecordedEnvironmentVerifier:
    status: CompletionStatus
    observed_state: str | None

    def verify(
        self,
        request: EnvironmentCheckRequest,
        source: EnvironmentSource,
        outcome: RequiredOutcome,
    ) -> EnvironmentVerification:
        assert request.source_id == source.id
        assert request.check_id == outcome.check_id
        evidence = (
            ()
            if self.status is CompletionStatus.UNKNOWN
            else (_evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),)
        )
        return EnvironmentVerification(
            status=self.status,
            observed_state=self.observed_state,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class FakeFailureJudge:
    search_text: str

    def investigate(
        self,
        case: TraceMiningCase,
        tools: MiningTools,
    ) -> FailureMiningResult:
        assert case == tools.case
        search = tools.search_trajectory(
            TrajectorySearchRequest(
                text=self.search_text,
                field=ObservationContentField.ANY,
                limit=10,
            )
        )
        assert search.status is ToolStatus.OK
        focused = tools.read_trajectory(
            TrajectoryPageRequest(cursor=search.hits[0].observation_id, limit=1)
        )
        assert focused.observations[0].record.id == search.hits[0].observation_id

        trajectory_evidence: tuple[EvidenceReference, ...] = ()
        cursor: ObservationId | None = None
        while True:
            page = tools.read_trajectory(TrajectoryPageRequest(cursor=cursor, limit=2))
            assert page.status is ToolStatus.OK
            trajectory_evidence = (*trajectory_evidence, *page.artifacts)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        verification = tools.verify_environment(
            EnvironmentCheckRequest(SOURCE_ID, CHECK_ID)
        )
        unresolved: tuple[str, ...]
        if verification.status is ToolStatus.UNAVAILABLE:
            completion = CompletionStatus.UNKNOWN
            verdict = MiningVerdict.AMBIGUOUS
            unresolved = ("Production state was unavailable.",)
            environment_evidence: tuple[EvidenceReference, ...] = ()
            observed_state = None
        else:
            assert verification.verification is not None
            completion = verification.verification.status
            verdict = (
                MiningVerdict.CONFIRMED_FAILURE
                if completion is CompletionStatus.NOT_COMPLETED
                else MiningVerdict.NO_FAILURE
            )
            unresolved = ()
            environment_evidence = verification.artifacts
            observed_state = verification.verification.observed_state

        adapted = tools.adapt(
            AdaptationRequest(
                (
                    FailureSourceKind.HUMAN_FEEDBACK,
                    FailureSourceKind.USER_CORRECTION,
                    FailureSourceKind.DOWNSTREAM_FAILURE,
                    FailureSourceKind.AGENT_ERROR,
                ),
                10,
            )
        )
        assert adapted.status is ToolStatus.OK

        behavior_evidence = next(
            item
            for item in trajectory_evidence
            if item.record_id.value == search.hits[0].observation_id.value
        )

        failure_behavior = (
            None
            if verdict is not MiningVerdict.CONFIRMED_FAILURE
            else FailureBehavior(
                primary=(
                    FailureBehaviorKind.FALSE_COMPLETION
                    if "successfully" in search.hits[0].excerpt.lower()
                    else FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE
                ),
                summary=(
                    "The agent claimed success while the ticket stayed open."
                    if "successfully" in search.hits[0].excerpt.lower()
                    else "The tool failure was not recovered before completion."
                ),
                observations=(
                    BehaviorObservation(
                        kind=(
                            FailureBehaviorKind.FALSE_COMPLETION
                            if "successfully" in search.hits[0].excerpt.lower()
                            else FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE
                        ),
                        phase=(
                            FailurePhase.COMPLETION
                            if "successfully" in search.hits[0].excerpt.lower()
                            else FailurePhase.RECOVERY
                        ),
                        first_observation_id=search.hits[0].observation_id,
                        last_observation_id=None,
                        recovery_status=RecoveryStatus.NOT_RECOVERED,
                        evidence=(behavior_evidence,),
                    ),
                ),
            )
        )

        return FailureMiningResult(
            task=tools.case.task,
            context=tools.case.context,
            verdict=verdict,
            source_ids=tuple(source.id for source in tools.case.sources),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Ticket closed successfully.",
                    observed_state=observed_state,
                    status=completion,
                    evidence=environment_evidence,
                ),
            ),
            failure_behavior=failure_behavior,
            trajectory_evidence=trajectory_evidence,
            environment_evidence=environment_evidence,
            confidence=Confidence(0.95 if completion is not CompletionStatus.UNKNOWN else 0.4),
            unresolved_questions=unresolved,
            invalid_reason=None,
        )


@dataclass(frozen=True, slots=True)
class ForgingFailureJudge:
    delegate: FakeFailureJudge

    def investigate(
        self,
        case: TraceMiningCase,
        tools: MiningTools,
    ) -> FailureMiningResult:
        result = self.delegate.investigate(case, tools)
        forged = _evidence(EvidenceKind.ENVIRONMENT, "invented-state", "invented")
        check = replace(result.completion_checks[0], evidence=(forged,))
        return replace(
            result,
            completion_checks=(check,),
            environment_evidence=(forged,),
        )


@dataclass(frozen=True, slots=True)
class PartialTraceFailureJudge:
    def investigate(
        self,
        case: TraceMiningCase,
        tools: MiningTools,
    ) -> FailureMiningResult:
        page = tools.read_trajectory(TrajectoryPageRequest(cursor=None, limit=1))
        verification = tools.verify_environment(
            EnvironmentCheckRequest(SOURCE_ID, CHECK_ID)
        )
        assert verification.verification is not None
        return FailureMiningResult(
            task=case.task,
            context=case.context,
            verdict=MiningVerdict.CONFIRMED_FAILURE,
            source_ids=tuple(source.id for source in case.sources),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Ticket closed successfully.",
                    observed_state=verification.verification.observed_state,
                    status=CompletionStatus.NOT_COMPLETED,
                    evidence=verification.artifacts,
                ),
            ),
            failure_behavior=FailureBehavior(
                primary=FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE,
                summary="The tool failure was not recovered before completion.",
                observations=(
                    BehaviorObservation(
                        kind=FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE,
                        phase=FailurePhase.RECOVERY,
                        first_observation_id=page.observations[0].record.id,
                        last_observation_id=None,
                        recovery_status=RecoveryStatus.NOT_RECOVERED,
                        evidence=page.artifacts,
                    ),
                ),
            ),
            trajectory_evidence=page.artifacts,
            environment_evidence=verification.artifacts,
            confidence=Confidence(0.9),
            unresolved_questions=(),
            invalid_reason=None,
        )


@pytest.mark.parametrize(
    ("outputs", "source_kind", "state", "observed", "search", "expected"),
    (
        (
            ("Close ticket", "update failed", "continuing", "Ticket closed successfully"),
            FailureSourceKind.DOWNSTREAM_FAILURE,
            CompletionStatus.NOT_COMPLETED,
            "Ticket remains open",
            "failed",
            MiningVerdict.CONFIRMED_FAILURE,
        ),
        (
            ("Close ticket", "update failed", "retry succeeded", "Ticket closed successfully"),
            FailureSourceKind.AGENT_ERROR,
            CompletionStatus.COMPLETED,
            "Ticket is closed",
            "failed",
            MiningVerdict.NO_FAILURE,
        ),
        (
            ("Close ticket", "update accepted", "Ticket closed successfully"),
            FailureSourceKind.USER_CORRECTION,
            CompletionStatus.NOT_COMPLETED,
            "Ticket remains open",
            "successfully",
            MiningVerdict.CONFIRMED_FAILURE,
        ),
        (
            ("Close ticket", "update accepted", "Ticket closed successfully"),
            FailureSourceKind.HUMAN_FEEDBACK,
            CompletionStatus.UNKNOWN,
            None,
            "successfully",
            MiningVerdict.AMBIGUOUS,
        ),
    ),
)
def test_mine_uses_complete_trajectory_and_verified_state(
    tmp_path: Path,
    outputs: tuple[str, ...],
    source_kind: FailureSourceKind,
    state: CompletionStatus,
    observed: str | None,
    search: str,
    expected: MiningVerdict,
) -> None:
    revision = _revision(tmp_path)
    run = Mine(
        revision=revision,
        collection=_collection(tmp_path, revision, outputs),
        nominations=(_nomination(source_kind),),
        judge=FakeFailureJudge(search),
        environment=RecordedEnvironmentVerifier(state, observed),
    ).run()

    assert run.results[0].verdict is expected
    assert len(run.results[0].trajectory_evidence) == len(outputs)


@pytest.mark.parametrize(
    ("collection_revision", "attribution", "gaps", "reason"),
    (
        ("other-revision", AttributionLevel.EXACT, (), MiningInvalidReason.REVISION_MISMATCH),
        (
            "revision-1",
            AttributionLevel.MISSING,
            (TraceGap.MISSING_ROOT,),
            MiningInvalidReason.CORRUPT_TRACE,
        ),
    ),
)
def test_wrong_revision_or_corrupt_trace_is_invalid(
    tmp_path: Path,
    collection_revision: str,
    attribution: AttributionLevel,
    gaps: tuple[TraceGap, ...],
    reason: MiningInvalidReason,
) -> None:
    revision = _revision(tmp_path)
    foreign_revision = _revision(tmp_path, collection_revision)
    collection = _collection(
        tmp_path,
        foreign_revision,
        ("Close ticket", "Ticket closed successfully"),
        attribution=attribution,
        gaps=gaps,
    )

    result = Mine(
        revision=revision,
        collection=collection,
        nominations=(_nomination(FailureSourceKind.AGENT_ERROR),),
        judge=FakeFailureJudge("successfully"),
        environment=RecordedEnvironmentVerifier(CompletionStatus.COMPLETED, "closed"),
    ).run().results[0]

    assert result.verdict is MiningVerdict.INVALID
    assert result.invalid_reason is reason


def test_confirmed_failure_requires_trajectory_and_environment_evidence() -> None:
    with pytest.raises(ValueError, match="confirmed failure requires"):
        FailureMiningResult(
            task=_task(),
            context=_context((ObservationId("observation-1"),)),
            verdict=MiningVerdict.CONFIRMED_FAILURE,
            source_ids=(FailureSourceId("signal-1"),),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Ticket closed successfully.",
                    observed_state="Ticket remains open.",
                    status=CompletionStatus.NOT_COMPLETED,
                    evidence=(),
                ),
            ),
            failure_behavior=FailureBehavior(
                primary=FailureBehaviorKind.FALSE_COMPLETION,
                summary="The agent claimed success while the ticket stayed open.",
                observations=(
                    BehaviorObservation(
                        kind=FailureBehaviorKind.FALSE_COMPLETION,
                        phase=FailurePhase.COMPLETION,
                        first_observation_id=ObservationId("observation-1"),
                        last_observation_id=None,
                        recovery_status=RecoveryStatus.NOT_RECOVERED,
                        evidence=(
                            _evidence(
                                EvidenceKind.TRAJECTORY,
                                "observation-1",
                                "observation-1",
                            ),
                        ),
                    ),
                ),
            ),
            trajectory_evidence=(
                _evidence(EvidenceKind.TRAJECTORY, "observation-1", "observation-1"),
            ),
            environment_evidence=(),
            confidence=Confidence(0.9),
            unresolved_questions=(),
            invalid_reason=None,
        )


def test_judge_cannot_invent_evidence_that_no_tool_returned(tmp_path: Path) -> None:
    revision = _revision(tmp_path)
    result = Mine(
        revision=revision,
        collection=_collection(
            tmp_path,
            revision,
            ("Close ticket", "update failed", "Ticket closed successfully"),
        ),
        nominations=(_nomination(FailureSourceKind.DOWNSTREAM_FAILURE),),
        judge=ForgingFailureJudge(FakeFailureJudge("failed")),
        environment=RecordedEnvironmentVerifier(
            CompletionStatus.NOT_COMPLETED,
            "Ticket remains open",
        ),
    ).run().results[0]

    assert result.verdict is MiningVerdict.INVALID
    assert result.invalid_reason is MiningInvalidReason.JUDGE_OUTPUT


def test_judge_must_read_the_full_trace_before_returning_verdict(tmp_path: Path) -> None:
    revision = _revision(tmp_path)
    result = Mine(
        revision=revision,
        collection=_collection(
            tmp_path,
            revision,
            ("Close ticket", "update failed", "Ticket closed successfully"),
        ),
        nominations=(_nomination(FailureSourceKind.DOWNSTREAM_FAILURE),),
        judge=PartialTraceFailureJudge(),
        environment=RecordedEnvironmentVerifier(
            CompletionStatus.NOT_COMPLETED,
            "Ticket remains open",
        ),
    ).run().results[0]

    assert result.verdict is MiningVerdict.INVALID
    assert result.invalid_reason is MiningInvalidReason.JUDGE_OUTPUT


def test_confirmed_failure_requires_failure_behavior() -> None:
    with pytest.raises(ValueError, match="confirmed failure requires"):
        FailureMiningResult(
            task=_task(),
            context=_context((ObservationId("observation-1"),)),
            verdict=MiningVerdict.CONFIRMED_FAILURE,
            source_ids=(FailureSourceId("signal-1"),),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Ticket closed successfully.",
                    observed_state="Ticket remains open.",
                    status=CompletionStatus.NOT_COMPLETED,
                    evidence=(_evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),),
                ),
            ),
            failure_behavior=None,
            trajectory_evidence=(
                _evidence(EvidenceKind.TRAJECTORY, "observation-1", "observation-1"),
            ),
            environment_evidence=(
                _evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),
            ),
            confidence=Confidence(0.9),
            unresolved_questions=(),
            invalid_reason=None,
        )


def test_no_failure_rejects_failure_behavior() -> None:
    with pytest.raises(ValueError, match="no-failure verdict"):
        FailureMiningResult(
            task=_task(),
            context=_context((ObservationId("observation-1"),)),
            verdict=MiningVerdict.NO_FAILURE,
            source_ids=(FailureSourceId("signal-1"),),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Retry succeeded.",
                    observed_state="Ticket is closed.",
                    status=CompletionStatus.COMPLETED,
                    evidence=(_evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),),
                ),
            ),
            failure_behavior=FailureBehavior(
                primary=FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE,
                summary="A failure was incorrectly retained after recovery.",
                observations=(
                    BehaviorObservation(
                        kind=FailureBehaviorKind.UNRECOVERED_ACTION_FAILURE,
                        phase=FailurePhase.RECOVERY,
                        first_observation_id=ObservationId("observation-1"),
                        last_observation_id=None,
                        recovery_status=RecoveryStatus.RECOVERED,
                        evidence=(
                            _evidence(
                                EvidenceKind.TRAJECTORY,
                                "observation-1",
                                "observation-1",
                            ),
                        ),
                    ),
                ),
            ),
            trajectory_evidence=(
                _evidence(EvidenceKind.TRAJECTORY, "observation-1", "observation-1"),
            ),
            environment_evidence=(
                _evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),
            ),
            confidence=Confidence(0.9),
            unresolved_questions=(),
            invalid_reason=None,
        )


def test_failure_behavior_observation_must_belong_to_context() -> None:
    with pytest.raises(ValueError, match="failure behavior observation"):
        FailureMiningResult(
            task=_task(),
            context=_context((ObservationId("observation-1"),)),
            verdict=MiningVerdict.CONFIRMED_FAILURE,
            source_ids=(FailureSourceId("signal-1"),),
            completion_checks=(
                CompletionCheck(
                    check_id=CHECK_ID,
                    required_outcome="The ticket is closed.",
                    agent_claim="Ticket closed successfully.",
                    observed_state="Ticket remains open.",
                    status=CompletionStatus.NOT_COMPLETED,
                    evidence=(_evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),),
                ),
            ),
            failure_behavior=FailureBehavior(
                primary=FailureBehaviorKind.FALSE_COMPLETION,
                summary="The agent claimed success while the ticket stayed open.",
                observations=(
                    BehaviorObservation(
                        kind=FailureBehaviorKind.FALSE_COMPLETION,
                        phase=FailurePhase.COMPLETION,
                        first_observation_id=ObservationId("observation-2"),
                        last_observation_id=None,
                        recovery_status=RecoveryStatus.NOT_RECOVERED,
                        evidence=(
                            _evidence(
                                EvidenceKind.TRAJECTORY,
                                "observation-2",
                                "observation-2",
                            ),
                        ),
                    ),
                ),
            ),
            trajectory_evidence=(
                _evidence(EvidenceKind.TRAJECTORY, "observation-1", "observation-1"),
            ),
            environment_evidence=(
                _evidence(EvidenceKind.ENVIRONMENT, "ticket-123", "state-1"),
            ),
            confidence=Confidence(0.9),
            unresolved_questions=(),
            invalid_reason=None,
        )


def test_search_focuses_natural_query_and_adapt_compares_signals(tmp_path: Path) -> None:
    revision = _revision(tmp_path)
    collection = _collection(
        tmp_path,
        revision,
        ("x" * 1500 + " update failed after retry", "Ticket remains open"),
    )
    nomination = _nomination(FailureSourceKind.DOWNSTREAM_FAILURE)
    prior_signal = replace(
        nomination.sources[0],
        id=FailureSourceId("signal-2"),
        trace_id=TraceId("trace-2"),
        kind=FailureSourceKind.HUMAN_FEEDBACK,
    )
    tools = MiningTools(
        TraceMiningCase(
            nomination.task,
            _context(collection.traces[0].observation_ids),
            nomination.sources,
        ),
        collection,
        RecordedEnvironmentVerifier(CompletionStatus.UNKNOWN, None),
        (*nomination.sources, prior_signal),
    )

    search = tools.search_trajectory(
        TrajectorySearchRequest("completion failed retries", ObservationContentField.ANY, 5)
    )
    adapted = tools.adapt(
        AdaptationRequest(
            (FailureSourceKind.DOWNSTREAM_FAILURE, FailureSourceKind.HUMAN_FEEDBACK),
            5,
        )
    )

    assert search.status is ToolStatus.OK
    assert "update failed" in search.hits[0].excerpt
    assert tuple(signal.id for signal in adapted.signals) == (
        FailureSourceId("signal-1"),
        FailureSourceId("signal-2"),
    )
