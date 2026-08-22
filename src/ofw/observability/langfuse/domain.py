"""Immutable normalized Langfuse observation and score records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    LangfuseConnectionId,
    TraceWindow,
)

COLLECTION_POLICY_VERSION = 1


class ObservationType(StrEnum):
    GENERATION = "GENERATION"
    SPAN = "SPAN"
    EVENT = "EVENT"
    AGENT = "AGENT"
    TOOL = "TOOL"
    CHAIN = "CHAIN"
    RETRIEVER = "RETRIEVER"
    EVALUATOR = "EVALUATOR"
    EMBEDDING = "EMBEDDING"
    GUARDRAIL = "GUARDRAIL"


class ObservationLevel(StrEnum):
    DEBUG = "DEBUG"
    DEFAULT = "DEFAULT"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ScoreDataType(StrEnum):
    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    CATEGORICAL = "CATEGORICAL"
    TEXT = "TEXT"
    CORRECTION = "CORRECTION"


class ScoreSource(StrEnum):
    API = "API"
    ANNOTATION = "ANNOTATION"
    EVAL = "EVAL"


class ScoreSubjectKind(StrEnum):
    TRACE = "trace"
    OBSERVATION = "observation"
    SESSION = "session"
    EXPERIMENT = "experiment"


class SyncStream(StrEnum):
    OBSERVATIONS = "observations"
    SCORES = "scores"


class AttributionLevel(StrEnum):
    EXACT = "exact"
    RELEASE = "release"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class TraceGap(StrEnum):
    MISSING_ROOT = "missing_root"
    MISSING_PARENT = "missing_parent"
    MULTIPLE_SESSIONS = "multiple_sessions"
    MULTIPLE_ENVIRONMENTS = "multiple_environments"
    MULTIPLE_RELEASES = "multiple_releases"


class CollectionCapabilityReason(StrEnum):
    READY = "ready"
    NO_TRACES = "no_traces"
    MISSING_REVISION_ATTRIBUTION = "missing_revision_attribution"
    AMBIGUOUS_REVISION_ATTRIBUTION = "ambiguous_revision_attribution"
    INCOMPLETE_TRACE = "incomplete_trace"


@dataclass(frozen=True, slots=True)
class ObservationId:
    value: str


@dataclass(frozen=True, slots=True)
class TraceId:
    value: str


@dataclass(frozen=True, slots=True)
class ProjectId:
    value: str


@dataclass(frozen=True, slots=True)
class ScoreId:
    value: str


@dataclass(frozen=True, slots=True)
class PageCursor:
    value: str


@dataclass(frozen=True, slots=True)
class CollectionSyncId:
    value: str

    @classmethod
    def for_collection(
        cls,
        revision: HarnessRevision,
        window: TraceWindow,
        stream: SyncStream,
    ) -> CollectionSyncId:
        if revision.observability is None:
            raise CollectionError(
                CollectionErrorCode.OBSERVABILITY_NOT_CONNECTED,
                str(revision.id),
            )
        payload = "\0".join(
            (
                str(COLLECTION_POLICY_VERSION),
                str(revision.id),
                str(revision.observability.id),
                window.start.isoformat(),
                window.end.isoformat(),
                stream.value,
            )
        )
        return cls(f"sync_{hashlib.sha256(payload.encode()).hexdigest()}")


@dataclass(frozen=True, slots=True)
class TracePayload:
    raw: str


@dataclass(frozen=True, slots=True)
class JsonDocument:
    canonical: str


@dataclass(frozen=True, slots=True)
class LangfuseServerVersion:
    major: int
    minor: int
    patch: int
    raw: str


@dataclass(frozen=True, slots=True)
class LangfuseHealth:
    version: LangfuseServerVersion
    status: str


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    id: ObservationId
    trace_id: TraceId | None
    start_time: datetime
    end_time: datetime | None
    project_id: ProjectId
    parent_observation_id: ObservationId | None
    type: ObservationType
    is_root: bool | None
    name: str | None
    level: ObservationLevel | None
    version: str | None
    environment: str | None
    user_id: str | None
    session_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    input: TracePayload | None
    output: TracePayload | None
    metadata: JsonDocument | None
    usage: JsonDocument | None
    costs: JsonDocument | None
    total_cost: float | None
    tags: tuple[str, ...]
    release: str | None
    trace_name: str | None
    digest: Sha256Digest
    status_message: str | None = None
    bookmarked: bool | None = None
    public: bool | None = None
    completion_start_time: datetime | None = None
    usage_pricing_tier_name: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreSubject:
    kind: ScoreSubjectKind
    id: str
    trace_id: TraceId | None


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    id: ScoreId
    project_id: ProjectId
    name: str
    value: bool | float | str
    data_type: ScoreDataType
    source: ScoreSource
    timestamp: datetime
    environment: str
    created_at: datetime
    updated_at: datetime
    comment: str | None
    metadata: JsonDocument | None
    subject: ScoreSubject | None
    digest: Sha256Digest
    config_id: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationPage:
    records: tuple[ObservationRecord, ...]
    cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class ScorePage:
    records: tuple[ScoreRecord, ...]
    cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class SyncCheckpoint:
    sync_id: CollectionSyncId
    stream: SyncStream
    cursor: PageCursor | None
    complete: bool
    page_count: int
    state_version: int


@dataclass(frozen=True, slots=True)
class TraceRecord:
    id: TraceId
    observation_ids: tuple[ObservationId, ...]
    root_observation_ids: tuple[ObservationId, ...]
    score_ids: tuple[ScoreId, ...]
    session_id: str | None
    environment: str | None
    release: str | None
    attribution: AttributionLevel
    gaps: tuple[TraceGap, ...]
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class CollectionCapability:
    collection_ready: bool
    mine_ready: bool
    automatic_fit_ready: bool
    reason: CollectionCapabilityReason


@dataclass(frozen=True, slots=True)
class CollectionResult:
    revision_id: HarnessRevisionId
    connection_id: LangfuseConnectionId
    window: TraceWindow
    observation_sync_id: CollectionSyncId
    score_sync_id: CollectionSyncId
    traces: tuple[TraceRecord, ...]
    observation_count: int
    score_count: int
    gap_count: int
    snapshot_digest: Sha256Digest
    capability: CollectionCapability
    store_path: Path
