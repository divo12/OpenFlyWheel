"""Typed JSON persistence models for normalized Langfuse records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ofw.contracts import Sha256Digest
from ofw.observability.langfuse.domain import (
    JsonDocument,
    ObservationId,
    ObservationLevel,
    ObservationRecord,
    ObservationType,
    ProjectId,
    ScoreDataType,
    ScoreId,
    ScoreRecord,
    ScoreSource,
    ScoreSubject,
    ScoreSubjectKind,
    TraceId,
    TracePayload,
)


class ObservationStorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    trace_id: str | None
    start_time: datetime
    end_time: datetime | None
    project_id: str
    parent_observation_id: str | None
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
    input: str | None
    output: str | None
    metadata: str | None
    usage: str | None
    costs: str | None
    total_cost: float | None
    tags: tuple[str, ...]
    release: str | None
    trace_name: str | None
    digest: str
    status_message: str | None = None
    bookmarked: bool | None = None
    public: bool | None = None
    completion_start_time: datetime | None = None
    usage_pricing_tier_name: str | None = None

    @classmethod
    def from_record(cls, record: ObservationRecord) -> ObservationStorageModel:
        return cls(
            id=record.id.value,
            trace_id=None if record.trace_id is None else record.trace_id.value,
            start_time=record.start_time,
            end_time=record.end_time,
            project_id=record.project_id.value,
            parent_observation_id=(
                None if record.parent_observation_id is None else record.parent_observation_id.value
            ),
            type=record.type,
            is_root=record.is_root,
            name=record.name,
            level=record.level,
            version=record.version,
            environment=record.environment,
            user_id=record.user_id,
            session_id=record.session_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            input=None if record.input is None else record.input.raw,
            output=None if record.output is None else record.output.raw,
            metadata=None if record.metadata is None else record.metadata.canonical,
            usage=None if record.usage is None else record.usage.canonical,
            costs=None if record.costs is None else record.costs.canonical,
            total_cost=record.total_cost,
            tags=record.tags,
            release=record.release,
            trace_name=record.trace_name,
            digest=str(record.digest),
            status_message=record.status_message,
            bookmarked=record.bookmarked,
            public=record.public,
            completion_start_time=record.completion_start_time,
            usage_pricing_tier_name=record.usage_pricing_tier_name,
        )

    def to_record(self) -> ObservationRecord:
        return ObservationRecord(
            id=ObservationId(self.id),
            trace_id=None if self.trace_id is None else TraceId(self.trace_id),
            start_time=self.start_time,
            end_time=self.end_time,
            project_id=ProjectId(self.project_id),
            parent_observation_id=(
                None
                if self.parent_observation_id is None
                else ObservationId(self.parent_observation_id)
            ),
            type=self.type,
            is_root=self.is_root,
            name=self.name,
            level=self.level,
            version=self.version,
            environment=self.environment,
            user_id=self.user_id,
            session_id=self.session_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            input=None if self.input is None else TracePayload(self.input),
            output=None if self.output is None else TracePayload(self.output),
            metadata=None if self.metadata is None else JsonDocument(self.metadata),
            usage=None if self.usage is None else JsonDocument(self.usage),
            costs=None if self.costs is None else JsonDocument(self.costs),
            total_cost=self.total_cost,
            tags=self.tags,
            release=self.release,
            trace_name=self.trace_name,
            digest=Sha256Digest(self.digest),
            status_message=self.status_message,
            bookmarked=self.bookmarked,
            public=self.public,
            completion_start_time=self.completion_start_time,
            usage_pricing_tier_name=self.usage_pricing_tier_name,
        )


class ScoreStorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    project_id: str
    name: str
    value: bool | float | str
    data_type: ScoreDataType
    source: ScoreSource
    timestamp: datetime
    environment: str
    created_at: datetime
    updated_at: datetime
    comment: str | None
    metadata: str | None
    subject_kind: ScoreSubjectKind | None
    subject_id: str | None
    subject_trace_id: str | None
    digest: str
    config_id: str | None = None

    @classmethod
    def from_record(cls, record: ScoreRecord) -> ScoreStorageModel:
        return cls(
            id=record.id.value,
            project_id=record.project_id.value,
            name=record.name,
            value=record.value,
            data_type=record.data_type,
            source=record.source,
            timestamp=record.timestamp,
            environment=record.environment,
            created_at=record.created_at,
            updated_at=record.updated_at,
            comment=record.comment,
            metadata=None if record.metadata is None else record.metadata.canonical,
            subject_kind=None if record.subject is None else record.subject.kind,
            subject_id=None if record.subject is None else record.subject.id,
            subject_trace_id=(
                None
                if record.subject is None or record.subject.trace_id is None
                else record.subject.trace_id.value
            ),
            digest=str(record.digest),
            config_id=record.config_id,
        )

    def to_record(self) -> ScoreRecord:
        subject = (
            None
            if self.subject_kind is None or self.subject_id is None
            else ScoreSubject(
                self.subject_kind,
                self.subject_id,
                None if self.subject_trace_id is None else TraceId(self.subject_trace_id),
            )
        )
        return ScoreRecord(
            id=ScoreId(self.id),
            project_id=ProjectId(self.project_id),
            name=self.name,
            value=self.value,
            data_type=self.data_type,
            source=self.source,
            timestamp=self.timestamp,
            environment=self.environment,
            created_at=self.created_at,
            updated_at=self.updated_at,
            comment=self.comment,
            metadata=None if self.metadata is None else JsonDocument(self.metadata),
            subject=subject,
            digest=Sha256Digest(self.digest),
            config_id=self.config_id,
        )
