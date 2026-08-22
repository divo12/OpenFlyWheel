"""Evidence-bound failure diagnosis and deterministic clustering."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ofw import (
    ClusterReview,
    ClusterReviewDecision,
    ClusterReviewerId,
    ClusterRevisionRef,
    ClusterState,
    DiagnosisError,
    DiagnosisErrorCode,
    DiagnosisReview,
    DiagnosisRun,
    FunctionName,
    Harness,
    HermesAgentVersion,
    HermesDiagnoser,
    MechanismKey,
    ModelFingerprint,
    ModuleName,
    ProcessCommand,
    ProcessLimits,
    PythonDiagnoser,
    PythonEntrypoint,
    Tool,
    hermes_python_command,
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
    (root / "tool.py").write_text("def search() -> str:\n    return 'result'\n", encoding="utf-8")
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
        "def diagnose_changed(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    observation = snapshot.observations[0]\n"
        "    mechanism = 'tool-schema' if observation.name == 'tool' else 'prompt-gap'\n"
        "    component = (\n"
        "        ComponentKind.TOOL if observation.name == 'tool' else ComponentKind.PROMPT\n"
        "    )\n"
        "    return TraceDiagnosis.proposed(\n"
        "        snapshot.trace.id, MechanismKey(mechanism), mechanism, 'changed diagnosis',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, observation.id.value),),\n"
        "        (component,), Severity.HIGH, 0.9,\n"
        "    )\n"
        "def invalid_anchor(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    return TraceDiagnosis.proposed(\n"
        "        snapshot.trace.id, MechanismKey('invalid-anchor'), 'invalid', 'invalid',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, 'missing'),),\n"
        "        (ComponentKind.PROMPT,), Severity.LOW, 0.5,\n"
        "    )\n"
        "def unconnected_component(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    observation = snapshot.observations[0]\n"
        "    return TraceDiagnosis.proposed(\n"
        "        snapshot.trace.id, MechanismKey('missing-component'), 'invalid', 'invalid',\n"
        "        (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, observation.id.value),),\n"
        "        (ComponentKind.MIDDLEWARE,), Severity.LOW, 0.5,\n"
        "    )\n"
        "def slow(snapshot: TraceSnapshot) -> TraceDiagnosis:\n"
        "    del snapshot\n"
        "    time.sleep(2)\n"
        "    raise RuntimeError('late')\n",
        encoding="utf-8",
    )
    (root / "fake_hermes.py").write_text(
        "from __future__ import annotations\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from pydantic import TypeAdapter\n"
        "from ofw import (ComponentKind, EvidenceAnchor, EvidenceAnchorKind, "
        "MechanismKey, Severity, TraceDiagnosis)\n"
        "from ofw.mine import TraceSnapshot\n"
        "assert not Path('prompt.md').exists()\n"
        "assert not Path('trace_snapshot.json').exists()\n"
        "assert not Path('fake_hermes.py').exists()\n"
        "assert len(sys.argv) == 5\n"
        "assert sys.argv[1] == 'azure-foundry'\n"
        "assert sys.argv[3] == 'high'\n"
        "assert sys.argv[4] == '0.20.0'\n"
        "assert all('TRACE_SNAPSHOT_JSON' not in argument for argument in sys.argv)\n"
        "if sys.argv[2] == 'invalid-output':\n"
        "    print('not-json')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "assert '\"relative_path\":\"prompt.md\"' in prompt\n"
        "assert 'fake_hermes.py' not in prompt\n"
        "snapshot_text = prompt.partition('TRACE_SNAPSHOT_JSON\\n')[2].partition("
        "'\\nCONNECTED_ASSETS_JSON\\n')[0]\n"
        "snapshot = TypeAdapter(TraceSnapshot).validate_json(snapshot_text)\n"
        "observation = snapshot.observations[0]\n"
        "name = observation.name\n"
        "mechanism = 'tool-schema' if name == 'tool' else 'prompt-gap'\n"
        "component = ComponentKind.TOOL if name == 'tool' else ComponentKind.PROMPT\n"
        "diagnosis = TraceDiagnosis.proposed(\n"
        "    snapshot.trace.id, MechanismKey(mechanism), mechanism,\n"
        "    'hermes fixture diagnosis',\n"
        "    (EvidenceAnchor(EvidenceAnchorKind.OBSERVATION, observation.id.value),),\n"
        "    (component,), Severity.HIGH, 0.95,\n"
        ")\n"
        "print(TypeAdapter(TraceDiagnosis).dump_json(diagnosis).decode())\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("diagnosis-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_tools(Tool("search", Path("tool.py")))
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
    harness.connect_tools(Tool("search", Path("tool.py")))
    harness.process()
    return result, harness


def _diagnoser(function: str, timeout: timedelta = timedelta(seconds=1)) -> PythonDiagnoser:
    return PythonDiagnoser(
        PythonEntrypoint(ModuleName("diagnoser"), FunctionName(function)),
        ProcessLimits(timeout),
    )


def _hermes_diagnoser(root: Path, model: str = "fixture-deployment") -> HermesDiagnoser:
    return HermesDiagnoser(
        ProcessCommand((sys.executable, str(root / "fake_hermes.py"))),
        ModelFingerprint("azure-foundry", model, "high"),
        HermesAgentVersion.V0_20_0,
        ProcessLimits(timedelta(seconds=2)),
        128_000,
    )


def test_verified_failures_form_evidence_bound_mechanism_clusters(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()

    assert tuple(cluster.mechanism for cluster in result.clusters) == (
        MechanismKey("prompt-gap"),
        MechanismKey("tool-schema"),
    )
    assert tuple(cluster.recurrence for cluster in result.clusters) == (1, 2)
    assert all(cluster.state is ClusterState.PROPOSED for cluster in result.clusters)
    assert all(cluster.evidence for cluster in result.clusters)
    assert result.abstained_count == 1
    assert TraceId("good") not in tuple(diagnosis.trace_id for diagnosis in result.diagnoses)


def test_hermes_diagnoser_runs_toolless_with_bounded_component_evidence(
    tmp_path: Path,
) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _hermes_diagnoser(harness.root)).run()

    assert tuple(cluster.mechanism for cluster in result.clusters) == (
        MechanismKey("prompt-gap"),
        MechanismKey("tool-schema"),
    )
    assert all(
        diagnosis.description == "hermes fixture diagnosis" for diagnosis in result.diagnoses
    )
    assert (harness.root / "prompt.md").read_text(encoding="utf-8") == "Be accurate.\n"


def test_invalid_hermes_output_abstains_fail_closed(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(
        harness,
        mine,
        _hermes_diagnoser(harness.root, "invalid-output"),
    ).run()

    assert not result.clusters
    assert result.abstained_count == 4


def test_hermes_prompt_budget_abstains_before_agent_execution(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    constrained = replace(
        _hermes_diagnoser(harness.root),
        maximum_prompt_bytes=1024,
    )

    result = DiagnosisRun(harness, mine, constrained).run()

    assert not result.clusters
    assert result.abstained_count == 4


def test_hermes_rejects_asset_content_that_no_longer_matches_revision(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    (harness.root / "prompt.md").write_text("Changed after processing.\n", encoding="utf-8")

    result = DiagnosisRun(revision, mine, _hermes_diagnoser(harness.root)).run()

    assert not result.clusters
    assert result.abstained_count == 4


def test_hermes_fingerprint_binds_model_and_prompt_budget(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    first = _hermes_diagnoser(harness.root)
    changed_model = _hermes_diagnoser(harness.root, "other-deployment")
    changed_budget = replace(first, maximum_prompt_bytes=64_000)

    assert first.fingerprint(revision.root) != changed_model.fingerprint(revision.root)
    assert first.fingerprint(revision.root) != changed_budget.fingerprint(revision.root)


def test_official_hermes_command_uses_the_stdin_bridge() -> None:
    command = hermes_python_command(Path(sys.executable))

    assert command.arguments[0] == sys.executable
    assert Path(command.arguments[1]).name == "_hermes_oneshot_bridge.py"


def test_cluster_review_is_content_bound_and_deterministic(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    diagnosis = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()
    cluster = diagnosis.clusters[0]
    review = ClusterReview(
        cluster.id,
        cluster.revision,
        cluster.content_digest,
        ClusterReviewerId("reviewer-1"),
        ClusterReviewDecision.CONFIRM,
        datetime(2026, 8, 22, 1, tzinfo=UTC),
    )
    operation = DiagnosisReview(diagnosis, (review,))

    first = operation.run()
    second = operation.run()

    reviewed = next(item for item in first.clusters if item.id == cluster.id)
    assert first == second
    assert reviewed.state is ClusterState.CONFIRMED
    assert first.reviews == (review,)
    assert first.id != diagnosis.id


def test_stale_cluster_review_is_rejected(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    diagnosis = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()
    cluster = diagnosis.clusters[0]
    stale = ClusterReview(
        cluster.id,
        cluster.revision,
        Sha256Digest("sha256:stale"),
        ClusterReviewerId("reviewer-1"),
        ClusterReviewDecision.CONFIRM,
        datetime(2026, 8, 22, 1, tzinfo=UTC),
    )

    with pytest.raises(DiagnosisError) as raised:
        DiagnosisReview(diagnosis, (stale,)).run()

    assert raised.value.code is DiagnosisErrorCode.REVIEW_INVALID


def test_rejected_cluster_remains_a_review_artifact(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    diagnosis = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()
    cluster = diagnosis.clusters[0]
    rejection = ClusterReview(
        cluster.id,
        cluster.revision,
        cluster.content_digest,
        ClusterReviewerId("reviewer-1"),
        ClusterReviewDecision.REJECT,
        datetime(2026, 8, 22, 1, tzinfo=UTC),
    )

    result = DiagnosisReview(diagnosis, (rejection,)).run()

    reviewed = next(item for item in result.clusters if item.id == cluster.id)
    assert reviewed.state is ClusterState.REJECTED


def test_changed_confirmed_cluster_requires_a_new_review(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    diagnosis = DiagnosisRun(harness, mine, _diagnoser("diagnose")).run()
    cluster = diagnosis.clusters[0]
    confirmed = DiagnosisReview(
        diagnosis,
        (
            ClusterReview(
                cluster.id,
                cluster.revision,
                cluster.content_digest,
                ClusterReviewerId("reviewer-1"),
                ClusterReviewDecision.CONFIRM,
                datetime(2026, 8, 22, 1, tzinfo=UTC),
            ),
        ),
    ).run()

    changed = DiagnosisRun(
        harness,
        mine,
        _diagnoser("diagnose_changed"),
        previous=confirmed,
    ).run()

    changed_cluster = next(item for item in changed.clusters if item.id == cluster.id)
    assert changed_cluster.revision == cluster.revision + 1
    assert changed_cluster.content_digest != cluster.content_digest
    assert changed_cluster.state is ClusterState.REOPENED


def test_invalid_evidence_anchor_becomes_abstention(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _diagnoser("invalid_anchor")).run()

    assert not result.clusters
    assert result.abstained_count == 4


def test_unconnected_component_attribution_becomes_abstention(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)

    result = DiagnosisRun(harness, mine, _diagnoser("unconnected_component")).run()

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


def test_snapshot_lineage_must_match_admission_revision_and_collection(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    admission = mine.admissions[0]
    path = admission.snapshot_path
    assert path is not None
    original = _SNAPSHOT_ADAPTER.validate_json(path.read_bytes())
    mismatches = (
        replace(original, trace=replace(original.trace, id=TraceId("swapped"))),
        replace(original, revision_id=HarnessRevisionId("other-revision")),
        replace(original, collection_digest=Sha256Digest("sha256:other-collection")),
    )

    for snapshot in mismatches:
        payload = _SNAPSHOT_ADAPTER.dump_json(snapshot)
        digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
        path.write_bytes(payload)
        changed_admission = replace(admission, snapshot_digest=digest)
        changed_mine = replace(mine, admissions=(changed_admission, *mine.admissions[1:]))
        with pytest.raises(DiagnosisError) as raised:
            DiagnosisRun(harness, changed_mine, _diagnoser("diagnose")).run()
        assert raised.value.code is DiagnosisErrorCode.ARTIFACT_INVALID


def test_cluster_identity_is_stable_across_revisions_and_tracks_parent(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    first_mine = replace(
        mine,
        admissions=tuple(
            admission for admission in mine.admissions if admission.trace_id != TraceId("tool-two")
        ),
    )
    first = DiagnosisRun(harness, first_mine, _diagnoser("diagnose")).run()
    second = DiagnosisRun(harness, mine, _diagnoser("diagnose"), previous=first).run()
    first_tool = next(
        cluster for cluster in first.clusters if cluster.mechanism == MechanismKey("tool-schema")
    )
    second_tool = next(
        cluster for cluster in second.clusters if cluster.mechanism == MechanismKey("tool-schema")
    )

    assert second_tool.id == first_tool.id
    assert second_tool.revision == 2
    assert second_tool.parents == (ClusterRevisionRef(first_tool.id, first_tool.revision),)
    assert second_tool.recurrence == 2


def test_cluster_without_current_failures_is_revisioned_as_resolved(tmp_path: Path) -> None:
    mine, harness = _mine_result(tmp_path)
    diagnoser = _diagnoser("diagnose")
    first = DiagnosisRun(harness, mine, diagnoser).run()
    without_tools = replace(
        mine,
        admissions=tuple(
            admission
            for admission in mine.admissions
            if admission.trace_id not in (TraceId("tool-one"), TraceId("tool-two"))
        ),
    )

    second = DiagnosisRun(harness, without_tools, diagnoser, previous=first).run()
    resolved = next(
        cluster for cluster in second.clusters if cluster.mechanism == MechanismKey("tool-schema")
    )

    assert resolved.state is ClusterState.RESOLVED
    assert resolved.revision == 2
    assert resolved.resolution_rate == 1.0
