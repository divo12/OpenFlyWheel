"""Strict Langfuse v4 wire models and normalization."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.contracts import CollectionError, CollectionErrorCode
from ofw.observability.langfuse.domain import (
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
    TraceId,
    TracePayload,
)

_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class HealthWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str
    status: str

    def normalize(self) -> LangfuseHealth:
        matched = _VERSION_PATTERN.match(self.version)
        if matched is None:
            raise CollectionError(CollectionErrorCode.INVALID_RESPONSE, self.version)
        version = LangfuseServerVersion(
            major=int(matched.group(1)),
            minor=int(matched.group(2)),
            patch=int(matched.group(3)),
            raw=self.version,
        )
        if version.major != 4:
            raise CollectionError(
                CollectionErrorCode.UNSUPPORTED_LANGFUSE_VERSION,
                self.version,
            )
        return LangfuseHealth(version=version, status=self.status)


class ObservationWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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
    input: str | None = None
    output: str | None = None
    metadata: JsonValue | None = None
    usage_details: JsonValue | None = Field(default=None, alias="usageDetails")
    cost_details: JsonValue | None = Field(default=None, alias="costDetails")
    total_cost: float | None = Field(default=None, alias="totalCost")
    usage_pricing_tier_name: str | None = Field(default=None, alias="usagePricingTierName")
    tags: tuple[str, ...] | None = None
    release: str | None = None
    trace_name: str | None = Field(default=None, alias="traceName")


class ObservationMetaWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cursor: str | None = None


class ObservationResponseWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: tuple[ObservationWire, ...]
    meta: ObservationMetaWire

    def normalize(self) -> ObservationPage:
        return ObservationPage(
            records=tuple(_normalize_observation(record) for record in self.data),
            cursor=None if self.meta.cursor is None else PageCursor(self.meta.cursor),
        )


class ScoreSubjectWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ScoreSubjectKind
    id: str
    trace_id: str | None = Field(default=None, alias="traceId")


class ScoreWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    limit: int
    cursor: str | None = None


class ScoreResponseWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: tuple[ScoreWire, ...]
    meta: ScoreMetaWire

    def normalize(self) -> ScorePage:
        return ScorePage(
            records=tuple(_normalize_score(record) for record in self.data),
            cursor=None if self.meta.cursor is None else PageCursor(self.meta.cursor),
        )


def _normalize_observation(wire: ObservationWire) -> ObservationRecord:
    metadata = _json_document(wire.metadata)
    usage = _json_document(wire.usage_details)
    costs = _json_document(wire.cost_details)
    digest_source = "\0".join(
        (
            wire.id,
            wire.trace_id or "",
            wire.start_time.isoformat(),
            "" if wire.end_time is None else wire.end_time.isoformat(),
            wire.parent_observation_id or "",
            wire.type.value,
            wire.input or "",
            wire.output or "",
            "" if metadata is None else metadata.canonical,
            "" if usage is None else usage.canonical,
            "" if costs is None else costs.canonical,
            wire.release or "",
        )
    )
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
        input=None if wire.input is None else TracePayload(wire.input),
        output=None if wire.output is None else TracePayload(wire.output),
        metadata=metadata,
        usage=usage,
        costs=costs,
        total_cost=wire.total_cost,
        tags=() if wire.tags is None else wire.tags,
        release=wire.release,
        trace_name=wire.trace_name,
        digest=Sha256Digest(f"sha256:{hashlib.sha256(digest_source.encode()).hexdigest()}"),
        status_message=wire.status_message,
        bookmarked=wire.bookmarked,
        public=wire.public,
        completion_start_time=wire.completion_start_time,
        usage_pricing_tier_name=wire.usage_pricing_tier_name,
    )


def _normalize_score(wire: ScoreWire) -> ScoreRecord:
    _validate_score_value(wire)
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
    digest_source = "\0".join(
        (
            wire.id,
            wire.name,
            wire.data_type.value,
            str(wire.value),
            wire.source.value,
            wire.timestamp.isoformat(),
            "" if subject is None else subject.kind.value,
            "" if subject is None else subject.id,
            "" if metadata is None else metadata.canonical,
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
        digest=Sha256Digest(f"sha256:{hashlib.sha256(digest_source.encode()).hexdigest()}"),
        config_id=wire.config_id,
    )


def _json_document(value: JsonValue | None) -> JsonDocument | None:
    if value is None:
        return None
    return JsonDocument(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


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
