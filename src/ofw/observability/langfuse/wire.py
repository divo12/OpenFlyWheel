"""Strict Langfuse v4 wire models and normalization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.contracts import (
    CollectionError,
    CollectionErrorCode,
    ContentCaptureMode,
    ObservationContentPolicy,
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
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_REDACTED_EMAIL = "[REDACTED_EMAIL]"
_REDACTED_BEARER = "Bearer [REDACTED_TOKEN]"
_REDACTED_SECRET = "[REDACTED_SECRET]"  # nosec B105
_CONTENT_NOT_CAPTURED = "[CONTENT_NOT_CAPTURED]"
_TRUNCATED = "[TRUNCATED]"


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
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

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
        policy: ObservationContentPolicy | None = None,
        redaction_values: tuple[str, ...] = (),
    ) -> ObservationPage:
        selected = policy or ObservationContentPolicy.metadata_only()
        normalized = tuple(
            _normalize_observation_with_content(record, selected, redaction_values)
            for record in self.data
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
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

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
        policy: ObservationContentPolicy | None = None,
        redaction_values: tuple[str, ...] = (),
    ) -> ScorePage:
        selected = policy or ObservationContentPolicy.metadata_only()
        return ScorePage(
            records=tuple(
                _normalize_score(record, selected, redaction_values) for record in self.data
            ),
            cursor=None if self.meta.cursor is None else PageCursor(self.meta.cursor),
        )


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    record: ObservationRecord
    input_content: ObservationContent | None
    output_content: ObservationContent | None


def _normalize_observation_with_content(
    wire: ObservationWire,
    policy: ObservationContentPolicy,
    redaction_values: tuple[str, ...],
) -> _NormalizedObservation:
    input_content = _observation_content(wire.input, policy, redaction_values)
    output_content = _observation_content(wire.output, policy, redaction_values)
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
    metadata = _revision_document(wire.metadata)
    usage = _json_document(wire.usage_details)
    costs = _json_document(wire.cost_details)
    digest_source = json.dumps(
        (
            wire.id,
            wire.trace_id,
            wire.start_time.isoformat(),
            None if wire.end_time is None else wire.end_time.isoformat(),
            wire.project_id,
            wire.parent_observation_id,
            wire.type.value,
            wire.is_root,
            wire.name,
            None if wire.level is None else wire.level.value,
            wire.status_message,
            wire.version,
            wire.environment,
            wire.bookmarked,
            wire.public,
            wire.user_id,
            wire.session_id,
            (
                None
                if wire.completion_start_time is None
                else wire.completion_start_time.isoformat()
            ),
            None if wire.created_at is None else wire.created_at.isoformat(),
            None if wire.updated_at is None else wire.updated_at.isoformat(),
            None if metadata is None else metadata.canonical,
            None if usage is None else usage.canonical,
            None if costs is None else costs.canonical,
            wire.total_cost,
            wire.usage_pricing_tier_name,
            wire.model_id,
            wire.input_price,
            wire.output_price,
            wire.total_price,
            wire.tags,
            wire.release,
            wire.trace_name,
            None if input_content is None else str(input_content.digest),
            None if output_content is None else str(output_content.digest),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
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
        model_id=wire.model_id,
        input_price=wire.input_price,
        output_price=wire.output_price,
        total_price=wire.total_price,
        input_content=input_content,
        output_content=output_content,
    )


def _observation_content(
    value: str | None,
    policy: ObservationContentPolicy,
    redaction_values: tuple[str, ...],
) -> ObservationContent | None:
    if value is None or policy.mode is ContentCaptureMode.METADATA_ONLY:
        return None
    bounded, truncated = _redact_and_bound(value, policy, redaction_values)
    reference = ObservationContentReference.for_text(bounded, truncated=truncated)
    return ObservationContent(reference, bounded)


def _redact_and_bound(
    value: str,
    policy: ObservationContentPolicy,
    redaction_values: tuple[str, ...],
) -> tuple[str, bool]:
    redacted = value
    for secret in sorted(redaction_values, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, _REDACTED_SECRET)
    redacted = _EMAIL_PATTERN.sub(_REDACTED_EMAIL, redacted)
    redacted = _BEARER_PATTERN.sub(_REDACTED_BEARER, redacted)
    return _bounded_utf8(redacted, policy.maximum_bytes_per_field)


def _bounded_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode()
    if len(encoded) <= maximum_bytes:
        return value, False
    marker = _TRUNCATED.encode()
    prefix = encoded[: maximum_bytes - len(marker)].decode(errors="ignore")
    return prefix + _TRUNCATED, True


def _unique_contents(contents: tuple[ObservationContent, ...]) -> tuple[ObservationContent, ...]:
    unique: list[ObservationContent] = []
    for content in contents:
        if all(existing.reference.digest != content.reference.digest for existing in unique):
            unique.append(content)
    return tuple(unique)


def _normalize_score(
    wire: ScoreWire,
    policy: ObservationContentPolicy,
    redaction_values: tuple[str, ...],
) -> ScoreRecord:
    _validate_score_value(wire)
    capture_details = policy.mode is ContentCaptureMode.REDACTED
    value: bool | float | str = wire.value
    if wire.data_type in (ScoreDataType.TEXT, ScoreDataType.CORRECTION):
        value = (
            _redact_and_bound(wire.value, policy, redaction_values)[0]
            if capture_details and isinstance(wire.value, str)
            else _CONTENT_NOT_CAPTURED
        )
    comment = (
        None
        if wire.comment is None or not capture_details
        else _redact_and_bound(wire.comment, policy, redaction_values)[0]
    )
    metadata_document = _json_document(wire.metadata) if capture_details else None
    metadata: JsonDocument | None = None
    if metadata_document is not None:
        bounded_metadata, truncated = _redact_and_bound(
            metadata_document.canonical,
            policy,
            redaction_values,
        )
        metadata = JsonDocument(
            json.dumps(bounded_metadata, ensure_ascii=False, separators=(",", ":"))
            if truncated
            else bounded_metadata
        )
    config_id = wire.config_id if capture_details else None
    subject = (
        None
        if wire.subject is None
        else ScoreSubject(
            kind=wire.subject.kind,
            id=wire.subject.id,
            trace_id=None if wire.subject.trace_id is None else TraceId(wire.subject.trace_id),
        )
    )
    digest_source = json.dumps(
        (
            wire.id,
            wire.project_id,
            wire.name,
            wire.data_type.value,
            value,
            wire.source.value,
            wire.timestamp.isoformat(),
            wire.environment,
            wire.created_at.isoformat(),
            wire.updated_at.isoformat(),
            comment,
            config_id,
            None if subject is None else subject.kind.value,
            None if subject is None else subject.id,
            (None if subject is None or subject.trace_id is None else subject.trace_id.value),
            None if metadata is None else metadata.canonical,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ScoreRecord(
        id=ScoreId(wire.id),
        project_id=ProjectId(wire.project_id),
        name=wire.name,
        value=value,
        data_type=wire.data_type,
        source=wire.source,
        timestamp=wire.timestamp,
        environment=wire.environment,
        created_at=wire.created_at,
        updated_at=wire.updated_at,
        comment=comment,
        metadata=metadata,
        subject=subject,
        digest=Sha256Digest(f"sha256:{hashlib.sha256(digest_source.encode()).hexdigest()}"),
        config_id=config_id,
    )


def _json_document(value: JsonValue | None) -> JsonDocument | None:
    if value is None:
        return None
    return JsonDocument(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )


def _revision_document(value: JsonValue | None) -> JsonDocument | None:
    if value is None:
        return None
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    try:
        metadata = RevisionMetadata.model_validate_json(canonical)
    except ValidationError:
        return None
    if metadata.revision_id is None:
        return None
    revision = json.dumps(metadata.revision_id, ensure_ascii=False, separators=(",", ":"))
    return JsonDocument(f'{{"ofw.harness.revision":{revision}}}')


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
