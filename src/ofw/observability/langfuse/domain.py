"""Immutable normalized Langfuse observation and score records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ofw.contracts import Sha256Digest


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
