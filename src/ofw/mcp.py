#!/usr/bin/env python3
"""Installable OpenFlywheel MCP server."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Annotated, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field

from ofw.evaluation.failure_curation import (
    FailureCurationObservation,
    FailureCurationService,
    RecordFailureCurationInput,
)
from ofw.evaluation.failure_patterns import (
    FailurePatternMiningObservation,
    FailurePatternMiningService,
    MineFailurePatternsInput,
)
from ofw.evaluation.failure_workspace import (
    FailureRecordObservation,
    FailureWorkspaceService,
    FileFailureCurationWorkspace,
    FileFailureWorkspace,
    RecordFailureInput,
)
from ofw.evaluation.langfuse import (
    LangfuseOutcomeStore,
    OutcomeStoreObservation,
    OutcomeStoreStatus,
)
from ofw.evaluation.outcome import (
    EvidenceReference,
    OutcomeEvaluation,
    TaskId,
    VerifierId,
    VerifierResult,
    VerifierVerdict,
)
from ofw.evolution import (
    AdvanceEvolutionInput,
    CandidateExecutionInput,
    CandidateExecutionObservation,
    CandidateExecutionService,
    CandidateGitGateway,
    EvolutionController,
    EvolutionObservation,
    FileHypothesisRepository,
    HypothesisObservation,
    HypothesisService,
    LangfuseCandidateTraceLocator,
    RecordHypothesisInput,
)
from ofw.observability.langfuse.contracts import LangfuseProject
from ofw.observability.langfuse.domain import TraceId
from ofw.observability.langfuse.trace_query import (
    GetSpanContextInput,
    GetTraceSchemaInput,
    ListTracesInput,
    QuerySpansInput,
    SessionIdentifier,
    SpanFilters,
    TraceListObservation,
    TraceQueryObservation,
    TraceQueryService,
    TraceTimeRange,
)
from ofw.observability.langfuse.transport import LangfuseHttpClient
from ofw.preparation import (
    PrepareWorkspaceInput,
    WorkspacePreparationObservation,
    WorkspacePreparationService,
)
from ofw.preparation.harbor import HarborBaselineRunner, HarborExperimentRunner
from ofw.preparation.worktree import GitWorktreeGateway

QueryInput = TypeVar("QueryInput")
QueryOutput = TypeVar("QueryOutput", bound=BaseModel)
_QUERY_TIMEOUT_SECONDS = 60.0
_PROGRAM_TEMPLATE_LIMIT_BYTES = 128 * 1024
TraceIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
SpanIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
CursorIdentifier = Annotated[str, Field(min_length=1, max_length=4096)]
TracePageLimit = Annotated[int, Field(strict=True, ge=1, le=50)]
TaskIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
EvolutionExperimentIdentifier = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
]
VerifierIdentifier = Annotated[str, Field(min_length=1, max_length=256)]
OutcomeScore = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
EvidenceIdentifier = Annotated[str, Field(min_length=1, max_length=1024)]
OutcomeEvidence = Annotated[
    tuple[EvidenceIdentifier, ...], Field(min_length=1, max_length=10)
]

server = FastMCP[None](  # type: ignore[misc]  # MCP auth generics are untyped upstream.
    name="openflywheel",
    instructions=(
        "Prepare isolated ITSM harness workspaces, read bounded Langfuse trace evidence, and "
        "record authoritative outcomes, compact failure diagnoses, exact patterns, and "
        "evidence-backed hypotheses and isolated candidates. Never infer outcomes, mutate "
        "traces, copy trace payloads into local storage, or broaden candidate edit authority. "
        "The evolution controller owns publication, rollback, budgets, and stopping; after "
        "rollback, require a fresh Harbor run for attribution."
    ),
    log_level="DEBUG",
)
read_only = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
record_write = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


class OutcomeToolErrorCode(StrEnum):
    STORE_FAILED = "outcome_store_failed"


class OutcomeToolError(Exception):
    """Sanitized outcome-recording failure returned to the MCP client."""

    __slots__ = ("code", "trace_id")

    def __init__(self, code: OutcomeToolErrorCode, trace_id: str) -> None:
        self.code = code
        self.trace_id = trace_id
        super().__init__(f"{code.value}: {trace_id}")


def _project() -> LangfuseProject:
    return LangfuseProject.from_env(
        environment=os.environ.get("LANGFUSE_ENVIRONMENT", "ofw-local"),
        allow_private_network=os.environ.get("LANGFUSE_ALLOW_PRIVATE_NETWORK") == "1",
    )


def _bounded_absolute_path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("workspace_root must be a path")
    text = str(value)
    if "\x00" in text or len(text.encode("utf-8")) > 1024:
        raise ValueError("workspace_root must be bounded")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("workspace_root must be absolute")
    return path


EvolutionWorkspaceRoot = Annotated[Path, BeforeValidator(_bounded_absolute_path)]


def _client() -> LangfuseHttpClient:
    return LangfuseHttpClient(_project(), timeout_seconds=_QUERY_TIMEOUT_SECONDS)


def _outcome_store() -> LangfuseOutcomeStore:
    return LangfuseOutcomeStore.from_project(_project())


def _preparation_service() -> WorkspacePreparationService:
    return WorkspacePreparationService(
        runner=HarborBaselineRunner(),
        workspace=GitWorktreeGateway(),
        base_program=_program_template("base.md"),
        itsm_program=_program_template("itsm.md"),
    )


def _failure_service() -> FailureWorkspaceService:
    return FailureWorkspaceService(FileFailureWorkspace())


def _failure_pattern_service() -> FailurePatternMiningService:
    return FailurePatternMiningService(FileFailureWorkspace())


def _curation_service() -> FailureCurationService:
    return FailureCurationService(FileFailureCurationWorkspace())


def _hypothesis_service() -> HypothesisService:
    workspace = FileFailureWorkspace()
    return HypothesisService(
        pattern_miner=FailurePatternMiningService(workspace),
        repository=FileHypothesisRepository(),
    )


@contextmanager
def _candidate_service() -> Iterator[CandidateExecutionService]:
    client = _client()
    try:
        store = _outcome_store()
        try:
            yield CandidateExecutionService(
                workspace=CandidateGitGateway(),
                hypotheses=FileHypothesisRepository(),
                runner=HarborExperimentRunner(),
                trace_locator=LangfuseCandidateTraceLocator(client),
                outcome_store=store,
            )
        finally:
            store.close()
    finally:
        client.close()


def _evolution_controller(workspace_root: Path) -> EvolutionController:
    return EvolutionController(workspace_root=workspace_root)


def _program_template(name: str) -> str:
    content = files("ofw.preparation.templates").joinpath(name).read_bytes()
    if len(content) > _PROGRAM_TEMPLATE_LIMIT_BYTES:
        raise ValueError(f"program template exceeds byte bound: {name}")
    return content.decode()


def _execute(
    query: QueryInput,
    operation: Callable[[TraceQueryService, QueryInput], QueryOutput],
) -> QueryOutput:
    client = _client()
    try:
        return operation(TraceQueryService(client), query)
    finally:
        client.close()


@server.tool(annotations=record_write, structured_output=True)
def prepare_workspace(config: PrepareWorkspaceInput) -> WorkspacePreparationObservation:
    """Create or poll one isolated ITSM experiment worktree and baseline."""
    return _preparation_service().prepare(config)


@server.tool(annotations=read_only, structured_output=True)
def list_traces(
    session_id: SessionIdentifier,
    time_range: TraceTimeRange,
    environment: TraceIdentifier | None = None,
    release: TraceIdentifier | None = None,
    cursor: CursorIdentifier | None = None,
    limit: TracePageLimit = 20,
) -> TraceListObservation:
    """List bounded logical-root traces for one session and time range."""
    query = ListTracesInput(
        session_id=session_id,
        environment=environment,
        release=release,
        time_range=time_range,
        cursor=cursor,
        limit=limit,
    )
    return _execute(query, TraceQueryService.list_traces)


@server.tool(annotations=read_only, structured_output=True)
def get_trace_schema(
    trace_id: TraceIdentifier,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Skim bounded trace structure without loading span input or output."""
    query = GetTraceSchemaInput(trace_id=trace_id, cursor=cursor)
    return _execute(query, TraceQueryService.get_trace_schema)


@server.tool(annotations=read_only, structured_output=True)
def query_spans(
    trace_id: TraceIdentifier,
    filters: SpanFilters | None = None,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Find bounded span IDs using exact structural filters."""
    query = QuerySpansInput(
        trace_id=trace_id,
        filters=filters or SpanFilters(),
        cursor=cursor,
    )
    return _execute(query, TraceQueryService.query_spans)


@server.tool(annotations=read_only, structured_output=True)
def get_span_context(
    trace_id: TraceIdentifier,
    span_id: SpanIdentifier,
    cursor: CursorIdentifier | None = None,
) -> TraceQueryObservation:
    """Read one span, its parent, and up to ten direct children with bounded excerpts."""
    query = GetSpanContextInput(trace_id=trace_id, span_id=span_id, cursor=cursor)
    return _execute(query, TraceQueryService.get_span_context)


@server.tool(annotations=record_write, structured_output=True)
def record_outcome(
    trace_id: TraceIdentifier,
    task_id: TaskIdentifier,
    verifier_id: VerifierIdentifier,
    evaluated_at: datetime,
    verdict: VerifierVerdict,
    evidence: OutcomeEvidence,
    score: OutcomeScore | None = None,
) -> OutcomeStoreObservation:
    """Record one authoritative external-verifier outcome on its exact trace."""
    result = VerifierResult(
        verdict=verdict,
        score=score,
        feedback="Recorded by the OpenFlywheel outcome tool.",
        evidence=tuple(EvidenceReference(reference) for reference in evidence),
    )
    outcome = OutcomeEvaluation.from_verifier_result(
        trace_id=TraceId(trace_id),
        task_id=TaskId(task_id),
        verifier_id=VerifierId(verifier_id),
        evaluated_at=evaluated_at,
        result=result,
    )
    try:
        submission = _outcome_store().store(outcome)
    except Exception:
        raise OutcomeToolError(OutcomeToolErrorCode.STORE_FAILED, trace_id) from None
    return OutcomeStoreObservation(
        status=OutcomeStoreStatus.SUCCESS,
        summary=f"Stored authoritative {verdict.value} outcome on the trace.",
        next_actions=("Continue only after retaining this score receipt.",),
        artifacts=(trace_id, submission.score_id.value),
        trace_id=trace_id,
        score_id=submission.score_id.value,
    )


@server.tool(annotations=record_write, structured_output=True)
def record_failure(request: RecordFailureInput) -> FailureRecordObservation:
    """Store one bounded diagnosis under a prepared harness's local .workspace."""
    return _failure_service().record(request)


@server.tool(annotations=read_only, structured_output=True)
def mine_failure_patterns(
    request: MineFailurePatternsInput,
) -> FailurePatternMiningObservation:
    """Group explicit compact diagnoses by exact normalized root cause."""
    return _failure_pattern_service().mine(request)


@server.tool(annotations=record_write, structured_output=True)
def record_failure_curation(
    request: RecordFailureCurationInput,
) -> FailureCurationObservation:
    """Store one bounded cross-failure curation under a prepared local workspace."""
    return _curation_service().record(request)


@server.tool(annotations=record_write, structured_output=True)
def record_hypothesis(request: RecordHypothesisInput) -> HypothesisObservation:
    """Record one exact evidence-backed hypothesis, then stop before candidate editing."""
    return _hypothesis_service().record(request)


@server.tool(annotations=record_write, structured_output=True)
def execute_candidate(
    request: CandidateExecutionInput,
) -> CandidateExecutionObservation:
    """Create, seal, launch, or poll one exact hypothesis candidate."""
    with _candidate_service() as service:
        return service.execute(request)


@server.tool(annotations=read_only, structured_output=True)
def evolution_status(
    workspace_root: EvolutionWorkspaceRoot,
    experiment_id: EvolutionExperimentIdentifier,
) -> EvolutionObservation:
    """Read the replayed evolution state without appending or mutating it."""
    return _evolution_controller(workspace_root).status(experiment_id)


@server.tool(annotations=record_write, structured_output=True)
def advance_evolution(request: AdvanceEvolutionInput) -> EvolutionObservation:
    """Advance one deterministic evolution action; identical requests are idempotent."""
    return _evolution_controller(request.workspace_root).advance(request)


def main() -> None:
    """Run the OpenFlywheel MCP server over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
