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
    FunctionName,
    Harness,
    LocalProcess,
    ModuleName,
    ProcessLimits,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
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
    payload = b'{"schema":"safe-snapshot","observations":[]}'
    digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
    snapshot = revision.root / ".ofw" / "benchmark-fixture.json"
    snapshot.write_bytes(payload)
    case = EvalCase(
        "developer-case",
        TraceId("developer-trace"),
        TraceFamilyId("developer-family"),
        ClusterFamilyId("developer-cluster"),
        ExportPartition.FRONTIER,
        SnapshotReference(snapshot, digest),
        (),
    )
    selection_case = EvalCase(
        "selection-case",
        TraceId("selection-trace"),
        TraceFamilyId("selection-family"),
        ClusterFamilyId("selection-cluster"),
        ExportPartition.SELECTION,
        SnapshotReference(snapshot, digest),
        (),
    )
    admission_case = EvalCase(
        "admission-case",
        TraceId("admission-trace"),
        TraceFamilyId("admission-family"),
        ClusterFamilyId("admission-cluster"),
        ExportPartition.ADMISSION,
        SnapshotReference(snapshot, digest),
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
        PartitionLedger(()),
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


def _policy(*, max_attempts: int = 10) -> BenchmarkPolicy:
    return BenchmarkPolicy(
        repeats=2,
        max_attempts=max_attempts,
        simulation_copies=1,
        synthetic_weight=0.25,
    )


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
