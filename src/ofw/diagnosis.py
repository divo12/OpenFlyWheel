"""Evidence-bound failure diagnosis and deterministic cluster revisions."""

from __future__ import annotations

import hashlib
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ofw.contracts import (
    ComponentKind,
    HarnessAsset,
    HarnessRevision,
    HarnessRevisionId,
    Sha256Digest,
)
from ofw.harness import Harness
from ofw.mine import (
    MineResult,
    MineRunId,
    TraceAdmission,
    TracePartition,
    TraceSnapshot,
    digest_bytes,
    write_artifact,
)
from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import (
    CanaryCase,
    CaseId,
    LocalProcess,
    ModelFingerprint,
    PreparedEnvironment,
    ProcessCommand,
    ProcessLimits,
    PythonEntrypoint,
    resolve_python_source,
)


class DiagnosisSchemaVersion(IntEnum):
    V1 = 1


class EvidenceAnchorKind(StrEnum):
    OBSERVATION = "observation"
    SCORE = "score"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ClusterState(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    TARGETED = "targeted"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    REJECTED = "rejected"


class ClusterReviewDecision(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"


class DiagnosisStatus(StrEnum):
    PROPOSED = "proposed"
    ABSTAINED = "abstained"


class DiagnosisErrorCode(StrEnum):
    STALE_HARNESS = "stale_harness"
    REVISION_MISMATCH = "revision_mismatch"
    ARTIFACT_INVALID = "artifact_invalid"
    REVIEW_INVALID = "review_invalid"


class DiagnosisError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: DiagnosisErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class MechanismKey:
    value: str

    def __post_init__(self) -> None:
        if not self.value or "\0" in self.value:
            raise ValueError("invalid mechanism key")


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    kind: EvidenceAnchorKind
    id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("evidence id is required")


@dataclass(frozen=True, slots=True)
class TraceDiagnosis:
    trace_id: TraceId
    status: DiagnosisStatus
    mechanism: MechanismKey | None
    title: str
    description: str
    evidence: tuple[EvidenceAnchor, ...]
    components: tuple[ComponentKind, ...]
    severity: Severity | None
    confidence: float | None

    @classmethod
    def proposed(
        cls,
        trace_id: TraceId,
        mechanism: MechanismKey,
        title: str,
        description: str,
        evidence: tuple[EvidenceAnchor, ...],
        components: tuple[ComponentKind, ...],
        severity: Severity,
        confidence: float,
    ) -> TraceDiagnosis:
        if (
            not title
            or not description
            or not evidence
            or not components
            or not math.isfinite(confidence)
            or confidence < 0
            or confidence > 1
        ):
            raise ValueError("invalid diagnosis")
        return cls(
            trace_id,
            DiagnosisStatus.PROPOSED,
            mechanism,
            title,
            description,
            evidence,
            components,
            severity,
            confidence,
        )

    @classmethod
    def abstained(cls, trace_id: TraceId) -> TraceDiagnosis:
        return cls(trace_id, DiagnosisStatus.ABSTAINED, None, "", "", (), (), None, None)


class HermesAgentVersion(StrEnum):
    V0_20_0 = "0.20.0"


def hermes_python_command(interpreter: Path) -> ProcessCommand:
    if not interpreter.is_absolute():
        raise ValueError("Hermes Python interpreter path must be absolute")
    bridge = Path(__file__).with_name("_hermes_oneshot_bridge.py").resolve(strict=True)
    return ProcessCommand((str(interpreter), str(bridge)))


@dataclass(frozen=True, slots=True)
class PythonDiagnoser:
    entrypoint: PythonEntrypoint
    limits: ProcessLimits
    model: ModelFingerprint | None = None

    def fingerprint(self, root: Path) -> Sha256Digest:
        source = resolve_python_source(root, self.entrypoint)
        model = (
            "none"
            if self.model is None
            else f"{self.model.provider}:{self.model.model}:{self.model.reasoning}"
        )
        return _digest_text(
            f"{self.entrypoint.module.value}\0{self.entrypoint.function.value}\0"
            f"{digest_bytes(source.read_bytes())}\0{self.limits.timeout.total_seconds()}\0{model}"
        )

    def diagnose(
        self,
        snapshot: TraceSnapshot,
        prepared: PreparedEnvironment,
        revision: HarnessRevision,
    ) -> TraceDiagnosis:
        del revision
        command = ProcessCommand(
            (
                sys.executable,
                "-m",
                "ofw._diagnosis_runner",
                self.entrypoint.module.value,
                self.entrypoint.function.value,
            )
        )
        process = prepared.run(command, _SNAPSHOT_ADAPTER.dump_json(snapshot).decode())
        if process.timed_out or process.exit_code != 0:
            return TraceDiagnosis.abstained(snapshot.trace.id)
        try:
            return _DIAGNOSIS_ADAPTER.validate_json(process.stdout)
        except ValidationError:
            return TraceDiagnosis.abstained(snapshot.trace.id)


@dataclass(frozen=True, slots=True)
class HermesDiagnoser:
    command: ProcessCommand
    model: ModelFingerprint
    agent_version: HermesAgentVersion
    limits: ProcessLimits
    maximum_prompt_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.agent_version, HermesAgentVersion):
            raise ValueError("unsupported Hermes agent version")
        if not 1024 <= self.maximum_prompt_bytes <= 131_072:
            raise ValueError("Hermes prompt budget must be between 1024 and 131072 bytes")

    def fingerprint(self, root: Path) -> Sha256Digest:
        del root
        return _digest_text(
            "\0".join(
                (
                    "hermes-diagnoser-v3",
                    *self.command.arguments,
                    self.model.provider,
                    self.model.model,
                    self.model.reasoning,
                    self.agent_version.value,
                    str(self.limits.timeout.total_seconds()),
                    str(self.maximum_prompt_bytes),
                )
            )
        )

    def diagnose(
        self,
        snapshot: TraceSnapshot,
        prepared: PreparedEnvironment,
        revision: HarnessRevision,
    ) -> TraceDiagnosis:
        command = ProcessCommand(
            (
                sys.executable,
                "-m",
                "ofw._hermes_diagnosis_runner",
                _COMMAND_ADAPTER.dump_json(self.command).decode(),
                self.model.provider,
                self.model.model,
                self.model.reasoning,
                str(self.limits.timeout.total_seconds()),
                str(self.maximum_prompt_bytes),
                self.agent_version.value,
                _HARNESS_ASSETS_ADAPTER.dump_json(revision.assets).decode(),
            )
        )
        process = prepared.run(command, _SNAPSHOT_ADAPTER.dump_json(snapshot).decode())
        if process.timed_out or process.exit_code != 0:
            return TraceDiagnosis.abstained(snapshot.trace.id)
        try:
            return _DIAGNOSIS_ADAPTER.validate_json(process.stdout)
        except ValidationError:
            return TraceDiagnosis.abstained(snapshot.trace.id)


DiagnoserAdapter = PythonDiagnoser | HermesDiagnoser


@dataclass(frozen=True, slots=True)
class ClusterId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ClusterReviewerId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip() or "\0" in self.value:
            raise DiagnosisError(DiagnosisErrorCode.REVIEW_INVALID, self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ClusterReview:
    cluster_id: ClusterId
    cluster_revision: int
    cluster_content_digest: Sha256Digest
    reviewer_id: ClusterReviewerId
    decision: ClusterReviewDecision
    reviewed_at: datetime

    def __post_init__(self) -> None:
        if self.cluster_revision < 1 or self.reviewed_at.utcoffset() is None:
            raise DiagnosisError(
                DiagnosisErrorCode.REVIEW_INVALID,
                self.cluster_id.value,
            )


@dataclass(frozen=True, slots=True)
class ClusterRevisionRef:
    id: ClusterId
    revision: int


@dataclass(frozen=True, slots=True)
class FailureCluster:
    id: ClusterId
    revision: int
    content_digest: Sha256Digest
    mechanism: MechanismKey
    title: str
    description: str
    source_trace_ids: tuple[TraceId, ...]
    evidence: tuple[EvidenceAnchor, ...]
    components: tuple[ComponentKind, ...]
    recurrence: int
    severity: Severity
    confidence: float
    resolution_rate: float
    state: ClusterState
    parents: tuple[ClusterRevisionRef, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosisRunId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DiagnosisResult:
    schema_version: DiagnosisSchemaVersion
    id: DiagnosisRunId
    mine_id: MineRunId
    revision_id: HarnessRevisionId
    diagnoser_digest: Sha256Digest
    source_watermark: datetime
    diagnoses: tuple[TraceDiagnosis, ...]
    clusters: tuple[FailureCluster, ...]
    root: Path
    reviews: tuple[ClusterReview, ...] = ()

    @property
    def abstained_count(self) -> int:
        return sum(diagnosis.status is DiagnosisStatus.ABSTAINED for diagnosis in self.diagnoses)

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "mine" / str(self.mine_id) / "diagnosis" / f"{self.id}.json"

    def to_json(self) -> str:
        return _RESULT_ADAPTER.dump_json(self).decode()


_SNAPSHOT_ADAPTER: TypeAdapter[TraceSnapshot] = TypeAdapter(TraceSnapshot)
_DIAGNOSIS_ADAPTER: TypeAdapter[TraceDiagnosis] = TypeAdapter(TraceDiagnosis)
_DIAGNOSES_ADAPTER: TypeAdapter[tuple[TraceDiagnosis, ...]] = TypeAdapter(
    tuple[TraceDiagnosis, ...]
)
_RESULT_ADAPTER: TypeAdapter[DiagnosisResult] = TypeAdapter(DiagnosisResult)
_COMMAND_ADAPTER: TypeAdapter[ProcessCommand] = TypeAdapter(ProcessCommand)
_HARNESS_ASSETS_ADAPTER: TypeAdapter[tuple[HarnessAsset, ...]] = TypeAdapter(
    tuple[HarnessAsset, ...]
)
_REVIEWS_ADAPTER: TypeAdapter[tuple[ClusterReview, ...]] = TypeAdapter(tuple[ClusterReview, ...])


@dataclass(frozen=True, slots=True)
class DiagnosisReview:
    source: DiagnosisResult
    reviews: tuple[ClusterReview, ...]

    def run(self) -> DiagnosisResult:
        _validate_reviews(self.source, self.reviews)
        combined = (*self.source.reviews, *self.reviews)
        review_digest = digest_bytes(_REVIEWS_ADAPTER.dump_json(combined))
        run_id = DiagnosisRunId(
            "diagnosis_"
            + hashlib.sha256(
                f"{self.source.id}\0{review_digest}\0{int(DiagnosisSchemaVersion.V1)}".encode()
            ).hexdigest()
        )
        result = replace(
            self.source,
            id=run_id,
            clusters=tuple(
                _reviewed_cluster(cluster, self.reviews) for cluster in self.source.clusters
            ),
            reviews=combined,
        )
        write_artifact(result.manifest_path, f"{result.to_json()}\n".encode())
        return result


@dataclass(frozen=True, slots=True)
class DiagnosisRun:
    source: Harness | HarnessRevision
    mine: MineResult
    diagnoser: DiagnoserAdapter
    previous: DiagnosisResult | None = None

    def run(self) -> DiagnosisResult:
        revision = _resolve_revision(self.source)
        if revision.id != self.mine.revision_id:
            raise DiagnosisError(DiagnosisErrorCode.REVISION_MISMATCH, str(revision.id))
        diagnoser_digest = self.diagnoser.fingerprint(revision.root)
        failures = tuple(
            admission
            for admission in self.mine.admissions
            if admission.partition is TracePartition.VERIFIED_FAILURE
            and admission.snapshot_path is not None
        )
        environment = LocalProcess(self.diagnoser.limits)
        prepared = environment.prepare(revision, CanaryCase(CaseId("diagnosis"), ""))
        try:
            diagnoses = tuple(
                self._diagnose(read_snapshot(admission, self.mine), prepared, revision)
                for admission in failures
                if admission.snapshot_path is not None
            )
        finally:
            environment.destroy(prepared)
        diagnoses_digest = digest_bytes(_DIAGNOSES_ADAPTER.dump_json(diagnoses))
        run_id = DiagnosisRunId(
            "diagnosis_"
            + hashlib.sha256(
                f"{self.mine.id}\0{diagnoser_digest}\0{diagnoses_digest}\0"
                f"{'' if self.previous is None else self.previous.id}\0"
                f"{int(DiagnosisSchemaVersion.V1)}".encode()
            ).hexdigest()
        )
        clusters = _clusters(diagnoses, diagnoser_digest, self.previous)
        result = DiagnosisResult(
            DiagnosisSchemaVersion.V1,
            run_id,
            self.mine.id,
            revision.id,
            diagnoser_digest,
            self.mine.window.end,
            diagnoses,
            clusters,
            revision.root,
            () if self.previous is None else self.previous.reviews,
        )
        write_artifact(result.manifest_path, f"{result.to_json()}\n".encode())
        return result

    def _diagnose(
        self,
        snapshot: TraceSnapshot,
        prepared: PreparedEnvironment,
        revision: HarnessRevision,
    ) -> TraceDiagnosis:
        diagnosis = self.diagnoser.diagnose(snapshot, prepared, revision)
        if (
            diagnosis.trace_id != snapshot.trace.id
            or not _diagnosis_valid(diagnosis)
            or not _anchors_exist(diagnosis, snapshot)
            or any(revision.component(component) is None for component in diagnosis.components)
        ):
            return TraceDiagnosis.abstained(snapshot.trace.id)
        return diagnosis


def _validate_reviews(
    source: DiagnosisResult,
    reviews: tuple[ClusterReview, ...],
) -> None:
    keys = tuple((review.cluster_id, review.cluster_revision) for review in reviews)
    previous_keys = tuple((review.cluster_id, review.cluster_revision) for review in source.reviews)
    if not reviews or len(set(keys)) != len(keys) or any(key in previous_keys for key in keys):
        raise DiagnosisError(DiagnosisErrorCode.REVIEW_INVALID, str(source.id))
    for review in reviews:
        cluster = next(
            (item for item in source.clusters if item.id == review.cluster_id),
            None,
        )
        if (
            cluster is None
            or cluster.revision != review.cluster_revision
            or cluster.content_digest != review.cluster_content_digest
            or cluster.state not in (ClusterState.PROPOSED, ClusterState.REOPENED)
        ):
            raise DiagnosisError(
                DiagnosisErrorCode.REVIEW_INVALID,
                review.cluster_id.value,
            )


def _reviewed_cluster(
    cluster: FailureCluster,
    reviews: tuple[ClusterReview, ...],
) -> FailureCluster:
    review = next(
        (
            item
            for item in reviews
            if item.cluster_id == cluster.id and item.cluster_revision == cluster.revision
        ),
        None,
    )
    if review is None:
        return cluster
    state = (
        ClusterState.CONFIRMED
        if review.decision is ClusterReviewDecision.CONFIRM
        else ClusterState.REJECTED
    )
    return replace(cluster, state=state)


def _clusters(
    diagnoses: tuple[TraceDiagnosis, ...],
    diagnoser_digest: Sha256Digest,
    previous: DiagnosisResult | None,
) -> tuple[FailureCluster, ...]:
    current_mechanisms = {
        diagnosis.mechanism
        for diagnosis in diagnoses
        if diagnosis.status is DiagnosisStatus.PROPOSED and diagnosis.mechanism is not None
    }
    previous_mechanisms = (
        set() if previous is None else {cluster.mechanism for cluster in previous.clusters}
    )
    mechanisms = tuple(sorted(current_mechanisms | previous_mechanisms, key=_mechanism_sort_key))
    return tuple(
        _cluster_revision(
            mechanism,
            tuple(diagnosis for diagnosis in diagnoses if diagnosis.mechanism == mechanism),
            diagnoser_digest,
            _previous_cluster(previous, mechanism),
        )
        for mechanism in mechanisms
    )


def _cluster_revision(
    mechanism: MechanismKey,
    diagnoses: tuple[TraceDiagnosis, ...],
    diagnoser_digest: Sha256Digest,
    previous: FailureCluster | None,
) -> FailureCluster:
    if not diagnoses:
        if previous is None:
            raise ValueError("cluster requires diagnosis or prior revision")
        return _resolved_cluster(previous)
    first = diagnoses[0]
    evidence = tuple(anchor for diagnosis in diagnoses for anchor in diagnosis.evidence)
    components = tuple(
        sorted(
            {component for diagnosis in diagnoses for component in diagnosis.components},
            key=_component_sort_key,
        )
    )
    severity = max(
        (diagnosis.severity for diagnosis in diagnoses if diagnosis.severity is not None),
        key=_severity_rank,
    )
    confidence = sum(diagnosis.confidence or 0 for diagnosis in diagnoses) / len(diagnoses)
    trace_ids = tuple(diagnosis.trace_id for diagnosis in diagnoses)
    diagnoses_digest = digest_bytes(_DIAGNOSES_ADAPTER.dump_json(diagnoses))
    content_digest = _digest_text(
        f"{diagnoser_digest}\0{diagnoses_digest}\0"
        + "\0".join(trace_id.value for trace_id in trace_ids)
    )
    if previous is not None and previous.content_digest == content_digest:
        return previous
    revision = 1 if previous is None else previous.revision + 1
    parents = () if previous is None else (ClusterRevisionRef(previous.id, previous.revision),)
    removed = (
        0
        if previous is None
        else sum(trace_id not in trace_ids for trace_id in previous.source_trace_ids)
    )
    resolution_rate = (
        0.0 if previous is None or not previous.source_trace_ids else removed / previous.recurrence
    )
    state = (
        ClusterState.PROPOSED
        if previous is None
        else (
            ClusterState.REOPENED
            if previous.state
            in (
                ClusterState.CONFIRMED,
                ClusterState.TARGETED,
                ClusterState.RESOLVED,
                ClusterState.REJECTED,
            )
            else previous.state
        )
    )
    return FailureCluster(
        _cluster_id(mechanism),
        revision,
        content_digest,
        mechanism,
        first.title,
        first.description,
        trace_ids,
        evidence,
        components,
        len(diagnoses),
        severity,
        confidence,
        resolution_rate,
        state,
        parents,
    )


def _resolved_cluster(previous: FailureCluster) -> FailureCluster:
    if previous.state in (ClusterState.RESOLVED, ClusterState.REJECTED):
        return previous
    return FailureCluster(
        previous.id,
        previous.revision + 1,
        _digest_text(f"resolved\0{previous.content_digest}"),
        previous.mechanism,
        previous.title,
        previous.description,
        (),
        (),
        previous.components,
        0,
        previous.severity,
        previous.confidence,
        1.0,
        ClusterState.RESOLVED,
        (ClusterRevisionRef(previous.id, previous.revision),),
    )


def _cluster_id(mechanism: MechanismKey) -> ClusterId:
    return ClusterId("cluster_" + hashlib.sha256(mechanism.value.encode()).hexdigest())


def _previous_cluster(
    previous: DiagnosisResult | None,
    mechanism: MechanismKey,
) -> FailureCluster | None:
    if previous is None:
        return None
    return next(
        (cluster for cluster in previous.clusters if cluster.mechanism == mechanism),
        None,
    )


def _anchors_exist(diagnosis: TraceDiagnosis, snapshot: TraceSnapshot) -> bool:
    if diagnosis.status is DiagnosisStatus.ABSTAINED:
        return True
    return bool(diagnosis.evidence) and all(
        _anchor_exists(anchor, snapshot) for anchor in diagnosis.evidence
    )


def _diagnosis_valid(diagnosis: TraceDiagnosis) -> bool:
    if diagnosis.status is DiagnosisStatus.ABSTAINED:
        return diagnosis.mechanism is None
    return (
        diagnosis.mechanism is not None
        and bool(diagnosis.title)
        and bool(diagnosis.description)
        and bool(diagnosis.evidence)
        and bool(diagnosis.components)
        and diagnosis.severity is not None
        and diagnosis.confidence is not None
        and math.isfinite(diagnosis.confidence)
        and 0 <= diagnosis.confidence <= 1
    )


def _anchor_exists(anchor: EvidenceAnchor, snapshot: TraceSnapshot) -> bool:
    if anchor.kind is EvidenceAnchorKind.OBSERVATION:
        return any(observation.id.value == anchor.id for observation in snapshot.observations)
    return any(score.id.value == anchor.id for score in snapshot.scores)


def read_snapshot(admission: TraceAdmission, mine: MineResult) -> TraceSnapshot:
    path = admission.snapshot_path
    digest = admission.snapshot_digest
    if path is None or digest is None:
        raise DiagnosisError(DiagnosisErrorCode.ARTIFACT_INVALID, admission.trace_id.value)
    try:
        root = (mine.root / ".ofw" / "mine" / str(mine.id) / "traces").resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        payload = resolved.read_bytes()
        if digest_bytes(payload) != digest:
            raise DiagnosisError(DiagnosisErrorCode.ARTIFACT_INVALID, str(path))
        snapshot = _SNAPSHOT_ADAPTER.validate_json(payload)
        if (
            snapshot.trace.id != admission.trace_id
            or snapshot.revision_id != mine.revision_id
            or snapshot.collection_digest != mine.collection_digest
        ):
            raise DiagnosisError(DiagnosisErrorCode.ARTIFACT_INVALID, str(path))
        return snapshot
    except (OSError, ValueError, ValidationError) as error:
        raise DiagnosisError(DiagnosisErrorCode.ARTIFACT_INVALID, str(path)) from error


def _resolve_revision(source: Harness | HarnessRevision) -> HarnessRevision:
    if isinstance(source, HarnessRevision):
        return source
    revision = source.current_revision
    if revision is None:
        raise DiagnosisError(DiagnosisErrorCode.STALE_HARNESS, source.name)
    return revision


def _severity_rank(severity: Severity) -> int:
    return (Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL).index(severity)


def _mechanism_sort_key(mechanism: MechanismKey) -> str:
    return mechanism.value


def _component_sort_key(component: ComponentKind) -> str:
    return component.value


def _digest_text(value: str) -> Sha256Digest:
    return digest_bytes(value.encode())
