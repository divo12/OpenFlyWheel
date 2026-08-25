"""MCP transport exposing a live OFW failure-mining case to Codex."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from ofw.mine import (
    AdaptationRequest,
    CompletionStatus,
    ConstraintKind,
    EnvironmentCheckId,
    EnvironmentCheckRequest,
    EnvironmentSourceId,
    EnvironmentSourceKind,
    EvidenceKind,
    EvidenceReference,
    FailureSource,
    FailureSourceKind,
    MiningTools,
    ToolAccess,
    ToolAction,
    ToolStatus,
    TrajectoryPageRequest,
    TrajectorySearchRequest,
    TrajectorySearchResult,
)
from ofw.observability.langfuse.domain import ObservationContentField, ObservationId


class McpModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class McpEvidence(McpModel):
    kind: EvidenceKind
    record_id: str
    digest: str


class McpRequiredOutcome(McpModel):
    check_id: str
    source_id: str
    description: str


class McpConstraint(McpModel):
    kind: ConstraintKind
    description: str


class McpToolCapability(McpModel):
    name: str
    access: ToolAccess


class McpEnvironmentSource(McpModel):
    id: str
    kind: EnvironmentSourceKind
    summary: str


class McpFailureSignal(McpModel):
    id: str
    kind: FailureSourceKind
    trace_id: str
    observed_at: datetime
    summary: str
    evidence: tuple[McpEvidence, ...]


class McpMiningCase(McpModel):
    task_id: str
    intent: str
    required_outcomes: tuple[McpRequiredOutcome, ...]
    constraints: tuple[McpConstraint, ...]
    revision_id: str
    trace_id: str
    trace_digest: str
    observation_ids: tuple[str, ...]
    session_id: str | None
    environment_name: str | None
    release: str | None
    available_tools: tuple[McpToolCapability, ...]
    environment_sources: tuple[McpEnvironmentSource, ...]
    initial_state_evidence: tuple[McpEvidence, ...]
    failure_signals: tuple[McpFailureSignal, ...]


class McpSearchHit(McpModel):
    observation_id: str
    trace_id: str | None
    field: ObservationContentField
    excerpt: str
    evidence: McpEvidence


class McpSearchResult(McpModel):
    status: ToolStatus
    summary: str
    hits: tuple[McpSearchHit, ...]
    next_actions: tuple[ToolAction, ...]


class McpTrajectoryObservation(McpModel):
    id: str
    parent_id: str | None
    name: str | None
    type: str
    start_time: datetime
    status_message: str | None
    input: str | None
    output: str | None
    evidence: McpEvidence


class McpTrajectoryPage(McpModel):
    status: ToolStatus
    summary: str
    observations: tuple[McpTrajectoryObservation, ...]
    next_cursor: str | None
    next_actions: tuple[ToolAction, ...]


class McpEnvironmentVerification(McpModel):
    tool_status: ToolStatus
    summary: str
    completion_status: CompletionStatus | None
    observed_state: str | None
    evidence: tuple[McpEvidence, ...]
    next_actions: tuple[ToolAction, ...]


class McpAdaptationResult(McpModel):
    status: ToolStatus
    summary: str
    signals: tuple[McpFailureSignal, ...]
    next_actions: tuple[ToolAction, ...]


@dataclass(slots=True)
class FailureMiningMcpServer:
    """A local, read-only MCP server backed by one live mining case."""

    tools: MiningTools
    server: MCPServer[None] = field(init=False)

    def __post_init__(self) -> None:
        server: MCPServer[None] = MCPServer(
            name="openflywheel-failure-mining",
            instructions=(
                "Inspect and verify one executed Hermes trajectory. Do not diagnose causes, "
                "propose fixes, cluster failures, generate evals, or mutate rubrics."
            ),
        )
        server.tool(structured_output=True)(self.get_mining_case)
        server.tool(structured_output=True)(self.search_trajectory)
        server.tool(structured_output=True)(self.search_prior_trajectories)
        server.tool(structured_output=True)(self.read_trajectory)
        server.tool(structured_output=True)(self.verify_environment)
        server.tool(structured_output=True)(self.adapt)
        self.server = server

    def get_mining_case(self) -> McpMiningCase:
        """Return the task, context, signals, environment sources, and required outcomes."""
        case = self.tools.case
        return McpMiningCase(
            task_id=case.task.id.value,
            intent=case.task.intent,
            required_outcomes=tuple(
                McpRequiredOutcome(
                    check_id=item.check_id.value,
                    source_id=item.source_id.value,
                    description=item.description,
                )
                for item in case.task.required_outcomes
            ),
            constraints=tuple(
                McpConstraint(kind=item.kind, description=item.description)
                for item in case.task.constraints
            ),
            revision_id=case.context.revision_id.value,
            trace_id=case.context.trace_id.value,
            trace_digest=case.context.trace_digest.value,
            observation_ids=tuple(item.value for item in case.context.observation_ids),
            session_id=case.context.session_id,
            environment_name=case.context.environment_name,
            release=case.context.release,
            available_tools=tuple(
                McpToolCapability(name=item.name.value, access=item.access)
                for item in case.context.available_tools
            ),
            environment_sources=tuple(
                McpEnvironmentSource(id=item.id.value, kind=item.kind, summary=item.summary)
                for item in case.context.environment_sources
            ),
            initial_state_evidence=_evidence(case.context.initial_state_evidence),
            failure_signals=tuple(_signal(item) for item in case.sources),
        )

    def search_trajectory(
        self,
        text: str,
        field: ObservationContentField = ObservationContentField.ANY,
        limit: int = 10,
    ) -> McpSearchResult:
        """Search the current full trajectory for focused evidence."""
        return _search_result(
            self.tools.search_trajectory(TrajectorySearchRequest(text, field, limit))
        )

    def search_prior_trajectories(
        self,
        text: str,
        field: ObservationContentField = ObservationContentField.ANY,
        limit: int = 10,
    ) -> McpSearchResult:
        """Search other trajectories in the collection for comparable evidence."""
        return _search_result(
            self.tools.search_prior_trajectories(
                TrajectorySearchRequest(text, field, limit)
            )
        )

    def read_trajectory(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> McpTrajectoryPage:
        """Read an ordered page; continue until next_cursor is null."""
        result = self.tools.read_trajectory(
            TrajectoryPageRequest(
                None if cursor is None else ObservationId(cursor),
                limit,
            )
        )
        return McpTrajectoryPage(
            status=result.status,
            summary=result.summary,
            observations=tuple(
                McpTrajectoryObservation(
                    id=item.record.id.value,
                    parent_id=(
                        None
                        if item.record.parent_observation_id is None
                        else item.record.parent_observation_id.value
                    ),
                    name=item.record.name,
                    type=item.record.type.value,
                    start_time=item.record.start_time,
                    status_message=item.record.status_message,
                    input=None if item.input_content is None else item.input_content.text,
                    output=None if item.output_content is None else item.output_content.text,
                    evidence=McpEvidence(
                        kind=EvidenceKind.TRAJECTORY,
                        record_id=item.record.id.value,
                        digest=item.record.digest.value,
                    ),
                )
                for item in result.observations
            ),
            next_cursor=None if result.next_cursor is None else result.next_cursor.value,
            next_actions=result.next_actions,
        )

    def verify_environment(
        self,
        source_id: str,
        check_id: str,
    ) -> McpEnvironmentVerification:
        """Verify a declared required outcome against its source-of-truth environment."""
        result = self.tools.verify_environment(
            EnvironmentCheckRequest(
                EnvironmentSourceId(source_id),
                EnvironmentCheckId(check_id),
            )
        )
        verification = result.verification
        return McpEnvironmentVerification(
            tool_status=result.status,
            summary=result.summary,
            completion_status=None if verification is None else verification.status,
            observed_state=None if verification is None else verification.observed_state,
            evidence=_evidence(result.artifacts),
            next_actions=result.next_actions,
        )

    def adapt(
        self,
        kinds: tuple[FailureSourceKind, ...],
        limit: int = 20,
    ) -> McpAdaptationResult:
        """Read human and production calibration signals without changing a rubric."""
        result = self.tools.adapt(AdaptationRequest(kinds, limit))
        return McpAdaptationResult(
            status=result.status,
            summary=result.summary,
            signals=tuple(_signal(item) for item in result.signals),
            next_actions=result.next_actions,
        )

    def run_stdio(self) -> None:
        """Serve this live case to a local Codex process over standard I/O."""
        self.server.run()


def _evidence(items: tuple[EvidenceReference, ...]) -> tuple[McpEvidence, ...]:
    return tuple(
        McpEvidence(
            kind=item.kind,
            record_id=item.record_id.value,
            digest=item.digest.value,
        )
        for item in items
    )


def _signal(item: FailureSource) -> McpFailureSignal:
    return McpFailureSignal(
        id=item.id.value,
        kind=item.kind,
        trace_id=item.trace_id.value,
        observed_at=item.observed_at,
        summary=item.summary,
        evidence=_evidence(item.evidence),
    )


def _search_result(result: TrajectorySearchResult) -> McpSearchResult:
    return McpSearchResult(
        status=result.status,
        summary=result.summary,
        hits=tuple(
            McpSearchHit(
                observation_id=hit.observation_id.value,
                trace_id=None if hit.trace_id is None else hit.trace_id.value,
                field=hit.field,
                excerpt=hit.excerpt,
                evidence=McpEvidence(
                    kind=EvidenceKind.TRAJECTORY,
                    record_id=hit.observation_id.value,
                    digest=hit.reference.digest.value,
                ),
            )
            for hit in result.hits
        ),
        next_actions=result.next_actions,
    )
