"""Authoritative family ledger and privacy-safe Mine export manifests."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from ofw.contracts import ComponentKind, HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.diagnosis import ClusterState, DiagnosisResult, FailureCluster, read_snapshot
from ofw.mine import (
    MineResult,
    SnapshotObservation,
    TraceAdmission,
    TracePartition,
    TraceSnapshot,
    write_artifact,
)
from ofw.observability.langfuse.domain import ScoreId, TraceId


class ExportSchemaVersion(IntEnum):
    V1 = 1


class ExportPartition(StrEnum):
    TRAINING = "training"
    MEMORY = "memory"
    FRONTIER = "frontier"
    REGRESSION = "regression"
    SELECTION = "selection"
    ADMISSION = "admission"
    REVIEW = "review"


class DatasetSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"


class ConsentStatus(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class PrivacyTransform(StrEnum):
    METADATA_ONLY = "metadata_only"


class LeakageErrorCode(StrEnum):
    FAMILY_CONFLICT = "family_conflict"
    REVISION_MISMATCH = "revision_mismatch"
    INVALID_POLICY = "invalid_policy"


class LeakageError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: LeakageErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class DataLicense:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise LeakageError(LeakageErrorCode.INVALID_POLICY, "license is required")


@dataclass(frozen=True, slots=True)
class ClusterPartitionRule:
    cluster_family_id: ClusterFamilyId
    partition: ExportPartition


@dataclass(frozen=True, slots=True)
class ExportPolicy:
    failure_partitions: tuple[ExportPartition, ...]
    validation_fraction: float
    license: DataLicense
    consent: ConsentStatus
    cluster_rules: tuple[ClusterPartitionRule, ...] = ()

    def __post_init__(self) -> None:
        allowed = (
            ExportPartition.MEMORY,
            ExportPartition.FRONTIER,
            ExportPartition.REGRESSION,
            ExportPartition.SELECTION,
            ExportPartition.ADMISSION,
        )
        if (
            not self.failure_partitions
            or any(partition not in allowed for partition in self.failure_partitions)
            or any(rule.partition not in allowed for rule in self.cluster_rules)
            or self.consent is not ConsentStatus.APPROVED
            or not math.isfinite(self.validation_fraction)
            or self.validation_fraction < 0
            or self.validation_fraction > 1
            or len({rule.cluster_family_id for rule in self.cluster_rules})
            != len(self.cluster_rules)
        ):
            raise LeakageError(LeakageErrorCode.INVALID_POLICY, "invalid export policy")

    @property
    def digest(self) -> Sha256Digest:
        return _digest_text(
            "\0".join(
                (
                    *(partition.value for partition in self.failure_partitions),
                    str(self.validation_fraction),
                    self.license.value,
                    self.consent.value,
                    *(
                        f"{rule.cluster_family_id.value}:{rule.partition.value}"
                        for rule in self.cluster_rules
                    ),
                    str(int(ExportSchemaVersion.V1)),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class TraceFamilyId:
    value: str


@dataclass(frozen=True, slots=True)
class ClusterFamilyId:
    value: str


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    trace_id: TraceId
    trace_family_id: TraceFamilyId
    cluster_family_id: ClusterFamilyId | None
    partition: ExportPartition
    snapshot: SnapshotReference


@dataclass(frozen=True, slots=True)
class PartitionLedger:
    entries: tuple[LedgerEntry, ...]

    def validate(self) -> bool:
        return all(
            len(
                {
                    candidate.partition
                    for candidate in self.entries
                    if candidate.trace_family_id == entry.trace_family_id
                }
            )
            == 1
            for entry in self.entries
        ) and all(
            len(
                {
                    candidate.partition
                    for candidate in self.entries
                    if entry.cluster_family_id is not None
                    and candidate.cluster_family_id == entry.cluster_family_id
                }
            )
            == 1
            for entry in self.entries
            if entry.cluster_family_id is not None
        )


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    path: Path
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class GoodTraceExample:
    trace_id: TraceId
    family_id: TraceFamilyId
    snapshot: SnapshotReference
    split: DatasetSplit


@dataclass(frozen=True, slots=True)
class GoodTraceDataset:
    id: str
    revision_id: HarnessRevisionId
    license: DataLicense
    consent: ConsentStatus
    privacy_transform: PrivacyTransform
    examples: tuple[GoodTraceExample, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    trace_id: TraceId
    family_id: TraceFamilyId
    cluster_family_id: ClusterFamilyId
    partition: ExportPartition
    snapshot: SnapshotReference
    verifier_score_ids: tuple[ScoreId, ...]
    deterministic: bool = True
    repeats: int = 1
    critical: bool = False


@dataclass(frozen=True, slots=True)
class EvalSuite:
    id: str
    revision_id: HarnessRevisionId
    cases: tuple[EvalCase, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    cluster_family_id: ClusterFamilyId
    mechanism: str
    proposal: str
    components: tuple[ComponentKind, ...]
    source_trace_ids: tuple[TraceId, ...]


@dataclass(frozen=True, slots=True)
class MemoryPatchSet:
    id: str
    revision_id: HarnessRevisionId
    candidates: tuple[MemoryCandidate, ...]
    path: Path


@dataclass(frozen=True, slots=True)
class Benchmark:
    id: str
    revision_id: HarnessRevisionId
    developer_suite_id: str
    selection_suite_id: str
    admission_suite_id: str
    execution_digest: Sha256Digest | None
    lifecycle_digest: Sha256Digest | None
    path: Path


@dataclass(frozen=True, slots=True)
class ExportBundle:
    id: str
    previous_id: str | None
    revision_id: HarnessRevisionId
    ledger: PartitionLedger
    good_traces: GoodTraceDataset
    developer_evals: EvalSuite
    selection_holdout: EvalSuite
    admission_holdout: EvalSuite
    memory: MemoryPatchSet
    benchmark: Benchmark
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "mine" / "exports" / self.id / "manifest.json"

    def to_json(self) -> str:
        return _BUNDLE_ADAPTER.dump_json(self).decode()


_LEDGER_ADAPTER: TypeAdapter[PartitionLedger] = TypeAdapter(PartitionLedger)
_GOOD_ADAPTER: TypeAdapter[GoodTraceDataset] = TypeAdapter(GoodTraceDataset)
_SUITE_ADAPTER: TypeAdapter[EvalSuite] = TypeAdapter(EvalSuite)
_MEMORY_ADAPTER: TypeAdapter[MemoryPatchSet] = TypeAdapter(MemoryPatchSet)
_BENCHMARK_ADAPTER: TypeAdapter[Benchmark] = TypeAdapter(Benchmark)
_BUNDLE_ADAPTER: TypeAdapter[ExportBundle] = TypeAdapter(ExportBundle)


@dataclass(frozen=True, slots=True)
class MineExports:
    revision: HarnessRevision
    mine: MineResult
    diagnosis: DiagnosisResult
    policy: ExportPolicy
    previous: ExportBundle | None = None

    def run(self) -> ExportBundle:
        self._validate_lineage()
        export_id = (
            "exports_"
            + hashlib.sha256(
                f"{self.mine.id}\0{self.diagnosis.id}\0{self.policy.digest}".encode()
                if self.previous is None
                else f"{self.mine.id}\0{self.diagnosis.id}\0{self.policy.digest}\0"
                f"{self.previous.id}".encode()
            ).hexdigest()
        )
        root = self.revision.root / ".ofw" / "mine" / "exports" / export_id
        current_entries = tuple(self._ledger_entry(admission) for admission in self._eligible())
        prior_entries = () if self.previous is None else self.previous.ledger.entries
        entries = (
            tuple(
                entry
                for entry in prior_entries
                if all(current.trace_id != entry.trace_id for current in current_entries)
            )
            + current_entries
        )
        ledger = PartitionLedger(entries)
        if not ledger.validate():
            raise LeakageError(LeakageErrorCode.FAMILY_CONFLICT, export_id)
        good = self._good_dataset(export_id, root, entries)
        developer = self._suite(
            export_id,
            root / "developer.json",
            entries,
            (ExportPartition.FRONTIER, ExportPartition.REGRESSION),
        )
        selection = self._suite(
            export_id,
            root / "selection.json",
            entries,
            (ExportPartition.SELECTION,),
        )
        admission = self._suite(
            export_id,
            root / "admission.json",
            entries,
            (ExportPartition.ADMISSION,),
        )
        memory = self._memory(export_id, root / "memory.json", entries)
        runtime = self.revision.runtime
        benchmark = Benchmark(
            f"benchmark_{export_id[8:]}",
            self.revision.id,
            developer.id,
            selection.id,
            admission.id,
            None if runtime is None else runtime.execution,
            None if runtime is None else runtime.lifecycle,
            root / "benchmark.json",
        )
        bundle = ExportBundle(
            export_id,
            None if self.previous is None else self.previous.id,
            self.revision.id,
            ledger,
            good,
            developer,
            selection,
            admission,
            memory,
            benchmark,
            self.revision.root,
        )
        self._write(bundle)
        return bundle

    def _validate_lineage(self) -> None:
        if (
            self.revision.id != self.mine.revision_id
            or self.revision.id != self.diagnosis.revision_id
            or self.mine.id != self.diagnosis.mine_id
        ):
            raise LeakageError(LeakageErrorCode.REVISION_MISMATCH, str(self.revision.id))

    def _eligible(self) -> tuple[TraceAdmission, ...]:
        return tuple(
            admission
            for admission in self.mine.admissions
            if admission.partition
            in (TracePartition.VERIFIED_GOOD, TracePartition.VERIFIED_FAILURE)
            and admission.snapshot_path is not None
            and admission.snapshot_digest is not None
        )

    def _ledger_entry(self, admission: TraceAdmission) -> LedgerEntry:
        snapshot = read_snapshot(admission, self.mine)
        family_id = _trace_family(snapshot)
        cluster = _cluster_for_trace(self.diagnosis, admission.trace_id)
        if admission.partition is TracePartition.VERIFIED_FAILURE and (
            cluster is None or cluster.state not in (ClusterState.CONFIRMED, ClusterState.TARGETED)
        ):
            return LedgerEntry(
                admission.trace_id,
                family_id,
                None if cluster is None else ClusterFamilyId(cluster.id.value),
                ExportPartition.REVIEW,
                _snapshot_reference(admission),
            )
        previous = self._previous_partition(family_id, admission.trace_id)
        if (
            previous is ExportPartition.REVIEW
            and admission.partition is TracePartition.VERIFIED_FAILURE
            and cluster is not None
            and cluster.state in (ClusterState.CONFIRMED, ClusterState.TARGETED)
        ):
            previous = None
        if previous is not None:
            is_good = admission.partition is TracePartition.VERIFIED_GOOD
            if is_good != (previous is ExportPartition.TRAINING):
                raise LeakageError(
                    LeakageErrorCode.FAMILY_CONFLICT,
                    family_id.value,
                )
            cluster_family = None if cluster is None else ClusterFamilyId(cluster.id.value)
            return LedgerEntry(
                admission.trace_id,
                family_id,
                cluster_family,
                previous,
                _snapshot_reference(admission),
            )
        if admission.partition is TracePartition.VERIFIED_GOOD:
            return LedgerEntry(
                admission.trace_id,
                family_id,
                None,
                ExportPartition.TRAINING,
                _snapshot_reference(admission),
            )
        if cluster is None:
            return LedgerEntry(
                admission.trace_id,
                family_id,
                None,
                ExportPartition.REVIEW,
                _snapshot_reference(admission),
            )
        cluster_family = ClusterFamilyId(cluster.id.value)
        partition = _failure_partition(cluster_family, self.policy)
        return LedgerEntry(
            admission.trace_id,
            family_id,
            cluster_family,
            partition,
            _snapshot_reference(admission),
        )

    def _previous_partition(
        self,
        family_id: TraceFamilyId,
        trace_id: TraceId,
    ) -> ExportPartition | None:
        if self.previous is None:
            return None
        cluster = _cluster_for_trace(self.diagnosis, trace_id)
        cluster_family = None if cluster is None else ClusterFamilyId(cluster.id.value)
        return next(
            (
                entry.partition
                for entry in self.previous.ledger.entries
                if entry.trace_family_id == family_id
                or (cluster_family is not None and entry.cluster_family_id == cluster_family)
            ),
            None,
        )

    def _good_dataset(
        self,
        export_id: str,
        root: Path,
        entries: tuple[LedgerEntry, ...],
    ) -> GoodTraceDataset:
        examples = tuple(
            GoodTraceExample(
                admission.trace_id,
                entry.trace_family_id,
                _snapshot_reference(admission),
                _split(entry.trace_family_id, self.policy.validation_fraction),
            )
            for admission in self.mine.admissions
            for entry in entries
            if admission.trace_id == entry.trace_id
            and admission.partition is TracePartition.VERIFIED_GOOD
            and entry.partition is ExportPartition.TRAINING
        )
        return GoodTraceDataset(
            f"good_{export_id[8:]}",
            self.revision.id,
            self.policy.license,
            self.policy.consent,
            PrivacyTransform.METADATA_ONLY,
            examples,
            root / "good.json",
        )

    def _suite(
        self,
        export_id: str,
        path: Path,
        entries: tuple[LedgerEntry, ...],
        partitions: tuple[ExportPartition, ...],
    ) -> EvalSuite:
        cases = tuple(
            _eval_case(admission, entry)
            for admission in self.mine.admissions
            for entry in entries
            if admission.trace_id == entry.trace_id
            and admission.partition is TracePartition.VERIFIED_FAILURE
            and entry.partition in partitions
        )
        return EvalSuite(
            f"suite_{path.stem}_{export_id[8:]}",
            self.revision.id,
            cases,
            path,
        )

    def _memory(
        self,
        export_id: str,
        path: Path,
        entries: tuple[LedgerEntry, ...],
    ) -> MemoryPatchSet:
        candidates = tuple(
            MemoryCandidate(
                ClusterFamilyId(cluster.id.value),
                cluster.mechanism.value,
                cluster.description,
                cluster.components,
                cluster.source_trace_ids,
            )
            for cluster in self.diagnosis.clusters
            if any(
                entry.cluster_family_id == ClusterFamilyId(cluster.id.value)
                and entry.partition is ExportPartition.MEMORY
                for entry in entries
            )
        )
        return MemoryPatchSet(
            f"memory_{export_id[8:]}",
            self.revision.id,
            candidates,
            path,
        )

    def _write(self, bundle: ExportBundle) -> None:
        root = bundle.manifest_path.parent
        write_artifact(root / "ledger.json", _LEDGER_ADAPTER.dump_json(bundle.ledger) + b"\n")
        write_artifact(bundle.good_traces.path, _GOOD_ADAPTER.dump_json(bundle.good_traces) + b"\n")
        for suite in (
            bundle.developer_evals,
            bundle.selection_holdout,
            bundle.admission_holdout,
        ):
            write_artifact(suite.path, _SUITE_ADAPTER.dump_json(suite) + b"\n")
        write_artifact(bundle.memory.path, _MEMORY_ADAPTER.dump_json(bundle.memory) + b"\n")
        write_artifact(
            bundle.benchmark.path, _BENCHMARK_ADAPTER.dump_json(bundle.benchmark) + b"\n"
        )
        write_artifact(bundle.manifest_path, f"{bundle.to_json()}\n".encode())


def _trace_family(snapshot: TraceSnapshot) -> TraceFamilyId:
    payload = "\0".join(
        f"{observation.type.value}:{observation.name or ''}:{observation.is_root}"
        f":{observation.level}:{observation.parent_observation_id is not None}"
        for observation in sorted(snapshot.observations, key=_observation_family_key)
    )
    return TraceFamilyId(f"family_{hashlib.sha256(payload.encode()).hexdigest()}")


def _observation_family_key(observation: SnapshotObservation) -> tuple[str, str, str, str]:
    return (
        observation.type.value,
        observation.name or "",
        str(observation.is_root),
        str(observation.parent_observation_id is not None),
    )


def _cluster_for_trace(diagnosis: DiagnosisResult, trace_id: TraceId) -> FailureCluster | None:
    return next(
        (cluster for cluster in diagnosis.clusters if trace_id in cluster.source_trace_ids),
        None,
    )


def _failure_partition(
    cluster: ClusterFamilyId,
    policy: ExportPolicy,
) -> ExportPartition:
    explicit = next(
        (rule.partition for rule in policy.cluster_rules if rule.cluster_family_id == cluster),
        None,
    )
    if explicit is not None:
        return explicit
    index = int(hashlib.sha256(cluster.value.encode()).hexdigest()[:8], 16)
    return policy.failure_partitions[index % len(policy.failure_partitions)]


def _snapshot_reference(admission: TraceAdmission) -> SnapshotReference:
    if admission.snapshot_path is None or admission.snapshot_digest is None:
        raise LeakageError(LeakageErrorCode.FAMILY_CONFLICT, admission.trace_id.value)
    return SnapshotReference(admission.snapshot_path, admission.snapshot_digest)


def _split(family: TraceFamilyId, validation_fraction: float) -> DatasetSplit:
    bucket = int(hashlib.sha256(family.value.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return DatasetSplit.VALIDATION if bucket < validation_fraction else DatasetSplit.TRAIN


def _eval_case(admission: TraceAdmission, entry: LedgerEntry) -> EvalCase:
    cluster = entry.cluster_family_id
    if cluster is None:
        raise LeakageError(LeakageErrorCode.FAMILY_CONFLICT, admission.trace_id.value)
    case_digest = hashlib.sha256(
        (admission.trace_id.value + entry.partition.value).encode()
    ).hexdigest()
    return EvalCase(
        f"eval_{case_digest}",
        admission.trace_id,
        entry.trace_family_id,
        cluster,
        entry.partition,
        _snapshot_reference(admission),
        admission.evidence_score_ids,
    )


def _digest_text(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value.encode()).hexdigest()}")
