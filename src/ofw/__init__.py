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
    AssetAccess,
    ComponentKind,
    GitCommit,
    HarnessAsset,
    HarnessComponent,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionId,
    HarnessValidationError,
    RepositorySnapshot,
    Sha256Digest,
    WorkspaceFile,
)
from ofw.harness import EditableFile, Harness, Subagent, Tool, editable
from ofw.mine import (
    Mine,
    MineError,
    MineErrorCode,
    MiningPolicy,
    ScoreName,
    TracePartition,
    TraceQualityThreshold,
)
from ofw.observability.langfuse import (
    CollectionError,
    CollectionErrorCode,
    ContentCaptureMode,
    LangfuseProject,
    ObservationContentPolicy,
    SecretEnvironmentVariable,
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
from ofw.runtime import (
    CanaryCase,
    CaseId,
    CommandLoop,
    CommandVerifier,
    DockerCompose,
    FunctionName,
    LocalProcess,
    ModelFingerprint,
    ModuleName,
    ProcessCommand,
    ProcessLimits,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
    RunErrorCode,
    RunResult,
    RunStatus,
    ServiceName,
    VerifierResult,
    VerifierVerdict,
)


class _OfwNamespace:
    __slots__ = ()

    LocalProcess = LocalProcess
    DockerCompose = DockerCompose
    ProcessLimits = ProcessLimits
    ProcessCommand = ProcessCommand
    CommandLoop = CommandLoop
    PythonLoop = PythonLoop
    PythonEntrypoint = PythonEntrypoint
    ModuleName = ModuleName
    FunctionName = FunctionName
    ModelFingerprint = ModelFingerprint
    CommandVerifier = CommandVerifier
    PythonVerifier = PythonVerifier
    CanaryCase = CanaryCase
    CaseId = CaseId
    ServiceName = ServiceName
    MiningPolicy = MiningPolicy
    ScoreName = ScoreName

    def editable(self, path: Path) -> EditableFile:
        return editable(path)

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
    "AssetAccess",
    "CanaryCase",
    "CaseId",
    "ComponentKind",
    "CollectionError",
    "CollectionErrorCode",
    "CollectionResult",
    "CommandLoop",
    "CommandVerifier",
    "ContentCaptureMode",
    "DockerCompose",
    "EditableFile",
    "GitCommit",
    "Harness",
    "HarnessAsset",
    "HarnessComponent",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "FunctionName",
    "Langfuse",
    "LangfuseOtelSpanAttributes",
    "LangfuseProject",
    "LangfuseSpan",
    "LocalProcess",
    "ModelFingerprint",
    "ModuleName",
    "Mine",
    "MineError",
    "MineErrorCode",
    "MiningPolicy",
    "ObservationContent",
    "ObservationContentField",
    "ObservationContentHit",
    "ObservationContentMatch",
    "ObservationContentPolicy",
    "ObservationContentQuery",
    "ObservationContentReference",
    "RepositorySnapshot",
    "ProcessCommand",
    "ProcessLimits",
    "PythonEntrypoint",
    "PythonLoop",
    "PythonVerifier",
    "RunErrorCode",
    "RunResult",
    "RunStatus",
    "ScoreName",
    "Sha256Digest",
    "ServiceName",
    "SecretEnvironmentVariable",
    "Subagent",
    "Tool",
    "TraceWindow",
    "TracePartition",
    "TraceQualityThreshold",
    "VerifierResult",
    "VerifierVerdict",
    "WorkspaceFile",
    "collect",
    "editable",
    "get_client",
    "is_default_export_span",
    "observe",
    "ofw",
    "propagate_attributes",
    "read_observation_content",
    "read_trace_observations",
    "search_observation_content",
]
