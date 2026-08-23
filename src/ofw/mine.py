"""Deterministic trace admission and immutable Mine snapshots."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.harness import Harness
from ofw.observability.langfuse.contracts import CollectionError, TraceWindow
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionResult,
    ObservationContent,
    ObservationContentReference,
    ObservationId,
    ObservationLevel,
    ObservationRecord,
    ObservationType,
    ScoreDataType,
    ScoreId,
    ScoreRecord,
    ScoreSource,
    ScoreSubject,
    ScoreSubjectKind,
    TraceGap,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.store import CollectionStore


class TracePartition(StrEnum):
    VERIFIED_GOOD = "verified_good"
    VERIFIED_FAILURE = "verified_failure"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class MineSchemaVersion(IntEnum):
    V1 = 1


class TraceQualityThreshold(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"


class AdmissionReason(StrEnum):
    VERIFIED_PASS = "verified_pass"  # nosec B105
    VERIFIED_FAIL = "verified_fail"
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    REVISION_ATTRIBUTION = "revision_attribution"
    TRACE_QUALITY = "trace_quality"
    EXCLUDED_TRACE = "excluded_trace"


class MineErrorCode(StrEnum):
    STALE_HARNESS = "stale_harness"
    REVISION_MISMATCH = "revision_mismatch"
    INVALID_POLICY = "invalid_policy"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    CONTENT_INVALID = "content_invalid"


class MineError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: MineErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class ScoreName:
    value: str

    def __post_init__(self) -> None:
        if not self.value or "\0" in self.value:
            raise MineError(MineErrorCode.INVALID_POLICY, "empty score name")


@dataclass(frozen=True, slots=True)
class TraceTag:
    value: str

    def __post_init__(self) -> None:
        if not self.value or "\0" in self.value:
            raise MineError(MineErrorCode.INVALID_POLICY, "empty trace tag")


@dataclass(frozen=True, slots=True)
class MiningPolicy:
    critical_scores: tuple[ScoreName, ...]
    trusted_sources: tuple[ScoreSource, ...]
    quality: TraceQualityThreshold
    numeric_pass_at: float = 0.5
    excluded_tags: tuple[TraceTag, ...] = (TraceTag("ofw-internal"),)

    def __post_init__(self) -> None:
        if (
            not self.critical_scores
            or not self.trusted_sources
            or len(set(self.critical_scores)) != len(self.critical_scores)
            or len(set(self.trusted_sources)) != len(self.trusted_sources)
            or not math.isfinite(self.numeric_pass_at)
        ):
            raise MineError(MineErrorCode.INVALID_POLICY, "evidence policy is required")

    @property
    def digest(self) -> Sha256Digest:
        return _digest_text(
            "\0".join(
                (
                    *(score.value for score in self.critical_scores),
                    *(source.value for source in self.trusted_sources),
                    self.quality.value,
                    str(self.numeric_pass_at),
                    *(tag.value for tag in self.excluded_tags),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class MineRunId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TraceSnapshot:
    schema_version: MineSchemaVersion
    revision_id: HarnessRevisionId
    collection_digest: Sha256Digest
    trace: SnapshotTrace
    observations: tuple[SnapshotObservation, ...]
    scores: tuple[SnapshotScore, ...]


@dataclass(frozen=True, slots=True)
class SnapshotTrace:
    id: TraceId
    observation_ids: tuple[ObservationId, ...]
    root_observation_ids: tuple[ObservationId, ...]
    evidence_score_ids: tuple[ScoreId, ...]
    attribution: AttributionLevel
    gaps: tuple[TraceGap, ...]
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class SnapshotObservation:
    id: ObservationId
    trace_id: TraceId | None
    start_time: datetime
    end_time: datetime | None
    parent_observation_id: ObservationId | None
    type: ObservationType
    is_root: bool | None
    name: str | None
    level: ObservationLevel | None
    status_message: str | None
    digest: Sha256Digest
    input_content: SnapshotContentReference | None = None
    output_content: SnapshotContentReference | None = None


@dataclass(frozen=True, slots=True)
class SnapshotContentReference:
    content: ObservationContentReference
    path: Path


@dataclass(frozen=True, slots=True)
class SnapshotScore:
    id: ScoreId
    name: str
    value: bool | float | str
    data_type: ScoreDataType
    source: ScoreSource
    timestamp: datetime
    subject: ScoreSubject | None
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class TraceAdmission:
    trace_id: TraceId
    partition: TracePartition
    reason: AdmissionReason
    evidence_score_ids: tuple[ScoreId, ...]
    snapshot_digest: Sha256Digest | None
    snapshot_path: Path | None


@dataclass(frozen=True, slots=True)
class MineResult:
    schema_version: MineSchemaVersion
    id: MineRunId
    revision_id: HarnessRevisionId
    window: TraceWindow
    collection_digest: Sha256Digest
    policy_digest: Sha256Digest
    admissions: tuple[TraceAdmission, ...]
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "mine" / str(self.id) / "manifest.json"

    @property
    def verified_good_count(self) -> int:
        return self._count(TracePartition.VERIFIED_GOOD)

    @property
    def verified_failure_count(self) -> int:
        return self._count(TracePartition.VERIFIED_FAILURE)

    @property
    def ambiguous_count(self) -> int:
        return self._count(TracePartition.AMBIGUOUS)

    @property
    def invalid_count(self) -> int:
        return self._count(TracePartition.INVALID)

    def to_json(self) -> str:
        return _MINE_RESULT_ADAPTER.dump_json(self).decode()

    def _count(self, partition: TracePartition) -> int:
        return sum(admission.partition is partition for admission in self.admissions)


_SNAPSHOT_ADAPTER: TypeAdapter[TraceSnapshot] = TypeAdapter(TraceSnapshot)
_MINE_RESULT_ADAPTER: TypeAdapter[MineResult] = TypeAdapter(MineResult)


@dataclass(frozen=True, slots=True)
class Mine:
    source: Harness | HarnessRevision
    collection: CollectionResult
    policy: MiningPolicy

    def run(self) -> MineResult:
        revision = _resolve_revision(self.source)
        if self.collection.revision_id != revision.id:
            raise MineError(MineErrorCode.REVISION_MISMATCH, str(revision.id))
        run_id = MineRunId(
            "mine_"
            + hashlib.sha256(
                "\0".join(
                    (
                        str(revision.id),
                        str(self.collection.snapshot_digest),
                        str(self.policy.digest),
                        str(int(MineSchemaVersion.V1)),
                        self.collection.window.start.isoformat(),
                        self.collection.window.end.isoformat(),
                    )
                ).encode()
            ).hexdigest()
        )
        store = CollectionStore(self.collection.store_path)
        try:
            observations = store.observations(self.collection.observation_sync_id)
            scores = store.scores(self.collection.score_sync_id)
            admissions = tuple(
                self._admit(revision, run_id, trace, observations, scores, store)
                for trace in sorted(self.collection.traces, key=_trace_sort_key)
            )
        finally:
            store.close()
        result = MineResult(
            MineSchemaVersion.V1,
            run_id,
            revision.id,
            self.collection.window,
            self.collection.snapshot_digest,
            self.policy.digest,
            admissions,
            revision.root,
        )
        write_artifact(result.manifest_path, f"{result.to_json()}\n".encode())
        return result

    def _admit(
        self,
        revision: HarnessRevision,
        run_id: MineRunId,
        trace: TraceRecord,
        observations: tuple[ObservationRecord, ...],
        scores: tuple[ScoreRecord, ...],
        store: CollectionStore,
    ) -> TraceAdmission:
        # ponytail: linear scans are simplest for local v0; index when profiling shows pressure.
        trace_observations = tuple(
            observation for observation in observations if observation.id in trace.observation_ids
        )
        trace_scores = tuple(
            score
            for score in scores
            if score.id in trace.score_ids and _score_belongs(score, trace, trace_observations)
        )
        partition, reason, evidence = self._classify(trace, trace_observations, trace_scores)
        if partition is TracePartition.INVALID:
            return TraceAdmission(trace.id, partition, reason, evidence, None, None)
        snapshot_scores = tuple(score for score in trace_scores if score.id in evidence)
        snapshot = TraceSnapshot(
            MineSchemaVersion.V1,
            revision.id,
            self.collection.snapshot_digest,
            _snapshot_trace(trace, evidence),
            tuple(
                self._snapshot_observation(revision, run_id, store, observation)
                for observation in trace_observations
            ),
            tuple(_snapshot_score(score) for score in snapshot_scores),
        )
        payload = _SNAPSHOT_ADAPTER.dump_json(snapshot)
        digest = digest_bytes(payload)
        path = revision.root / ".ofw" / "mine" / str(run_id) / "traces" / f"{digest.value[7:]}.json"
        write_artifact(path, payload)
        return TraceAdmission(trace.id, partition, reason, evidence, digest, path)

    def _snapshot_observation(
        self,
        revision: HarnessRevision,
        run_id: MineRunId,
        store: CollectionStore,
        observation: ObservationRecord,
    ) -> SnapshotObservation:
        return _snapshot_observation(
            observation,
            self._snapshot_content(revision, run_id, store, observation.input_content),
            self._snapshot_content(revision, run_id, store, observation.output_content),
        )

    def _snapshot_content(
        self,
        revision: HarnessRevision,
        run_id: MineRunId,
        store: CollectionStore,
        reference: ObservationContentReference | None,
    ) -> SnapshotContentReference | None:
        if reference is None:
            return None
        try:
            content = store.read_content(self.collection.observation_sync_id, reference)
        except CollectionError as error:
            raise MineError(MineErrorCode.CONTENT_INVALID, str(reference.digest)) from error
        path = (
            revision.root
            / ".ofw"
            / "mine"
            / str(run_id)
            / "content"
            / f"{reference.digest.value[7:]}.txt"
        )
        write_artifact(path, content.text.encode())
        return SnapshotContentReference(reference, path)

    def _classify(
        self,
        trace: TraceRecord,
        observations: tuple[ObservationRecord, ...],
        scores: tuple[ScoreRecord, ...],
    ) -> tuple[TracePartition, AdmissionReason, tuple[ScoreId, ...]]:
        if trace.attribution is not AttributionLevel.EXACT:
            return TracePartition.INVALID, AdmissionReason.REVISION_ATTRIBUTION, ()
        if not observations or not all(
            any(observation.id == observation_id for observation in observations)
            for observation_id in trace.observation_ids
        ):
            return TracePartition.INVALID, AdmissionReason.TRACE_QUALITY, ()
        if self.policy.quality is TraceQualityThreshold.COMPLETE and trace.gaps:
            return TracePartition.INVALID, AdmissionReason.TRACE_QUALITY, ()
        if any(
            tag.value in observation.tags
            for tag in self.policy.excluded_tags
            for observation in observations
        ):
            return TracePartition.INVALID, AdmissionReason.EXCLUDED_TRACE, ()
        evidence = tuple(
            score
            for score in scores
            if score.source in self.policy.trusted_sources
            and any(score.name == name.value for name in self.policy.critical_scores)
        )
        verdicts: list[bool] = []
        missing = False
        conflicting = False
        for name in self.policy.critical_scores:
            matching = tuple(score for score in evidence if score.name == name.value)
            if not matching:
                missing = True
                continue
            resolved = tuple(
                _score_passes(score, self.policy.numeric_pass_at) for score in matching
            )
            if any(verdict is None for verdict in resolved) or len(set(resolved)) != 1:
                conflicting = True
                continue
            verdict = resolved[0]
            if verdict is not None:
                verdicts.append(verdict)
        evidence_ids = tuple(score.id for score in evidence)
        if conflicting:
            return (
                TracePartition.AMBIGUOUS,
                AdmissionReason.CONFLICTING_EVIDENCE,
                evidence_ids,
            )
        if any(not verdict for verdict in verdicts):
            return TracePartition.VERIFIED_FAILURE, AdmissionReason.VERIFIED_FAIL, evidence_ids
        if missing:
            return TracePartition.AMBIGUOUS, AdmissionReason.MISSING_EVIDENCE, evidence_ids
        return TracePartition.VERIFIED_GOOD, AdmissionReason.VERIFIED_PASS, evidence_ids


def _score_passes(score: ScoreRecord, numeric_pass_at: float) -> bool | None:
    if score.data_type is ScoreDataType.BOOLEAN and isinstance(score.value, bool):
        return score.value
    if score.data_type is ScoreDataType.NUMERIC and isinstance(score.value, float):
        return score.value >= numeric_pass_at
    return None


def _score_belongs(
    score: ScoreRecord,
    trace: TraceRecord,
    observations: tuple[ObservationRecord, ...],
) -> bool:
    subject = score.subject
    if subject is None:
        return False
    if subject.kind is ScoreSubjectKind.TRACE:
        return subject.id == trace.id.value
    if subject.kind is ScoreSubjectKind.OBSERVATION:
        return any(observation.id.value == subject.id for observation in observations) and (
            subject.trace_id is None or subject.trace_id == trace.id
        )
    if subject.kind is ScoreSubjectKind.SESSION:
        return trace.session_id is not None and subject.id == trace.session_id
    return False


def _snapshot_trace(trace: TraceRecord, evidence: tuple[ScoreId, ...]) -> SnapshotTrace:
    return SnapshotTrace(
        trace.id,
        trace.observation_ids,
        trace.root_observation_ids,
        evidence,
        trace.attribution,
        trace.gaps,
        trace.digest,
    )


def _snapshot_observation(
    observation: ObservationRecord,
    input_content: SnapshotContentReference | None,
    output_content: SnapshotContentReference | None,
) -> SnapshotObservation:
    return SnapshotObservation(
        observation.id,
        observation.trace_id,
        observation.start_time,
        observation.end_time,
        observation.parent_observation_id,
        observation.type,
        observation.is_root,
        observation.name,
        observation.level,
        observation.status_message,
        observation.digest,
        input_content,
        output_content,
    )


def _snapshot_score(score: ScoreRecord) -> SnapshotScore:
    return SnapshotScore(
        score.id,
        score.name,
        score.value,
        score.data_type,
        score.source,
        score.timestamp,
        score.subject,
        score.digest,
    )


def _trace_sort_key(trace: TraceRecord) -> str:
    return trace.id.value


def _resolve_revision(source: Harness | HarnessRevision) -> HarnessRevision:
    if isinstance(source, HarnessRevision):
        return source
    revision = source.current_revision
    if revision is None:
        raise MineError(MineErrorCode.STALE_HARNESS, source.name)
    return revision


def write_artifact(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise MineError(MineErrorCode.ARTIFACT_WRITE_FAILED, str(path)) from error


def _digest_text(value: str) -> Sha256Digest:
    return digest_bytes(value.encode())


def digest_bytes(value: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value).hexdigest()}")


def read_snapshot_content(
    result: MineResult,
    reference: SnapshotContentReference,
) -> ObservationContent:
    try:
        allowed = (
            result.root / ".ofw" / "mine" / str(result.id) / "content"
        ).resolve(strict=True)
        resolved = reference.path.resolve(strict=True)
        resolved.relative_to(allowed)
        expected_name = f"{reference.content.digest.value[7:]}.txt"
        if resolved.name != expected_name:
            raise ValueError("content path does not match digest")
        return ObservationContent(reference.content, resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MineError(MineErrorCode.CONTENT_INVALID, str(reference.path)) from error
