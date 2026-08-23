"""Reproducible benchmark runner, baseline, and bounded simulation."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ofw import (
    BenchmarkError,
    BenchmarkErrorCode,
    BenchmarkPolicy,
    BenchmarkRunner,
    BenchmarkStatus,
    DockerCompose,
    FunctionName,
    Harness,
    LocalProcess,
    ModuleName,
    ProcessLimits,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
    ServiceName,
    VerifierVerdict,
)
from ofw.contracts import HarnessRevision, Sha256Digest
from ofw.exports import (
    Benchmark,
    ClusterFamilyId,
    ConsentStatus,
    DataLicense,
    EvalCase,
    EvalSuite,
    ExportBundle,
    ExportPartition,
    GoodTraceDataset,
    LedgerEntry,
    MemoryPatchSet,
    PartitionLedger,
    PrivacyTransform,
    SnapshotReference,
    TraceFamilyId,
)
from ofw.observability.langfuse.domain import TraceId


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _harness(
    tmp_path: Path,
    *,
    loop_function: str = "stable",
    verifier_function: str = "passes",
) -> Harness:
    root = tmp_path / "benchmark-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "agent_loop.py").write_text(
        "from __future__ import annotations\n"
        "import time\n"
        "def stable(value: str) -> str:\n"
        "    return value\n"
        "def unstable(value: str) -> str:\n"
        "    return value + str(time.time_ns())\n",
        encoding="utf-8",
    )
    (root / "verifiers.py").write_text(
        "from __future__ import annotations\n"
        "from ofw import RunResult, VerifierResult, VerifierVerdict\n"
        "def passes(result: RunResult) -> VerifierResult:\n"
        "    return VerifierResult(VerifierVerdict.PASS, 1.0, 'pass')\n"
        "def errors(result: RunResult) -> VerifierResult:\n"
        "    del result\n"
        "    raise RuntimeError('fixture error')\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("benchmark-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    harness.connect_lifecycle(
        PythonLoop(PythonEntrypoint(ModuleName("agent_loop"), FunctionName(loop_function)))
    )
    harness.connect_verifiers(
        PythonVerifier(
            "benchmark",
            PythonEntrypoint(ModuleName("verifiers"), FunctionName(verifier_function)),
        )
    )
    harness.process()
    return harness


def _bundle(revision: HarnessRevision) -> ExportBundle:
    developer_snapshot = _snapshot_reference(revision, "developer")
    selection_snapshot = _snapshot_reference(revision, "selection")
    admission_snapshot = _snapshot_reference(revision, "admission")
    case = EvalCase(
        "developer-case",
        TraceId("developer-trace"),
        TraceFamilyId("developer-family"),
        ClusterFamilyId("developer-cluster"),
        ExportPartition.FRONTIER,
        developer_snapshot,
        (),
    )
    selection_case = EvalCase(
        "selection-case",
        TraceId("selection-trace"),
        TraceFamilyId("selection-family"),
        ClusterFamilyId("selection-cluster"),
        ExportPartition.SELECTION,
        selection_snapshot,
        (),
    )
    admission_case = EvalCase(
        "admission-case",
        TraceId("admission-trace"),
        TraceFamilyId("admission-family"),
        ClusterFamilyId("admission-cluster"),
        ExportPartition.ADMISSION,
        admission_snapshot,
        (),
    )
    root = revision.root / ".ofw" / "mine" / "exports" / "fixture"
    developer = EvalSuite("developer", revision.id, (case,), root / "developer.json")
    selection = EvalSuite("selection", revision.id, (selection_case,), root / "selection.json")
    admission = EvalSuite("admission", revision.id, (admission_case,), root / "admission.json")
    runtime = revision.runtime
    assert runtime is not None
    benchmark = Benchmark(
        "benchmark-fixture",
        revision.id,
        developer.id,
        selection.id,
        admission.id,
        runtime.execution,
        runtime.lifecycle,
        root / "benchmark.json",
    )
    return ExportBundle(
        "exports-fixture",
        None,
        revision.id,
        PartitionLedger(
            (
                LedgerEntry(
                    case.trace_id,
                    case.family_id,
                    case.cluster_family_id,
                    case.partition,
                    case.snapshot,
                ),
                LedgerEntry(
                    selection_case.trace_id,
                    selection_case.family_id,
                    selection_case.cluster_family_id,
                    selection_case.partition,
                    selection_case.snapshot,
                ),
                LedgerEntry(
                    admission_case.trace_id,
                    admission_case.family_id,
                    admission_case.cluster_family_id,
                    admission_case.partition,
                    admission_case.snapshot,
                ),
            )
        ),
        GoodTraceDataset(
            "good",
            revision.id,
            DataLicense("fixture-approved"),
            ConsentStatus.APPROVED,
            PrivacyTransform.METADATA_ONLY,
            (),
            root / "good.json",
        ),
        developer,
        selection,
        admission,
        MemoryPatchSet("memory", revision.id, (), root / "memory.json"),
        benchmark,
        revision.root,
    )


def _snapshot_reference(revision: HarnessRevision, label: str) -> SnapshotReference:
    payload = f'{{"schema":"safe-snapshot","label":"{label}","observations":[]}}'.encode()
    digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
    snapshot = revision.root / ".ofw" / f"benchmark-{label}.json"
    snapshot.write_bytes(payload)
    return SnapshotReference(snapshot, digest)


def _policy(*, max_attempts: int = 10) -> BenchmarkPolicy:
    return BenchmarkPolicy(
        repeats=2,
        max_attempts=max_attempts,
        simulation_copies=1,
        synthetic_weight=0.25,
    )


def _reset_failure_harness(tmp_path: Path) -> Harness:
    harness = _harness(tmp_path)
    root = harness.root
    compose = root / "compose.yaml"
    compose.write_text(
        "services:\n  agent:\n    image: fixture-agent:latest\n",
        encoding="utf-8",
    )
    executable = root / "fake_docker.py"
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys\nraise SystemExit(1 if 'down' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    harness.connect_execute(
        DockerCompose(
            Path("compose.yaml"),
            ServiceName("agent"),
            executable,
            ProcessLimits(timedelta(seconds=2)),
        )
    )
    harness.process()
    return harness


def test_baseline_is_reproducible_and_holdouts_remain_sealed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    runner = BenchmarkRunner(harness, bundle, _policy())

    baseline = runner.establish_baseline()
    result = runner.verify_baseline(baseline)

    assert baseline.path.is_file()
    assert result.status is BenchmarkStatus.COMPLETE
    assert len(result.attempts) == 4
    assert all(
        attempt.case_id not in ("selection-case", "admission-case") for attempt in result.attempts
    )
    assert sum(attempt.synthetic for attempt in result.attempts) == 2
    assert all(attempt.weight == 0.25 for attempt in result.attempts if attempt.synthetic)


def test_unstable_baseline_aborts_on_semantic_drift(tmp_path: Path) -> None:
    harness = _harness(tmp_path, loop_function="unstable")
    revision = harness.current_revision
    assert revision is not None
    runner = BenchmarkRunner(harness, _bundle(revision), _policy())
    baseline = runner.establish_baseline()

    with pytest.raises(BenchmarkError) as raised:
        runner.verify_baseline(baseline)

    assert raised.value.code is BenchmarkErrorCode.BASELINE_DRIFT


def test_verifier_error_is_recorded_as_failure_not_dropped(tmp_path: Path) -> None:
    harness = _harness(tmp_path, verifier_function="errors")
    revision = harness.current_revision
    assert revision is not None

    result = BenchmarkRunner(harness, _bundle(revision), _policy()).run()

    assert not result.attempts[0].passed
    assert result.attempts[0].verifiers[0].verdict is VerifierVerdict.ERROR
    assert result.weighted_pass_rate == 0


def test_hard_attempt_budget_returns_explicit_partial_result(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None

    result = BenchmarkRunner(
        harness,
        _bundle(revision),
        _policy(max_attempts=2),
    ).run()

    assert result.status is BenchmarkStatus.BUDGET_EXHAUSTED
    assert len(result.attempts) == 2


def test_holdout_case_in_developer_suite_fails_before_execution(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    leaked = replace(bundle, developer_evals=bundle.selection_holdout)

    with pytest.raises(BenchmarkError) as raised:
        BenchmarkRunner(harness, leaked, _policy()).run()

    assert raised.value.code is BenchmarkErrorCode.HOLDOUT_LEAK


def test_relabelled_holdout_case_still_fails_ledger_check(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    forged_case = replace(
        bundle.admission_holdout.cases[0],
        partition=ExportPartition.FRONTIER,
    )
    forged_suite = replace(bundle.developer_evals, cases=(forged_case,))

    with pytest.raises(BenchmarkError) as raised:
        BenchmarkRunner(harness, replace(bundle, developer_evals=forged_suite), _policy()).run()

    assert raised.value.code is BenchmarkErrorCode.HOLDOUT_LEAK


def test_authorized_labels_cannot_swap_in_holdout_snapshot(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    forged_case = replace(
        bundle.developer_evals.cases[0],
        snapshot=bundle.admission_holdout.cases[0].snapshot,
    )
    forged_suite = replace(bundle.developer_evals, cases=(forged_case,))

    with pytest.raises(BenchmarkError) as raised:
        BenchmarkRunner(harness, replace(bundle, developer_evals=forged_suite), _policy()).run()

    assert raised.value.code is BenchmarkErrorCode.HOLDOUT_LEAK


def test_reset_failure_persists_completed_attempt_evidence(tmp_path: Path) -> None:
    harness = _reset_failure_harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    policy = BenchmarkPolicy(1, 1, 0, 0.25)

    result = BenchmarkRunner(harness, _bundle(revision), policy).run()

    assert result.status is BenchmarkStatus.ENVIRONMENT_ERROR
    assert len(result.attempts) == 1
    assert result.manifest_path.is_file()
