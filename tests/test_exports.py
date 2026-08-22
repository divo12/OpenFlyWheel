"""Authoritative no-leak ledger and Mine export behavior."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ofw import (
    ClusterFamilyId,
    ClusterPartitionRule,
    ConsentStatus,
    DataLicense,
    ExportPartition,
    ExportPolicy,
    Harness,
    LeakageError,
    LeakageErrorCode,
    MineExports,
)
from ofw.contracts import ComponentKind, HarnessRevision, Sha256Digest
from ofw.diagnosis import (
    ClusterId,
    ClusterState,
    DiagnosisResult,
    DiagnosisRunId,
    DiagnosisSchemaVersion,
    FailureCluster,
    MechanismKey,
    Severity,
)
from ofw.mine import (
    AdmissionReason,
    MineResult,
    MineRunId,
    MineSchemaVersion,
    SnapshotObservation,
    SnapshotTrace,
    TraceAdmission,
    TracePartition,
    TraceSnapshot,
)
from ofw.observability.langfuse.contracts import TraceWindow
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    ObservationId,
    ObservationType,
    TraceId,
)

_SNAPSHOT_ADAPTER: TypeAdapter[TraceSnapshot] = TypeAdapter(TraceSnapshot)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _revision(tmp_path: Path) -> HarnessRevision:
    root = tmp_path / "export-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("export-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    return harness.process()


def _snapshot(
    revision: HarnessRevision,
    mine_id: MineRunId,
    trace: str,
    name: str,
) -> tuple[Path, Sha256Digest]:
    observation_id = ObservationId(f"observation-{trace}")
    snapshot = TraceSnapshot(
        MineSchemaVersion.V1,
        revision.id,
        Sha256Digest("sha256:collection"),
        SnapshotTrace(
            TraceId(trace),
            (observation_id,),
            (observation_id,),
            (),
            AttributionLevel.EXACT,
            (),
            Sha256Digest(f"sha256:trace-{trace}"),
        ),
        (
            SnapshotObservation(
                observation_id,
                TraceId(trace),
                datetime(2026, 8, 22, tzinfo=UTC),
                datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
                None,
                ObservationType.AGENT,
                True,
                name,
                None,
                None,
                Sha256Digest(f"sha256:observation-{trace}"),
            ),
        ),
        (),
    )
    payload = _SNAPSHOT_ADAPTER.dump_json(snapshot)
    digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
    path = revision.root / ".ofw" / "mine" / str(mine_id) / "traces" / f"{digest.value[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, digest


def _admission(
    revision: HarnessRevision,
    mine_id: MineRunId,
    trace: str,
    name: str,
    partition: TracePartition,
) -> TraceAdmission:
    path, digest = _snapshot(revision, mine_id, trace, name)
    return TraceAdmission(
        TraceId(trace),
        partition,
        (
            AdmissionReason.VERIFIED_PASS
            if partition is TracePartition.VERIFIED_GOOD
            else AdmissionReason.VERIFIED_FAIL
        ),
        (),
        digest,
        path,
    )


def _inputs(
    tmp_path: Path, *, conflict: bool = False
) -> tuple[HarnessRevision, MineResult, DiagnosisResult]:
    revision = _revision(tmp_path)
    mine_id = MineRunId("mine_exports")
    good_name = "shared-topology"
    failure_name = good_name if conflict else "tool-topology"
    admissions = (
        _admission(revision, mine_id, "good-one", good_name, TracePartition.VERIFIED_GOOD),
        _admission(revision, mine_id, "good-two", good_name, TracePartition.VERIFIED_GOOD),
        _admission(
            revision, mine_id, "tool-failure", failure_name, TracePartition.VERIFIED_FAILURE
        ),
        _admission(
            revision,
            mine_id,
            "prompt-failure",
            "prompt-topology",
            TracePartition.VERIFIED_FAILURE,
        ),
    )
    start = datetime(2026, 8, 22, tzinfo=UTC)
    mine = MineResult(
        MineSchemaVersion.V1,
        mine_id,
        revision.id,
        TraceWindow(start, start + timedelta(hours=1)),
        Sha256Digest("sha256:collection"),
        Sha256Digest("sha256:policy"),
        admissions,
        revision.root,
    )
    clusters = (
        _cluster("prompt-gap", TraceId("prompt-failure"), ComponentKind.PROMPT),
        _cluster("tool-schema", TraceId("tool-failure"), ComponentKind.TOOL),
    )
    diagnosis = DiagnosisResult(
        DiagnosisSchemaVersion.V1,
        DiagnosisRunId("diagnosis_exports"),
        mine_id,
        revision.id,
        Sha256Digest("sha256:diagnoser"),
        mine.window.end,
        (),
        clusters,
        revision.root,
    )
    return revision, mine, diagnosis


def _cluster(mechanism: str, trace: TraceId, component: ComponentKind) -> FailureCluster:
    cluster_id = ClusterId(f"cluster-{mechanism}")
    return FailureCluster(
        cluster_id,
        1,
        Sha256Digest(f"sha256:{mechanism}"),
        MechanismKey(mechanism),
        mechanism,
        "fixture cluster",
        (trace,),
        (),
        (component,),
        1,
        Severity.HIGH,
        0.9,
        0.0,
        ClusterState.CONFIRMED,
    )


def _policy() -> ExportPolicy:
    return ExportPolicy(
        failure_partitions=(ExportPartition.FRONTIER, ExportPartition.ADMISSION),
        validation_fraction=0.2,
        license=DataLicense("fixture-approved"),
        consent=ConsentStatus.APPROVED,
        cluster_rules=(
            ClusterPartitionRule(
                ClusterFamilyId("cluster-prompt-gap"),
                ExportPartition.FRONTIER,
            ),
            ClusterPartitionRule(
                ClusterFamilyId("cluster-tool-schema"),
                ExportPartition.ADMISSION,
            ),
        ),
    )


def test_family_ledger_prevents_cross_partition_leakage(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)

    bundle = MineExports(revision, mine, diagnosis, _policy()).run()

    assert len(bundle.good_traces.examples) == 2
    assert bundle.good_traces.examples[0].family_id == bundle.good_traces.examples[1].family_id
    assert bundle.good_traces.examples[0].split == bundle.good_traces.examples[1].split
    training_families = tuple(example.family_id for example in bundle.good_traces.examples)
    eval_families = tuple(case.family_id for case in bundle.developer_evals.cases)
    holdout_families = tuple(case.family_id for case in bundle.admission_holdout.cases)
    assert all(family not in eval_families for family in training_families)
    assert all(family not in holdout_families for family in training_families)
    assert bundle.ledger.validate()


def test_holdout_artifacts_are_separate_from_developer_suite(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)

    bundle = MineExports(revision, mine, diagnosis, _policy()).run()

    assert bundle.developer_evals.path != bundle.admission_holdout.path
    assert bundle.admission_holdout.cases
    assert all(
        case.partition is not ExportPartition.ADMISSION for case in bundle.developer_evals.cases
    )


def test_unconfirmed_failure_clusters_stay_in_review(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    proposed = replace(
        diagnosis,
        clusters=tuple(
            replace(cluster, state=ClusterState.PROPOSED) for cluster in diagnosis.clusters
        ),
    )

    bundle = MineExports(revision, mine, proposed, _policy()).run()

    failure_entries = tuple(
        entry for entry in bundle.ledger.entries if entry.cluster_family_id is not None
    )
    assert failure_entries
    assert all(entry.partition is ExportPartition.REVIEW for entry in failure_entries)
    assert not bundle.developer_evals.cases
    assert not bundle.selection_holdout.cases
    assert not bundle.admission_holdout.cases


def test_confirmed_cluster_can_graduate_from_previous_review_partition(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    proposed = replace(
        diagnosis,
        clusters=tuple(
            replace(cluster, state=ClusterState.PROPOSED) for cluster in diagnosis.clusters
        ),
    )
    review_bundle = MineExports(revision, mine, proposed, _policy()).run()
    confirmed = replace(
        diagnosis,
        clusters=tuple(
            replace(cluster, state=ClusterState.CONFIRMED) for cluster in diagnosis.clusters
        ),
    )

    graduated = MineExports(
        revision,
        mine,
        confirmed,
        _policy(),
        previous=review_bundle,
    ).run()

    failure_entries = tuple(
        entry for entry in graduated.ledger.entries if entry.cluster_family_id is not None
    )
    assert failure_entries
    assert all(entry.partition is not ExportPartition.REVIEW for entry in failure_entries)
    assert graduated.developer_evals.cases
    assert graduated.admission_holdout.cases


def test_export_bundle_is_idempotent_and_content_addressed(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    exports = MineExports(revision, mine, diagnosis, _policy())

    first = exports.run()
    second = exports.run()

    assert first == second
    assert first.manifest_path.read_text(encoding="utf-8") == f"{first.to_json()}\n"


def test_same_family_requested_for_training_and_eval_fails_closed(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path, conflict=True)

    with pytest.raises(LeakageError) as raised:
        MineExports(revision, mine, diagnosis, _policy()).run()

    assert raised.value.code is LeakageErrorCode.FAMILY_CONFLICT


def test_memory_partition_produces_proposal_without_mutating_harness(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    policy = ExportPolicy(
        failure_partitions=(ExportPartition.MEMORY, ExportPartition.REGRESSION),
        validation_fraction=0.2,
        license=DataLicense("fixture-approved"),
        consent=ConsentStatus.APPROVED,
        cluster_rules=(
            ClusterPartitionRule(
                ClusterFamilyId("cluster-prompt-gap"),
                ExportPartition.MEMORY,
            ),
            ClusterPartitionRule(
                ClusterFamilyId("cluster-tool-schema"),
                ExportPartition.REGRESSION,
            ),
        ),
    )
    prompt_before = (revision.root / "prompt.md").read_bytes()

    bundle = MineExports(revision, mine, diagnosis, policy).run()

    assert len(bundle.memory.candidates) == 1
    assert (revision.root / "prompt.md").read_bytes() == prompt_before


def test_new_cluster_does_not_reassign_existing_cluster_families(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    first = MineExports(revision, mine, diagnosis, _policy()).run()
    new_admission = _admission(
        revision,
        mine.id,
        "new-failure",
        "new-topology",
        TracePartition.VERIFIED_FAILURE,
    )
    expanded_mine = replace(mine, admissions=(*mine.admissions, new_admission))
    expanded_diagnosis = replace(
        diagnosis,
        clusters=(
            *diagnosis.clusters,
            _cluster("aaa-new-mechanism", TraceId("new-failure"), ComponentKind.TOOL),
        ),
    )

    second = MineExports(revision, expanded_mine, expanded_diagnosis, _policy()).run()

    for entry in first.ledger.entries:
        if entry.cluster_family_id is None:
            continue
        matching = next(
            candidate
            for candidate in second.ledger.entries
            if candidate.cluster_family_id == entry.cluster_family_id
        )
        assert matching.partition is entry.partition


def test_reordered_same_topology_cannot_cross_training_and_eval(tmp_path: Path) -> None:
    revision, mine, diagnosis = _inputs(tmp_path)
    good_admission = mine.admissions[0]
    failed_admission = mine.admissions[2]
    assert good_admission.snapshot_path is not None
    assert failed_admission.snapshot_path is not None
    good_snapshot = _SNAPSHOT_ADAPTER.validate_json(good_admission.snapshot_path.read_bytes())
    failed_snapshot = _SNAPSHOT_ADAPTER.validate_json(failed_admission.snapshot_path.read_bytes())
    good_root = replace(good_snapshot.observations[0], name="shared-root")
    good_child = replace(
        good_root,
        id=ObservationId("good-child"),
        parent_observation_id=good_root.id,
        is_root=False,
        name="shared-child",
    )
    failed_root = replace(failed_snapshot.observations[0], name="shared-root")
    failed_child = replace(
        failed_root,
        id=ObservationId("failed-child"),
        parent_observation_id=failed_root.id,
        is_root=False,
        name="shared-child",
    )
    changed_good = replace(good_snapshot, observations=(good_root, good_child))
    changed_failed = replace(failed_snapshot, observations=(failed_child, failed_root))
    good_admission = _replace_snapshot(good_admission, changed_good)
    failed_admission = _replace_snapshot(failed_admission, changed_failed)
    changed_mine = replace(
        mine,
        admissions=(good_admission, mine.admissions[1], failed_admission, mine.admissions[3]),
    )

    with pytest.raises(LeakageError) as raised:
        MineExports(revision, changed_mine, diagnosis, _policy()).run()

    assert raised.value.code is LeakageErrorCode.FAMILY_CONFLICT


def _replace_snapshot(
    admission: TraceAdmission,
    snapshot: TraceSnapshot,
) -> TraceAdmission:
    payload = _SNAPSHOT_ADAPTER.dump_json(snapshot)
    digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
    assert admission.snapshot_path is not None
    path = admission.snapshot_path.with_name(f"{digest.value[7:]}.json")
    path.write_bytes(payload)
    return replace(admission, snapshot_digest=digest, snapshot_path=path)
