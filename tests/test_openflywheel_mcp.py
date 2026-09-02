"""Unified OpenFlywheel MCP permission surface."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool

from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_patterns import (
    FailurePatternMiningObservation,
    FailurePatternMiningStatus,
    FailurePatternOrdering,
    MineFailurePatternsInput,
)
from ofw.evaluation.failure_workspace import (
    FailedOutcomeInput,
    FailureRecordObservation,
    FailureRecordStatus,
    FailureWorkspaceErrorCode,
    FailureWorkspaceFailure,
    FailureWorkspaceService,
    FileFailureWorkspace,
    RecordFailureInput,
)
from ofw.evaluation.langfuse import (
    OutcomeScoreSubmission,
    OutcomeStoreObservation,
    OutcomeStoreStatus,
)
from ofw.evaluation.outcome import (
    OutcomeEvaluation,
    OutcomeEvaluationError,
    TaskId,
    VerifierId,
)
from ofw.observability.langfuse.domain import ScoreId, TraceId
from ofw.observability.langfuse.trace_query import (
    GetSpanContextInput,
    GetTraceSchemaInput,
    ListTracesInput,
    QuerySpansInput,
    SpanFilters,
    TraceTimeRange,
)
from ofw.preparation import (
    PreparationPhase,
    PreparationStatus,
    PrepareWorkspaceInput,
    WorkspacePreparationObservation,
    WorkspacePreparationService,
)
from ofw.runtime import EvidenceReference, VerifierVerdict

_FAILURE_ARTIFACT_ID = "00000000-0000-0000-0000-000000000001"


class OpenFlywheelMcpModule(Protocol):
    server: FastMCP[None]
    OutcomeToolError: type[Exception]

    def _preparation_service(self) -> WorkspacePreparationService: ...

    def _failure_service(self) -> FailureWorkspaceService: ...

    def _program_template(self, name: str) -> str: ...

    def prepare_workspace(
        self,
        config: PrepareWorkspaceInput,
    ) -> WorkspacePreparationObservation: ...

    def list_traces(
        self,
        session_id: str,
        time_range: TraceTimeRange,
        environment: str | None = None,
        release: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> object: ...

    def get_trace_schema(self, trace_id: str, cursor: str | None = None) -> object: ...

    def query_spans(
        self,
        trace_id: str,
        filters: SpanFilters | None = None,
        cursor: str | None = None,
    ) -> object: ...

    def get_span_context(
        self,
        trace_id: str,
        span_id: str,
        cursor: str | None = None,
    ) -> object: ...

    def record_outcome(
        self,
        trace_id: str,
        task_id: str,
        verifier_id: str,
        evaluated_at: datetime,
        verdict: VerifierVerdict,
        evidence: tuple[str, ...],
        score: float | None = None,
    ) -> OutcomeStoreObservation: ...

    def record_failure(self, request: RecordFailureInput) -> FailureRecordObservation: ...

    def mine_failure_patterns(
        self,
        request: MineFailurePatternsInput,
    ) -> FailurePatternMiningObservation: ...


class _FakeOutcomeStore:
    def __init__(self) -> None:
        self.outcomes: list[OutcomeEvaluation] = []
        self.close_count = 0
        self.failure: Exception | None = None

    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        if self.failure is not None:
            raise self.failure
        self.outcomes.append(outcome)
        return OutcomeScoreSubmission(ScoreId("score-1"), outcome.trace_id)

    def close(self) -> None:
        self.close_count += 1


class _FakePreparationService:
    def __init__(self, observation: WorkspacePreparationObservation) -> None:
        self.observation = observation
        self.requests: list[PrepareWorkspaceInput] = []

    def prepare(self, request: PrepareWorkspaceInput) -> WorkspacePreparationObservation:
        self.requests.append(request)
        return self.observation


class _FakeFailureService:
    def __init__(self, observation: FailureRecordObservation) -> None:
        self.observation = observation
        self.requests: list[RecordFailureInput] = []
        self.failure: FailureWorkspaceFailure | None = None

    def record(self, request: RecordFailureInput) -> FailureRecordObservation:
        if self.failure is not None:
            raise self.failure
        self.requests.append(request)
        return self.observation


def _module() -> OpenFlywheelMcpModule:
    return cast(OpenFlywheelMcpModule, importlib.import_module("ofw.mcp"))


def _server() -> FastMCP[None]:
    return _module().server


def _annotation_flags(tool: Tool) -> tuple[bool | None, bool | None, bool | None]:
    annotations = tool.annotations
    assert annotations is not None
    return (
        annotations.readOnlyHint,
        annotations.destructiveHint,
        annotations.idempotentHint,
    )


def _failed_outcome_input() -> FailedOutcomeInput:
    return FailedOutcomeInput(
        trace_id="trace-1",
        task_id="task-1",
        verifier_id="itsm-bench@v1",
        evaluated_at=datetime(2026, 8, 28, 6, 0, tzinfo=UTC),
        score=0.0,
        evidence=("harbor://trial-1/verifier/result",),
        outcome_score_id="outcome-score-1",
    )


def _failure_observation(
    evidence_status: FailureEvidenceStatus,
    issue_type: FailureType | None,
) -> FailureRecordObservation:
    relative_path = Path(f".workspace/failures/{_FAILURE_ARTIFACT_ID}.json")
    return FailureRecordObservation(
        status=FailureRecordStatus.SUCCESS,
        summary="Stored one failure diagnosis in the local workspace.",
        next_actions=("Retain the artifact path for failure analysis.",),
        artifacts=(str(relative_path), _FAILURE_ARTIFACT_ID),
        trace_id="trace-1",
        task_id="task-1",
        artifact_id=_FAILURE_ARTIFACT_ID,
        relative_path=relative_path,
        evidence_status=evidence_status,
        issue_type=issue_type,
    )


def _supported_failure_request(root: Path) -> RecordFailureInput:
    return RecordFailureInput(
        workspace_root=root,
        outcome=_failed_outcome_input(),
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
        expected_outcome="Incident is closed.",
        actual_outcome="Incident remains open.",
        critical_observation_id="observation-7",
        evidence_observation_ids=("observation-7",),
        root_cause="The agent finalized before checking state.",
        counterfactual_action="Check state before finalizing.",
        inconclusive_reason=None,
    )


def _inconclusive_failure_request(root: Path) -> RecordFailureInput:
    return RecordFailureInput(
        workspace_root=root,
        outcome=_failed_outcome_input(),
        evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
        issue_type=None,
        expected_outcome="Incident is closed.",
        actual_outcome="Incident remains open.",
        critical_observation_id=None,
        evidence_observation_ids=(),
        root_cause=None,
        counterfactual_action=None,
        inconclusive_reason="The trace is incomplete.",
    )


def test_mcp_exposes_scoped_read_and_recording_tools() -> None:
    tools = asyncio.run(_server().list_tools())

    assert [tool.name for tool in tools] == [
        "prepare_workspace",
        "list_traces",
        "get_trace_schema",
        "query_spans",
        "get_span_context",
        "record_outcome",
        "record_failure",
        "mine_failure_patterns",
    ]
    assert tuple(map(_annotation_flags, tools)) == (
        (False, False, True),
        (True, False, True),
        (True, False, True),
        (True, False, True),
        (True, False, True),
        (False, False, True),
        (False, False, True),
        (True, False, True),
    )


def test_prepare_workspace_passes_the_strict_config_to_the_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    expected = WorkspacePreparationObservation(
        status=PreparationStatus.WARNING,
        summary="The isolated ITSM baseline is still running.",
        next_actions=("Poll prepare_workspace with the identical request.",),
        artifacts=(str(tmp_path / "worktree"),),
        preparation_id="demo",
        phase=PreparationPhase.RUNNING,
        branch_name="ofw/demo",
        worktree_path=tmp_path / "worktree",
        next_poll_after_seconds=30,
    )
    service = _FakePreparationService(expected)
    monkeypatch.setattr(module, "_preparation_service", lambda: service)
    config = PrepareWorkspaceInput(
        experiment_id="demo",
        harness_root=tmp_path / "harness",
        base_ref="HEAD",
        worktree_parent=tmp_path / "worktrees",
        benchmark_root=tmp_path / "itsm",
        harbor_executable=tmp_path / "harbor",
        harbor_config=Path("config.json"),
        expected_task_count=1,
        editable_paths=(Path("prompt.md"),),
        goal="Improve verifier-backed ITSM quality.",
        quality_target=1.0,
        max_iterations=5,
        no_improvement_limit=3,
        max_cost_per_task_usd=1.0,
        max_latency_seconds=600.0,
        max_baseline_seconds=3600,
    )

    result = module.prepare_workspace(config)

    assert result == expected
    assert service.requests == [config]


def test_trace_tools_construct_their_typed_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    captured: list[object] = []
    expected = object()

    def execute(query: object, operation: object) -> object:
        captured.append(query)
        return expected

    monkeypatch.setattr(module, "_execute", execute)
    time_range = TraceTimeRange(
        start_time=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 8, 27, 11, 0, tzinfo=UTC),
    )
    filters = SpanFilters(tool_name="terminal", max_results=3)

    assert module.list_traces("session-1", time_range, "itsm-bench", "release-1") is expected
    assert module.get_trace_schema("trace-1") is expected
    assert module.query_spans("trace-1", filters) is expected
    assert module.get_span_context("trace-1", "span-1") is expected
    assert isinstance(captured[0], ListTracesInput)
    assert isinstance(captured[1], GetTraceSchemaInput)
    assert isinstance(captured[2], QuerySpansInput)
    assert isinstance(captured[3], GetSpanContextInput)


def test_preparation_service_loads_packaged_program_templates() -> None:
    module = _module()

    service = module._preparation_service()

    assert isinstance(service, WorkspacePreparationService)
    assert module._program_template("base.md").startswith("# OpenFlywheel Agent Program")


def test_record_outcome_maps_the_strict_contract_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    store = _FakeOutcomeStore()

    def outcome_store() -> _FakeOutcomeStore:
        return store

    monkeypatch.setattr(module, "_outcome_store", outcome_store)
    evaluated_at = datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC)

    result = module.record_outcome(
        trace_id="trace-1",
        task_id="task-1",
        verifier_id="verifier@v1",
        evaluated_at=evaluated_at,
        verdict=VerifierVerdict.PASS,
        score=1.0,
        evidence=("artifact://result",),
    )

    assert result == OutcomeStoreObservation(
        status=OutcomeStoreStatus.SUCCESS,
        summary="Stored authoritative pass outcome on the trace.",
        next_actions=("Continue only after retaining this score receipt.",),
        artifacts=("trace-1", "score-1"),
        trace_id="trace-1",
        score_id="score-1",
    )
    assert store.outcomes == [
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=evaluated_at,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=(EvidenceReference("artifact://result"),),
        )
    ]
    assert store.close_count == 1


def test_invalid_outcome_fails_before_opening_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    opened = False

    def outcome_store() -> _FakeOutcomeStore:
        nonlocal opened
        opened = True
        return _FakeOutcomeStore()

    monkeypatch.setattr(module, "_outcome_store", outcome_store)

    with pytest.raises(OutcomeEvaluationError):
        module.record_outcome(
            trace_id="trace-1",
            task_id="task-1",
            verifier_id="verifier@v1",
            evaluated_at=datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC),
            verdict=VerifierVerdict.PASS,
            score=None,
            evidence=("artifact://result",),
        )

    assert opened is False


def test_provider_failure_is_typed_and_does_not_leak_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    store = _FakeOutcomeStore()
    store.failure = RuntimeError("provider failure containing secret-key")

    def outcome_store() -> _FakeOutcomeStore:
        return store

    monkeypatch.setattr(module, "_outcome_store", outcome_store)

    with pytest.raises(module.OutcomeToolError) as raised:
        module.record_outcome(
            trace_id="trace-1",
            task_id="task-1",
            verifier_id="verifier@v1",
            evaluated_at=datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC),
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=("artifact://result",),
        )

    assert str(raised.value) == "outcome_store_failed: trace-1"
    assert store.close_count == 1


def test_record_failure_passes_one_strict_object_to_the_workspace_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    expected = _failure_observation(
        FailureEvidenceStatus.SUPPORTED,
        FailureType.CONTROL_FLOW_FAILURE,
    )
    service = _FakeFailureService(expected)
    request = _supported_failure_request(tmp_path)
    monkeypatch.setattr(module, "_failure_service", lambda: service)

    assert module.record_failure(request) == expected
    assert service.requests == [request]


def test_record_failure_preserves_typed_workspace_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    observation = _failure_observation(FailureEvidenceStatus.INCONCLUSIVE, None)
    service = _FakeFailureService(observation)
    service.failure = FailureWorkspaceFailure(
        FailureWorkspaceErrorCode.WRITE_FAILED,
        _FAILURE_ARTIFACT_ID,
    )
    monkeypatch.setattr(module, "_failure_service", lambda: service)
    request = _inconclusive_failure_request(tmp_path)

    with pytest.raises(FailureWorkspaceFailure) as raised:
        module.record_failure(request)

    assert raised.value.code is FailureWorkspaceErrorCode.WRITE_FAILED
    assert str(raised.value) == f"write_failed: {_FAILURE_ARTIFACT_ID}"


def test_mine_failure_patterns_passes_one_bounded_object_to_the_service(
    tmp_path: Path,
) -> None:
    module = _module()
    (tmp_path / "PROGRAM.md").write_text("# Program\n", encoding="utf-8")
    (tmp_path / "experiment_config.yaml").write_text(
        "benchmark: itsm-bench\n",
        encoding="utf-8",
    )
    recorded = FailureWorkspaceService(FileFailureWorkspace()).record(
        _inconclusive_failure_request(tmp_path)
    )
    request = MineFailurePatternsInput(
        workspace_root=tmp_path,
        artifact_ids=(recorded.artifact_id,),
    )

    result = module.mine_failure_patterns(request)

    assert result.status is FailurePatternMiningStatus.SUCCESS
    assert result.ordering is FailurePatternOrdering.OCCURRENCES_TASKS_LATEST_FINGERPRINT
    assert result.source_artifact_count == 1
    assert result.patterns == ()
    assert result.inconclusive_artifact_ids == (recorded.artifact_id,)
