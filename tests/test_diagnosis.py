"""Evidence-bound failure diagnosis and deterministic clustering."""

from __future__ import annotations

import hashlib
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ofw import (
    DiagnosisError,
    DiagnosisErrorCode,
    DiagnosisRun,
    FunctionName,
    Harness,
    MechanismKey,
    ModuleName,
    ProcessLimits,
    PythonDiagnoser,
    PythonEntrypoint,
)
from ofw.contracts import HarnessRevisionId, Sha256Digest
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


def _repository(tmp_path: Path) -> tuple[Path, HarnessRevisionId]:
    root = tmp_path / "diagnosis-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "diagnoser.py").write_text(
        "from __future__ import annotations\n"
        "import time\n"
        "from ofw import (ComponentKind, EvidenceAnchor, EvidenceAnchorKind, MechanismKey, "
        "Severity, TraceDiagnosis)\n"
        "from ofw.mine import TraceSnapshot\n"
        "def diagnose(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    observation = snapshot.observations[0]\n"
        "    if observation.name == 'abstain':\n"
        "        return TraceDiagnosis.abstained(snapshot.trace.id)\n"
        "    mechanism = 'tool-schema' if observation.name == 'tool' else 'prompt-gap'\n"
        "    component = (\n"
        "        ComponentKind.TOOL if observation.name == 'tool' else ComponentKind.PROMPT\n"
        "    )\n"
        "    return TraceDiagnosis.proposed(\n"
        "        snapshot.trace.id, MechanismKey(mechanism), mechanism, 'fixture diagnosis',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, observation.id.value),),\n"
        "        (component,), Severity.HIGH, 0.9,\n"
        "    )\n"
        "def invalid_anchor(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    return TraceDiagnosis.proposed(\n"
        "        snapshot.trace.id, MechanismKey('invalid-anchor'), 'invalid', 'invalid',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, 'missing'),),\n"
        "        (ComponentKind.PROMPT,), Severity.LOW, 0.5,\n"
        "    )\n"
        "def slow(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    del snapshot\n"
        "    time.sleep(2)\n"
        "    raise RuntimeError('late')\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("diagnosis-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    return root, harness.process().id


def _snapshot(
    root: Path,
    revision_id: HarnessRevisionId,
    mine_id: MineRunId,
    trace: str,
    name: str,
) -> tuple[Path, Sha256Digest]:
    observation_id = ObservationId(f"observation-{trace}")
    snapshot = TraceSnapshot(
        MineSchemaVersion.V1,
        revision_id,
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
    path = root / ".ofw" / "mine" / str(mine_id) / "traces" / f"{digest.value[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, digest


def _admission(
    root: Path,
    revision_id: HarnessRevisionId,
    mine_id: MineRunId,
    trace: str,
    name: str,
    partition: TracePartition,
) -> TraceAdmission:
    path, digest = _snapshot(root, revision_id, mine_id, trace, name)
    return TraceAdmission(
        TraceId(trace),
        partition,
        (
            AdmissionReason.VERIFIED_FAIL
            if partition is TracePartition.VERIFIED_FAILURE
            else AdmissionReason.VERIFIED_PASS
        ),
        (),
        digest,
        path,
    )


def _mine_result(tmp_path: Path) -> tuple[MineResult, Harness]:
    root, revision_id = _repository(tmp_path)
    cases = (
        ("tool-one", "tool", TracePartition.VERIFIED_FAILURE),
        ("tool-two", "tool", TracePartition.VERIFIED_FAILURE),
        ("prompt-one", "prompt", TracePartition.VERIFIED_FAILURE),
        ("unknown", "abstain", TracePartition.VERIFIED_FAILURE),
        ("good", "tool", TracePartition.VERIFIED_GOOD),
    )
    mine_id = MineRunId("mine_fixture")
    admissions = tuple(
        _admission(root, revision_id, mine_id, trace, name, partition)
        for trace, name, partition in cases
    )
    start = datetime(2026, 8, 22, tzinfo=UTC)
    result = MineResult(
        MineSchemaVersion.V1,
        mine_id,
        revision_id,
        TraceWindow(start, start + timedelta(hours=1)),
        Sha256Digest("sha256:collection"),
        Sha256Digest("sha256:policy"),
        admissions,
        root,
    )
    harness = Harness("diagnosis-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.process()
    return result, harness


def _diagnoser(function: str, timeout: timedelta = timedelta(seconds=1)) -> PythonDiagnoser:
    return PythonDiagnoser(
        PythonEntrypoint(ModuleName("diagnoser"), FunctionName(function)),
        ProcessLimits(timeout),
    )


def test_verified_failures_form_evidence_bound_mechanism_clusters(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()

    assert tuple(cluster.mechanism for cluster in result.clusters) == (
        MechanismKey("prompt-gap"),
        MechanismKey("tool-schema"),
    )
    assert tuple(cluster.recurrence for cluster in result.clusters) == (1, 2)
    assert all(cluster.evidence for cluster in result.clusters)
    assert result.abstained_count == 1
    assert TraceId("good") not in tuple(diagnosis.trace_id for diagnosis in result.diagnoses)


def test_invalid_evidence_anchor_becomes_abstention(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _diagnoser("invalid_anchor")).run()

    assert not result.clusters
    assert result.abstained_count == 4


def test_diagnoser_timeout_is_bounded_and_abstains(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    started = time.monotonic()

    result = DiagnosisRun(
        harness,
        mine,
        _diagnoser("slow", timedelta(milliseconds=50)),
    ).run()

    assert result.abstained_count == 4
    assert time.monotonic() - started < 1


def test_diagnosis_run_is_deterministic(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    run = DiagnosisRun(harness, mine, _diagnoser("diagnose"))

    assert run.run() == run.run()


def test_tampered_snapshot_is_rejected_before_diagnosis(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    snapshot_path = mine.admissions[0].snapshot_path
    assert snapshot_path is not None
    snapshot_path.write_text("{}", encoding="utf-8")

    with pytest.raises(DiagnosisError) as raised:
        DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()

    assert raised.value.code is DiagnosisErrorCode.ARTIFACT_INVALID
