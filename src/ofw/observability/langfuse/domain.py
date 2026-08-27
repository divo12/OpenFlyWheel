"""Immutable normalized Langfuse observation and score records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ofw.contracts import HarnessRevisionId, Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    LangfuseConnectionId,
    LangfuseConnectionManifest,
    TraceWindow,
)


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


class AttributionLevel(StrEnum):
    EXACT = "exact"
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


class ObservationContentField(StrEnum):
    ANY = "any"
    INPUT = "input"
    OUTPUT = "output"


class ObservationContentMatch(StrEnum):
    EXACT = "exact"
    TOKEN_PHRASE = "token_phrase"  # nosec B105


@dataclass(frozen=True, slots=True)
class ObservationId:
    value: str


@dataclass(frozen=True, slots=True)
class TraceId:
    value: str


@dataclass(frozen=True, slots=True)
class SessionId:
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
class JsonDocument:
    canonical: str


@dataclass(frozen=True, slots=True)
class ObservationContentReference:
    digest: Sha256Digest
    byte_count: int

    @classmethod
    def for_text(cls, text: str) -> ObservationContentReference:
        encoded = text.encode()
        return cls(
            Sha256Digest(f"sha256:{hashlib.sha256(encoded).hexdigest()}"),
            len(encoded),
        )


@dataclass(frozen=True, slots=True)
class ObservationContent:
    reference: ObservationContentReference
    text: str

    def __post_init__(self) -> None:
        expected = ObservationContentReference.for_text(self.text)
        if self.reference != expected:
            raise ValueError("observation content reference mismatch")


@dataclass(frozen=True, slots=True)
class ObservationContentQuery:
    text: str
    match: ObservationContentMatch
    field: ObservationContentField
    trace_id: TraceId | None
    limit: int
    maximum_excerpt_characters: int

    def __post_init__(self) -> None:
        if (
            not self.text.strip()
            or len(self.text) > 1024
            or not isinstance(self.match, ObservationContentMatch)
            or not isinstance(self.field, ObservationContentField)
            or not 1 <= self.limit <= 100
            or not 1 <= self.maximum_excerpt_characters <= 4096
        ):
            raise CollectionError(CollectionErrorCode.INVALID_CONTENT_QUERY, self.text)


@dataclass(frozen=True, slots=True)
class ObservationContentHit:
    observation_id: ObservationId
    trace_id: TraceId | None
    field: ObservationContentField
    reference: ObservationContentReference
    excerpt: str


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
    metadata: JsonDocument | None
    usage: JsonDocument | None
    costs: JsonDocument | None
    total_cost: float | None
    tags: tuple[str, ...]
    release: str | None
    trace_name: str | None
    raw: JsonDocument
    digest: Sha256Digest
    status_message: str | None = None
    bookmarked: bool | None = None
    public: bool | None = None
    completion_start_time: datetime | None = None
    usage_pricing_tier_name: str | None = None
    model_id: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    total_price: str | None = None
    input_content: ObservationContentReference | None = None
    output_content: ObservationContentReference | None = None


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
    raw: JsonDocument
    digest: Sha256Digest
    config_id: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationPage:
    records: tuple[ObservationRecord, ...]
    cursor: PageCursor | None
    contents: tuple[ObservationContent, ...] = ()


@dataclass(frozen=True, slots=True)
class ScorePage:
    records: tuple[ScoreRecord, ...]
    cursor: PageCursor | None


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
class SessionRecord:
    id: SessionId
    trace_ids: tuple[TraceId, ...]
    observation_ids: tuple[ObservationId, ...]
    score_ids: tuple[ScoreId, ...]
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class CollectionResult:
    revision_id: HarnessRevisionId
    connection: LangfuseConnectionManifest
    window: TraceWindow
    observations: tuple[ObservationRecord, ...]
    contents: tuple[ObservationContent, ...]
    scores: tuple[ScoreRecord, ...]
    traces: tuple[TraceRecord, ...]
    sessions: tuple[SessionRecord, ...]
    gap_count: int
    snapshot_digest: Sha256Digest
    capability: CollectionCapabilityReason

    @property
    def connection_id(self) -> LangfuseConnectionId:
        return self.connection.id

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def score_count(self) -> int:
        return len(self.scores)
