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

from ofw.benchmarking import (
    Baseline,
    BenchmarkError,
    BenchmarkErrorCode,
    BenchmarkPolicy,
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkStatus,
)
from ofw.candidate import (
    CandidateBuilder,
    CandidateError,
    CandidateErrorCode,
    CandidateEvidence,
    CandidatePolicy,
    ChangePrediction,
    FileEdit,
    LineRange,
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
from ofw.diagnosis import (
    ClusterId,
    ClusterRevisionRef,
    ClusterState,
    DiagnosisError,
    DiagnosisErrorCode,
    DiagnosisResult,
    DiagnosisRun,
    EvidenceAnchor,
    EvidenceAnchorKind,
    FailureCluster,
    MechanismKey,
    PythonDiagnoser,
    Severity,
    TraceDiagnosis,
)
from ofw.exports import (
    ClusterFamilyId,
    ClusterPartitionRule,
    ConsentStatus,
    DataLicense,
    ExportBundle,
    ExportPartition,
    ExportPolicy,
    LeakageError,
    LeakageErrorCode,
    MineExports,
    PrivacyTransform,
)
from ofw.fit import (
    CandidateOutcome,
    CandidateStatus,
    CaseDelta,
    FitCampaign,
    FitError,
    FitErrorCode,
    FitPolicy,
    FitResult,
    GateReason,
)
from ofw.harness import EditableFile, Harness, Subagent, Tool, editable
from ofw.mine import (
    Mine,
    MineError,
    MineErrorCode,
    MineResult,
    MiningPolicy,
    ScoreName,
    SnapshotContentReference,
    TracePartition,
    TraceQualityThreshold,
    read_snapshot_content,
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
    MetricKind,
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
    PythonDiagnoser = PythonDiagnoser
    ExportPolicy = ExportPolicy
    BenchmarkPolicy = BenchmarkPolicy
    BenchmarkRunner = BenchmarkRunner
    CandidatePolicy = CandidatePolicy
    CandidateBuilder = CandidateBuilder
    FitPolicy = FitPolicy

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

    def read_snapshot_content(
        self,
        result: MineResult,
        reference: SnapshotContentReference,
    ) -> ObservationContent:
        return read_snapshot_content(result, reference)


ofw = _OfwNamespace()

__all__ = [
    "AssetAccess",
    "Baseline",
    "BenchmarkError",
    "BenchmarkErrorCode",
    "BenchmarkPolicy",
    "BenchmarkResult",
    "BenchmarkRunner",
    "BenchmarkStatus",
    "CandidateBuilder",
    "CandidateError",
    "CandidateErrorCode",
    "CandidateEvidence",
    "CandidatePolicy",
    "CandidateOutcome",
    "CandidateStatus",
    "CaseDelta",
    "CanaryCase",
    "CaseId",
    "ClusterPartitionRule",
    "ClusterFamilyId",
    "ClusterId",
    "ClusterState",
    "ChangePrediction",
    "ConsentStatus",
    "ClusterRevisionRef",
    "ComponentKind",
    "CollectionError",
    "CollectionErrorCode",
    "CollectionResult",
    "CommandLoop",
    "CommandVerifier",
    "ContentCaptureMode",
    "DockerCompose",
    "DiagnosisResult",
    "DiagnosisRun",
    "DataLicense",
    "DiagnosisError",
    "DiagnosisErrorCode",
    "EditableFile",
    "EvidenceAnchor",
    "EvidenceAnchorKind",
    "ExportBundle",
    "ExportPartition",
    "ExportPolicy",
    "FailureCluster",
    "FileEdit",
    "FitCampaign",
    "FitError",
    "FitErrorCode",
    "FitPolicy",
    "FitResult",
    "GitCommit",
    "Harness",
    "HarnessAsset",
    "HarnessComponent",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "FunctionName",
    "GateReason",
    "Langfuse",
    "LangfuseOtelSpanAttributes",
    "LangfuseProject",
    "LangfuseSpan",
    "LeakageError",
    "LeakageErrorCode",
    "LineRange",
    "LocalProcess",
    "ModelFingerprint",
    "ModuleName",
    "Mine",
    "MineError",
    "MineErrorCode",
    "MineExports",
    "MiningPolicy",
    "MechanismKey",
    "MetricKind",
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
    "PrivacyTransform",
    "PythonEntrypoint",
    "PythonLoop",
    "PythonDiagnoser",
    "PythonVerifier",
    "RunErrorCode",
    "RunResult",
    "RunStatus",
    "ScoreName",
    "Sha256Digest",
    "ServiceName",
    "Severity",
    "SecretEnvironmentVariable",
    "SnapshotContentReference",
    "Subagent",
    "Tool",
    "TraceWindow",
    "TracePartition",
    "TraceQualityThreshold",
    "TraceDiagnosis",
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
    "read_snapshot_content",
    "read_trace_observations",
    "search_observation_content",
]
