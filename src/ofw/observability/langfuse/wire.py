"""Strict Langfuse v4 wire models and normalization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
)
from ofw.observability.langfuse.domain import (
    JsonDocument,
    ObservationContent,
    ObservationContentReference,
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
    TraceId,
)

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class HealthWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    version: str
    status: str

    def validate_server_version(self) -> None:
        matched = _VERSION_PATTERN.match(self.version)
        if matched is None:
            raise CollectionError(CollectionErrorCode.INVALID_RESPONSE, self.version)
        if int(matched.group(1)) != 4:
            raise CollectionError(
                CollectionErrorCode.UNSUPPORTED_LANGFUSE_VERSION,
                self.version,
            )


class ObservationWire(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    trace_id: str | None = Field(alias="traceId")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime | None = Field(alias="endTime")
    project_id: str = Field(alias="projectId")
    parent_observation_id: str | None = Field(alias="parentObservationId")
    type: ObservationType
    is_root: bool | None = Field(default=None, alias="isRootObservation")
    name: str | None = None
    level: ObservationLevel | None = None
    status_message: str | None = Field(default=None, alias="statusMessage")
    version: str | None = None
    environment: str | None = None
    bookmarked: bool | None = None
    public: bool | None = None
    user_id: str | None = Field(default=None, alias="userId")
    session_id: str | None = Field(default=None, alias="sessionId")
    completion_start_time: datetime | None = Field(default=None, alias="completionStartTime")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")
    metadata: JsonValue | None = None
    usage_details: JsonValue | None = Field(default=None, alias="usageDetails")
    cost_details: JsonValue | None = Field(default=None, alias="costDetails")
    total_cost: float | None = Field(default=None, alias="totalCost")
    usage_pricing_tier_name: str | None = Field(default=None, alias="usagePricingTierName")
    model_id: str | None = Field(default=None, alias="modelId")
    input_price: str | None = Field(default=None, alias="inputPrice")
    output_price: str | None = Field(default=None, alias="outputPrice")
    total_price: str | None = Field(default=None, alias="totalPrice")
    tags: tuple[str, ...] | None = None
    release: str | None = None
    trace_name: str | None = Field(default=None, alias="traceName")
    input: str | None = None
    output: str | None = None


class ObservationMetaWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    cursor: str | None = None


class ObservationResponseWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    data: tuple[ObservationWire, ...]
    meta: ObservationMetaWire

    def normalize(
        self,
    ) -> ObservationPage:
        normalized = tuple(
            _normalize_observation_with_content(record) for record in self.data
        )
        return ObservationPage(
            records=tuple(item.record for item in normalized),
            cursor=None if self.meta.cursor is None else PageCursor(self.meta.cursor),
            contents=_unique_contents(
                tuple(
                    content
                    for item in normalized
                    for content in (item.input_content, item.output_content)
                    if content is not None
                )
            ),
        )


class RevisionMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    revision_id: str | None = Field(default=None, alias="ofw.harness.revision")


class ScoreSubjectWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    kind: ScoreSubjectKind
    id: str
    trace_id: str | None = Field(default=None, alias="traceId")


class ScoreWire(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    project_id: str = Field(alias="projectId")
    name: str
    value: bool | float | str
    data_type: ScoreDataType = Field(alias="dataType")
    source: ScoreSource
    timestamp: datetime
    environment: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    comment: str | None = None
    config_id: str | None = Field(default=None, alias="configId")
    metadata: JsonValue | None = None
    author_user_id: str | None = Field(default=None, alias="authorUserId")
    queue_id: str | None = Field(default=None, alias="queueId")
    subject: ScoreSubjectWire | None = None


class ScoreMetaWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    limit: int | None = None
    cursor: str | None = None


class ScoreResponseWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    data: tuple[ScoreWire, ...]
    meta: ScoreMetaWire

    def normalize(
        self,
    ) -> ScorePage:
        return ScorePage(
            records=tuple(_normalize_score(record) for record in self.data),
            cursor=None if self.meta.cursor is None else PageCursor(self.meta.cursor),
        )


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    record: ObservationRecord
    input_content: ObservationContent | None
    output_content: ObservationContent | None


def _normalize_observation_with_content(
    wire: ObservationWire,
) -> _NormalizedObservation:
    input_content = _observation_content(wire.input)
    output_content = _observation_content(wire.output)
    return _NormalizedObservation(
        _normalize_observation(
            wire,
            None if input_content is None else input_content.reference,
            None if output_content is None else output_content.reference,
        ),
        input_content,
        output_content,
    )


def _normalize_observation(
    wire: ObservationWire,
    input_content: ObservationContentReference | None,
    output_content: ObservationContentReference | None,
) -> ObservationRecord:
    raw = _wire_document(wire)
    metadata = _json_document(wire.metadata)
    usage = _json_document(wire.usage_details)
    costs = _json_document(wire.cost_details)
    return ObservationRecord(
        id=ObservationId(wire.id),
        trace_id=None if wire.trace_id is None else TraceId(wire.trace_id),
        start_time=wire.start_time,
        end_time=wire.end_time,
        project_id=ProjectId(wire.project_id),
        parent_observation_id=(
            None
            if wire.parent_observation_id is None
            else ObservationId(wire.parent_observation_id)
        ),
        type=wire.type,
        is_root=wire.is_root,
        name=wire.name,
        level=wire.level,
        version=wire.version,
        environment=wire.environment,
        user_id=wire.user_id,
        session_id=wire.session_id,
        created_at=wire.created_at,
        updated_at=wire.updated_at,
        metadata=metadata,
        usage=usage,
        costs=costs,
        total_cost=wire.total_cost,
        tags=() if wire.tags is None else wire.tags,
        release=wire.release,
        trace_name=wire.trace_name,
        raw=raw,
        digest=Sha256Digest(f"sha256:{hashlib.sha256(raw.canonical.encode()).hexdigest()}"),
        status_message=wire.status_message,
        bookmarked=wire.bookmarked,
        public=wire.public,
        completion_start_time=wire.completion_start_time,
        usage_pricing_tier_name=wire.usage_pricing_tier_name,
        model_id=wire.model_id,
        input_price=wire.input_price,
        output_price=wire.output_price,
        total_price=wire.total_price,
        input_content=input_content,
        output_content=output_content,
    )


def _observation_content(value: str | None) -> ObservationContent | None:
    if value is None:
        return None
    return ObservationContent(ObservationContentReference.for_text(value), value)


def _unique_contents(contents: tuple[ObservationContent, ...]) -> tuple[ObservationContent, ...]:
    unique: list[ObservationContent] = []
    for content in contents:
        if all(existing.reference.digest != content.reference.digest for existing in unique):
            unique.append(content)
    return tuple(unique)


def _normalize_score(wire: ScoreWire) -> ScoreRecord:
    _validate_score_value(wire)
    raw = _wire_document(wire)
    metadata = _json_document(wire.metadata)
    subject = (
        None
        if wire.subject is None
        else ScoreSubject(
            kind=wire.subject.kind,
            id=wire.subject.id,
            trace_id=None if wire.subject.trace_id is None else TraceId(wire.subject.trace_id),
        )
    )
    return ScoreRecord(
        id=ScoreId(wire.id),
        project_id=ProjectId(wire.project_id),
        name=wire.name,
        value=wire.value,
        data_type=wire.data_type,
        source=wire.source,
        timestamp=wire.timestamp,
        environment=wire.environment,
        created_at=wire.created_at,
        updated_at=wire.updated_at,
        comment=wire.comment,
        metadata=metadata,
        subject=subject,
        raw=raw,
        digest=Sha256Digest(f"sha256:{hashlib.sha256(raw.canonical.encode()).hexdigest()}"),
        config_id=wire.config_id,
    )


def _json_document(value: JsonValue | None) -> JsonDocument | None:
    if value is None:
        return None
    return _canonical_document(value)


def _canonical_document(value: JsonValue) -> JsonDocument:
    return JsonDocument(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def _wire_document(wire: BaseModel) -> JsonDocument:
    dumped = cast(object, wire.model_dump(mode="json", by_alias=True, exclude_none=False))
    return _canonical_document(_JSON_OBJECT_ADAPTER.validate_python(dumped))


def _validate_score_value(wire: ScoreWire) -> None:
    valid = (
        (wire.data_type is ScoreDataType.BOOLEAN and isinstance(wire.value, bool))
        or (
            wire.data_type is ScoreDataType.NUMERIC
            and isinstance(wire.value, float)
            and not isinstance(wire.value, bool)
        )
        or (
            wire.data_type
            in (ScoreDataType.CATEGORICAL, ScoreDataType.TEXT, ScoreDataType.CORRECTION)
            and isinstance(wire.value, str)
        )
    )
    if not valid:
        raise CollectionError(CollectionErrorCode.INVALID_RESPONSE, wire.id)
