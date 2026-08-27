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
from ofw.evaluation import (
    LangfuseOutcomeStore,
    OutcomeErrorCode,
    OutcomeEvaluation,
    OutcomeEvaluationError,
    OutcomeScoreSubmission,
    TaskId,
    VerifierId,
)
from ofw.harness import EditableFile, Harness, Subagent, Tool, editable
from ofw.observability.langfuse import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)
from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import (
    CanaryCase,
    CaseId,
    CommandLoop,
    CommandVerifier,
    E2BSandbox,
    EvidenceReference,
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

    def editable(self, path: Path) -> EditableFile:
        return editable(path)


ofw = _OfwNamespace()

__all__ = [
    "AssetAccess",
    "CanaryCase",
    "CaseId",
    "ComponentKind",
    "CollectionError",
    "CollectionErrorCode",
    "CommandLoop",
    "CommandVerifier",
    "E2BSandbox",
    "EditableFile",
    "EvidenceReference",
    "GitCommit",
    "Harness",
    "HarnessAsset",
    "HarnessComponent",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "Langfuse",
    "LangfuseOutcomeStore",
    "LangfuseOtelSpanAttributes",
    "LangfuseProject",
    "LangfuseSpan",
    "ModelFingerprint",
    "OutcomeErrorCode",
    "OutcomeEvaluation",
    "OutcomeEvaluationError",
    "OutcomeScoreSubmission",
    "RepositorySnapshot",
    "ProcessCommand",
    "ProcessLimits",
    "RunErrorCode",
    "RunResult",
    "RunStatus",
    "Sha256Digest",
    "Subagent",
    "TaskId",
    "Tool",
    "TraceId",
    "TraceWindow",
    "VerifierResult",
    "VerifierVerdict",
    "VerifierId",
    "WorkspaceFile",
    "editable",
    "get_client",
    "is_default_export_span",
    "observe",
    "ofw",
    "propagate_attributes",
]
