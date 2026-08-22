"""Paired A/B gates, finalist selection, admission, and rollback."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from ofw import (
    BenchmarkPolicy,
    CandidateBuilder,
    CandidateEvidence,
    CandidatePolicy,
    ChangePrediction,
    ClusterId,
    ComponentKind,
    FileEdit,
    FitCampaign,
    FitError,
    FitErrorCode,
    FitPolicy,
    FunctionName,
    Harness,
    LocalProcess,
    ModuleName,
    ProcessLimits,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
    Tool,
    ofw,
)
from ofw.candidate import CandidateBuild
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


def _harness(tmp_path: Path) -> Harness:
    root = tmp_path / "fit-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "tool.py").write_text(
        "def run(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    (root / "agent_loop.py").write_text(
        "from tool import run\ndef run_case(value: str) -> str:\n    return run(value)\n",
        encoding="utf-8",
    )
    (root / "verifiers.py").write_text(
        "from __future__ import annotations\n"
        "from ofw import RunResult, VerifierResult, VerifierVerdict\n"
        "def verify(result: RunResult) -> VerifierResult:\n"
        "    output = result.output or ''\n"
        "    frontier = 'frontier' in output or 'selection' in output or 'admission' in output\n"
        "    passed = ('FIXED' in output) if frontier else ('BROKEN' not in output)\n"
        "    verdict = VerifierVerdict.PASS if passed else VerifierVerdict.FAIL\n"
        "    return VerifierResult(verdict, 1.0 if passed else 0.0, 'fixture')\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("fit-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_tools(Tool("run", ofw.editable(Path("tool.py"))))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    harness.connect_lifecycle(
        PythonLoop(PythonEntrypoint(ModuleName("agent_loop"), FunctionName("run_case")))
    )
    harness.connect_verifiers(
        PythonVerifier(
            "fixture",
            PythonEntrypoint(ModuleName("verifiers"), FunctionName("verify")),
        )
    )
    harness.process()
    return harness


def _snapshot(revision: HarnessRevision, label: str) -> SnapshotReference:
    payload = f'{{"label":"{label}"}}'.encode()
    digest = Sha256Digest(f"sha256:{hashlib.sha256(payload).hexdigest()}")
    path = revision.root / ".ofw" / f"fit-{label}.json"
    path.write_bytes(payload)
    return SnapshotReference(path, digest)


def _case(
    revision: HarnessRevision,
    name: str,
    partition: ExportPartition,
    *,
    critical: bool = False,
) -> EvalCase:
    snapshot = _snapshot(revision, name)
    return EvalCase(
        name,
        TraceId(name),
        TraceFamilyId(f"family-{name}"),
        ClusterFamilyId(f"cluster-{name}"),
        partition,
        snapshot,
        (),
        critical=critical,
    )


def _bundle(revision: HarnessRevision) -> ExportBundle:
    frontier = _case(revision, "frontier-case", ExportPartition.FRONTIER)
    regression = _case(
        revision,
        "regression-case",
        ExportPartition.REGRESSION,
        critical=True,
    )
    selection = _case(revision, "selection-case", ExportPartition.SELECTION)
    admission = _case(revision, "admission-case", ExportPartition.ADMISSION)
    ledger = PartitionLedger(
        tuple(
            LedgerEntry(
                case.trace_id,
                case.family_id,
                case.cluster_family_id,
                case.partition,
                case.snapshot,
            )
            for case in (frontier, regression, selection, admission)
        )
    )
    root = revision.root / ".ofw" / "fit-export"
    developer = EvalSuite("developer", revision.id, (frontier, regression), root / "developer.json")
    selection_suite = EvalSuite("selection", revision.id, (selection,), root / "selection.json")
    admission_suite = EvalSuite("admission", revision.id, (admission,), root / "admission.json")
    runtime = revision.runtime
    assert runtime is not None
    benchmark = Benchmark(
        "fit-benchmark",
        revision.id,
        developer.id,
        selection_suite.id,
        admission_suite.id,
        runtime.execution,
        runtime.lifecycle,
        root / "benchmark.json",
    )
    return ExportBundle(
        "fit-exports",
        None,
        revision.id,
        ledger,
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
        selection_suite,
        admission_suite,
        MemoryPatchSet("memory", revision.id, (), root / "memory.json"),
        benchmark,
        revision.root,
    )


def _candidate(
    revision: HarnessRevision,
    replacement: str,
    hypothesis: str,
) -> CandidateBuild:
    tool = revision.root / "tool.py"
    digest = Sha256Digest(f"sha256:{hashlib.sha256(tool.read_bytes()).hexdigest()}")
    evidence = CandidateEvidence(
        revision.id,
        (ClusterId("cluster-frontier"),),
        ("regression-case",),
        (),
    )
    prediction = ChangePrediction(
        hypothesis,
        (ClusterId("cluster-frontier"),),
        ("regression-case",),
        (ComponentKind.TOOL,),
        (),
        0.5,
        0.0,
        0.0,
    )
    return CandidateBuilder(
        revision,
        evidence,
        CandidatePolicy(1, 4096, (ComponentKind.TOOL,)),
    ).create((FileEdit(Path("tool.py"), digest, replacement),), prediction)


def _fit_policy() -> FitPolicy:
    return FitPolicy(
        minimum_target_delta=0.5,
        minimum_regression_score=1.0,
        maximum_critical_regressions=0,
        maximum_latency_delta=1.0,
        maximum_cost_delta=0.0,
        minimum_selection_pass_rate=1.0,
        minimum_admission_pass_rate=1.0,
    )


def test_paired_gates_reject_regression_and_admit_one_winner(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    good = _candidate(
        revision,
        "def run(value: str) -> str:\n"
        "    return value + (' FIXED' if 'regression' not in value else '')\n",
        "Fix frontier only.",
    )
    bad = _candidate(
        revision,
        "def run(value: str) -> str:\n    return value + ' BROKEN FIXED'\n",
        "Break regression.",
    )
    campaign = FitCampaign(
        harness,
        bundle,
        BenchmarkPolicy(1, 10, 0, 0.25),
        _fit_policy(),
        (good, bad),
    )

    result = campaign.run()

    assert campaign.run() == result
    assert result.winner_id == good.candidate.id
    good_outcome = next(
        outcome for outcome in result.outcomes if outcome.candidate_id == good.candidate.id
    )
    bad_outcome = next(
        outcome for outcome in result.outcomes if outcome.candidate_id == bad.candidate.id
    )
    assert good_outcome.selection_result is not None
    assert good_outcome.admission_result is not None
    assert bad_outcome.critical_regressions == 1
    assert bad_outcome.selection_result is None
    assert not bad.workspace.root.exists()
    assert good.workspace.root.exists()
    assert any(
        delta.case_id == "frontier-case" and delta.pass_delta == 1 for delta in good_outcome.deltas
    )
    good.workspace.close()


def test_admission_failure_returns_no_winner_and_discards_finalist(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    bundle = _bundle(revision)
    candidate = _candidate(
        revision,
        "def run(value: str) -> str:\n"
        "    return value + (' FIXED' if 'frontier' in value else '')\n",
        "Fix frontier but not admission.",
    )

    result = FitCampaign(
        harness,
        bundle,
        BenchmarkPolicy(1, 10, 0, 0.25),
        _fit_policy(),
        (candidate,),
    ).run()

    assert result.winner_id is None
    assert not candidate.workspace.root.exists()


def test_candidate_drift_is_rejected_before_baseline_or_holdouts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    candidate = _candidate(
        revision,
        "def run(value: str) -> str:\n    return value + ' FIXED'\n",
        "Declared edit.",
    )
    (candidate.workspace.root / "tool.py").write_text(
        "def run(value: str) -> str:\n    return value + ' UNDECLARED'\n",
        encoding="utf-8",
    )

    with pytest.raises(FitError) as raised:
        FitCampaign(
            harness,
            _bundle(revision),
            BenchmarkPolicy(1, 10, 0, 0.25),
            _fit_policy(),
            (candidate,),
        ).run()

    assert raised.value.code is FitErrorCode.CANDIDATE_DRIFT
    assert not candidate.workspace.root.exists()
