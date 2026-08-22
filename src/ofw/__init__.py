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
from ofw.diagnosis import (
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
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import CollectionResult
from ofw.observability.langfuse.service import collect
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
    PythonDiagnoser = PythonDiagnoser

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


ofw = _OfwNamespace()

__all__ = [
    "AssetAccess",
    "CanaryCase",
    "CaseId",
    "ClusterState",
    "ClusterRevisionRef",
    "ComponentKind",
    "CollectionError",
    "CollectionErrorCode",
    "CollectionResult",
    "CommandLoop",
    "CommandVerifier",
    "DockerCompose",
    "DiagnosisResult",
    "DiagnosisRun",
    "DiagnosisError",
    "DiagnosisErrorCode",
    "EditableFile",
    "EvidenceAnchor",
    "EvidenceAnchorKind",
    "FailureCluster",
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
    "MechanismKey",
    "RepositorySnapshot",
    "ProcessCommand",
    "ProcessLimits",
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
]
