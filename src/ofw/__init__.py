"""Public OpenFlyWheel harness API."""

from pathlib import Path

from langfuse import (
    Langfuse,
    LangfuseOtelSpanAttributes,
    LangfuseSpan,
    get_client,
    is_default_export_span,
    observe,
    propagate_attributes,
)

from ofw.contracts import (
    GitCommit,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionId,
    HarnessValidationError,
    RepositorySnapshot,
    Sha256Digest,
)
from ofw.mine import (
    AdaptationRequest,
    AdaptationResult,
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
    FailureMiningRun,
    FailurePhase,
    FailureSource,
    FailureSourceId,
    FailureSourceKind,
    Mine,
    MiningContext,
    MiningInvalidReason,
    MiningNomination,
    MiningTask,
    MiningVerdict,
    RecoveryStatus,
    RequiredOutcome,
    TaskConstraint,
    TaskId,
    ToolAccess,
    ToolCapability,
    ToolName,
    TraceMiningCase,
)
from ofw.observability.langfuse import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    CollectionResult,
    ObservationContent,
    ObservationContentField,
    ObservationContentHit,
    ObservationContentMatch,
    ObservationContentQuery,
    ObservationContentReference,
    ObservationRecord,
    TraceId,
)
from ofw.observability.langfuse.service import (
    collect,
    read_observation_content,
    read_trace_observations,
    search_observation_content,
)
from ofw.repository import process_repository
from ofw.runtime import (
    CanaryCase,
    CaseId,
    CommandLoop,
    CommandVerifier,
    E2BSandbox,
    ModelFingerprint,
    ProcessCommand,
    ProcessLimits,
    RunErrorCode,
    RunResult,
    RunStatus,
    VerifierResult,
    VerifierVerdict,
)


class _OfwNamespace:
    __slots__ = ()

    E2BSandbox = E2BSandbox
    ProcessLimits = ProcessLimits
    ProcessCommand = ProcessCommand
    CommandLoop = CommandLoop
    ModelFingerprint = ModelFingerprint
    CommandVerifier = CommandVerifier
    CanaryCase = CanaryCase
    CaseId = CaseId

    def collect(
        self,
        revision: HarnessRevision,
        *,
        window: TraceWindow,
        store_path: Path | None = None,
    ) -> CollectionResult:
        return collect(revision, window=window, store_path=store_path)

    def search_observation_content(
        self,
        collection: CollectionResult,
        query: ObservationContentQuery,
    ) -> tuple[ObservationContentHit, ...]:
        return search_observation_content(collection, query)

    def read_trace_observations(
        self,
        collection: CollectionResult,
        trace_id: TraceId,
        limit: int,
    ) -> tuple[ObservationRecord, ...]:
        return read_trace_observations(collection, trace_id, limit)

    def read_observation_content(
        self,
        collection: CollectionResult,
        reference: ObservationContentReference,
    ) -> ObservationContent:
        return read_observation_content(collection, reference)

ofw = _OfwNamespace()

__all__ = [
    "AdaptationRequest",
    "AdaptationResult",
    "BehaviorObservation",
    "CanaryCase",
    "CaseId",
    "CollectionError",
    "CollectionErrorCode",
    "CollectionResult",
    "CommandLoop",
    "CommandVerifier",
    "CompletionCheck",
    "CompletionStatus",
    "Confidence",
    "ConstraintKind",
    "E2BSandbox",
    "EnvironmentCheckId",
    "EnvironmentCheckRequest",
    "EnvironmentSource",
    "EnvironmentSourceId",
    "EnvironmentSourceKind",
    "EnvironmentVerification",
    "EvidenceKind",
    "EvidenceRecordId",
    "EvidenceReference",
    "FailureMiningResult",
    "FailureMiningRun",
    "FailureBehavior",
    "FailureBehaviorKind",
    "FailurePhase",
    "FailureSource",
    "FailureSourceId",
    "FailureSourceKind",
    "GitCommit",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "Langfuse",
    "LangfuseOtelSpanAttributes",
    "LangfuseProject",
    "LangfuseSpan",
    "ModelFingerprint",
    "Mine",
    "MiningContext",
    "MiningInvalidReason",
    "MiningNomination",
    "MiningTask",
    "MiningVerdict",
    "ObservationContent",
    "ObservationContentField",
    "ObservationContentHit",
    "ObservationContentMatch",
    "ObservationContentQuery",
    "ObservationContentReference",
    "RepositorySnapshot",
    "RecoveryStatus",
    "RequiredOutcome",
    "ProcessCommand",
    "ProcessLimits",
    "RunErrorCode",
    "RunResult",
    "RunStatus",
    "Sha256Digest",
    "TaskConstraint",
    "TaskId",
    "ToolAccess",
    "ToolCapability",
    "ToolName",
    "TraceWindow",
    "TraceMiningCase",
    "VerifierResult",
    "VerifierVerdict",
    "collect",
    "get_client",
    "is_default_export_span",
    "observe",
    "ofw",
    "process_repository",
    "propagate_attributes",
    "read_observation_content",
    "read_trace_observations",
    "search_observation_content",
]
