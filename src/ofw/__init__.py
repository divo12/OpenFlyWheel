"""Public OpenFlyWheel harness API."""

from pathlib import Path

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
from ofw.observability.langfuse import (
    CollectionError,
    CollectionErrorCode,
    EnvironmentName,
    LangfuseBaseUrl,
    LangfuseConnectionId,
    LangfuseConnectionManifest,
    LangfuseProject,
    LangfuseProjectMode,
    SecretEnvironmentVariable,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapability,
    CollectionCapabilityReason,
    CollectionResult,
    CollectionSyncId,
    JsonDocument,
    LangfuseHealth,
    LangfuseServerVersion,
    ObservationId,
    ObservationLevel,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    PageCursor,
    ProjectId,
    ScoreDataType,
    ScoreId,
    ScorePage,
    ScoreRecord,
    ScoreSource,
    ScoreSubject,
    ScoreSubjectKind,
    TraceGap,
    TraceId,
    TracePayload,
    TraceRecord,
)
from ofw.observability.langfuse.service import collect


class _OfwNamespace:
    __slots__ = ()

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
    "AttributionLevel",
    "ComponentKind",
    "CollectionError",
    "CollectionErrorCode",
    "CollectionCapability",
    "CollectionCapabilityReason",
    "CollectionResult",
    "CollectionSyncId",
    "EditableFile",
    "EnvironmentName",
    "JsonDocument",
    "LangfuseHealth",
    "GitCommit",
    "Harness",
    "HarnessAsset",
    "HarnessComponent",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "LangfuseBaseUrl",
    "LangfuseConnectionId",
    "LangfuseConnectionManifest",
    "LangfuseProject",
    "LangfuseProjectMode",
    "LangfuseServerVersion",
    "ObservationId",
    "ObservationLevel",
    "ObservationPage",
    "ObservationRecord",
    "ObservationType",
    "PageCursor",
    "ProjectId",
    "RepositorySnapshot",
    "Sha256Digest",
    "SecretEnvironmentVariable",
    "ScoreDataType",
    "ScoreId",
    "ScorePage",
    "ScoreRecord",
    "ScoreSource",
    "ScoreSubject",
    "ScoreSubjectKind",
    "Subagent",
    "Tool",
    "TraceWindow",
    "TraceId",
    "TraceGap",
    "TracePayload",
    "TraceRecord",
    "WorkspaceFile",
    "collect",
    "editable",
    "ofw",
]
