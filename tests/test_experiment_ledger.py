"""Immutable experiment-attempt ledger tests."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.contracts import GitCommit
from ofw.evaluation.experiment_ledger import (
    ExperimentArtifact,
    ExperimentAttempt,
    ExperimentDecision,
    ExperimentId,
    ExperimentLedgerErrorCode,
    ExperimentLedgerFailure,
    ExperimentLedgerService,
    ExperimentRecordObservation,
    ExperimentRecordStatus,
    ExperimentRunId,
    FileExperimentLedger,
    RecordExperimentInput,
)
from ofw.observability.langfuse.domain import ScoreId

_DECIDED_AT = datetime(2026, 9, 2, 8, 30, tzinfo=UTC)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _prepared_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    root.mkdir()
    (root / "PROGRAM.md").write_text("# Program\n", encoding="utf-8")
    (root / "experiment_config.yaml").write_text("benchmark: itsm-bench\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "PROGRAM.md", "experiment_config.yaml")
    _git(root, "commit", "-qm", "prepare")
    return root


def _request(
    root: Path,
    *,
    hypothesis: str = "Require the agent to confirm incident state before finalizing.",
    workspace_root: Path | None = None,
    verifier_receipts: tuple[str, ...] = ("score-task-1", "score-task-2"),
    gate_decision: ExperimentDecision = ExperimentDecision.ADMIT,
    total_cost_usd: float | None = 0.42,
    latency_seconds: float | None = 73.5,
    rejection_reason: str | None = None,
    experiment_id: str = "itsm-hermes-demo",
    parent_revision: str | None = None,
) -> RecordExperimentInput:
    return RecordExperimentInput(
        workspace_root=workspace_root or root,
        experiment_id=experiment_id,
        run_id="run-001",
        parent_revision=parent_revision or _git(root, "rev-parse", "HEAD"),
        hypothesis=hypothesis,
        verifier_receipts=verifier_receipts,
        gate_decision=gate_decision,
        total_cost_usd=total_cost_usd,
        latency_seconds=latency_seconds,
        rejection_reason=rejection_reason,
        decided_at=_DECIDED_AT,
    )


def test_records_one_typed_experiment_attempt_without_dirtying_the_worktree(
    tmp_path: Path,
) -> None:
    root = _prepared_workspace(tmp_path)
    request = _request(root)

    observation = ExperimentLedgerService(FileExperimentLedger()).record(request)
    expected_path = Path(f".workspace/experiments/{observation.artifact_id}.json")
    artifact = ExperimentArtifact.model_validate_json(
        (root / expected_path).read_text(encoding="utf-8")
    )

    assert observation == ExperimentRecordObservation(
        status=ExperimentRecordStatus.SUCCESS,
        summary="Stored one experiment attempt in the local ledger.",
        next_actions=("Retain the artifact path with the candidate decision.",),
        artifacts=(str(expected_path), observation.artifact_id),
        experiment_id="itsm-hermes-demo",
        run_id="run-001",
        artifact_id=observation.artifact_id,
        relative_path=expected_path,
        gate_decision=ExperimentDecision.ADMIT,
    )
    assert artifact == ExperimentArtifact(
        artifact_id=observation.artifact_id,
        experiment_id="itsm-hermes-demo",
        run_id="run-001",
        parent_revision=request.parent_revision,
        hypothesis="Require the agent to confirm incident state before finalizing.",
        verifier_receipts=("score-task-1", "score-task-2"),
        gate_decision=ExperimentDecision.ADMIT,
        total_cost_usd=0.42,
        latency_seconds=73.5,
        rejection_reason=None,
        decided_at=_DECIDED_AT,
    )
    assert _git(root, "status", "--short") == ""


def test_recording_is_idempotent_and_conflicting_rewrites_fail_closed(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    service = ExperimentLedgerService(FileExperimentLedger())
    first = service.record(_request(root))
    artifact_path = root / first.relative_path
    original = artifact_path.read_text(encoding="utf-8")

    assert service.record(_request(root)) == first
    with pytest.raises(ExperimentLedgerFailure) as raised:
        service.record(_request(root, hypothesis="A conflicting hypothesis."))

    assert raised.value.code is ExperimentLedgerErrorCode.ARTIFACT_CONFLICT
    assert artifact_path.read_text(encoding="utf-8") == original
    assert len(tuple(artifact_path.parent.glob("*.json"))) == 1


def test_rejected_attempt_can_preserve_missing_measurements(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    request = RecordExperimentInput(
        workspace_root=root,
        experiment_id="itsm-hermes-demo",
        run_id="run-002",
        parent_revision=_git(root, "rev-parse", "HEAD"),
        hypothesis="Add a state check before finalizing.",
        verifier_receipts=(),
        gate_decision=ExperimentDecision.REJECT,
        total_cost_usd=None,
        latency_seconds=None,
        rejection_reason="Verifier evidence was unavailable.",
        decided_at=_DECIDED_AT,
    )

    observation = ExperimentLedgerService(FileExperimentLedger()).record(request)
    artifact = ExperimentArtifact.model_validate_json(
        (root / observation.relative_path).read_text(encoding="utf-8")
    )

    assert (
        artifact.gate_decision,
        artifact.verifier_receipts,
        artifact.total_cost_usd,
        artifact.latency_seconds,
        artifact.rejection_reason,
    ) == (
        ExperimentDecision.REJECT,
        (),
        None,
        None,
        "Verifier evidence was unavailable.",
    )


def test_input_rejects_inconsistent_decisions_and_untrusted_values(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    request = _request(root)

    with pytest.raises(ValidationError):
        RecordExperimentInput.model_validate_json(
            request.model_dump_json().removesuffix("}") + ',"unexpected":"field"}'
        )
    with pytest.raises(ValidationError):
        _request(root, workspace_root=Path("relative"))
    with pytest.raises(ValidationError):
        _request(root, verifier_receipts=())
    with pytest.raises(ValidationError):
        _request(root, rejection_reason="A rejection reason on an admitted attempt.")
    with pytest.raises(ValidationError):
        _request(
            root,
            gate_decision=ExperimentDecision.REJECT,
            verifier_receipts=(),
            total_cost_usd=None,
            latency_seconds=None,
        )
    with pytest.raises(ValidationError):
        _request(root, total_cost_usd=-0.01)
    with pytest.raises(ValidationError):
        _request(root, latency_seconds=float("inf"))
    with pytest.raises(ValidationError):
        _request(root, verifier_receipts=("score-task-1", "score-task-1"))
    with pytest.raises(ValidationError):
        _request(root, experiment_id="itsm-demo!")
    with pytest.raises(ValidationError):
        _request(root, parent_revision="a" * 40 + "trailing")
    with pytest.raises(ValidationError):
        ExperimentArtifact(
            artifact_id="00000000-0000-0000-0000-000000000001",
            experiment_id=request.experiment_id,
            run_id=request.run_id,
            parent_revision=request.parent_revision,
            hypothesis=request.hypothesis,
            verifier_receipts=(),
            gate_decision=ExperimentDecision.ADMIT,
            total_cost_usd=request.total_cost_usd,
            latency_seconds=request.latency_seconds,
            rejection_reason=None,
            decided_at=request.decided_at,
        )


def test_admitted_input_may_omit_rejection_reason(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)

    request = RecordExperimentInput(
        workspace_root=root,
        experiment_id="itsm-hermes-demo",
        run_id="run-001",
        parent_revision=_git(root, "rev-parse", "HEAD"),
        hypothesis="Confirm state before finalizing.",
        verifier_receipts=("score-task-1",),
        gate_decision=ExperimentDecision.ADMIT,
        total_cost_usd=0.42,
        latency_seconds=73.5,
        decided_at=_DECIDED_AT,
    )

    assert request.rejection_reason is None


def test_direct_domain_attempt_rejects_invalid_measurements() -> None:
    with pytest.raises(ExperimentLedgerFailure) as raised:
        ExperimentAttempt(
            experiment_id=ExperimentId("itsm-hermes-demo"),
            run_id=ExperimentRunId("run-001"),
            parent_revision=GitCommit("a" * 40),
            hypothesis="Confirm state before finalizing.",
            verifier_receipts=(ScoreId("score-task-1"),),
            gate_decision=ExperimentDecision.ADMIT,
            total_cost_usd=-0.01,
            latency_seconds=73.5,
            rejection_reason=None,
            decided_at=_DECIDED_AT,
        )

    assert raised.value.code is ExperimentLedgerErrorCode.INVALID_ATTEMPT


def test_oversized_attempt_fails_before_workspace_creation(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    receipts = tuple(f"score-{index}-{'x' * 240}" for index in range(500))
    request = _request(root, verifier_receipts=receipts)

    with pytest.raises(ExperimentLedgerFailure) as raised:
        ExperimentLedgerService(FileExperimentLedger()).record(request)

    assert raised.value.code is ExperimentLedgerErrorCode.ARTIFACT_TOO_LARGE
    assert raised.value.subject == "00a21ba0-def2-5594-b61b-f4edc1af5b6e"
    assert not (root / ".workspace").exists()


def test_artifact_directory_must_be_a_directory(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    workspace = root / ".workspace"
    workspace.mkdir()
    (workspace / "experiments").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ExperimentLedgerFailure) as raised:
        ExperimentLedgerService(FileExperimentLedger()).record(_request(root))

    assert raised.value.code is ExperimentLedgerErrorCode.INVALID_WORKSPACE
    assert raised.value.subject == "experiments"


def test_existing_workspace_ignore_must_cover_runtime_artifacts(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    workspace = root / ".workspace"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("failures/\n", encoding="utf-8")

    with pytest.raises(ExperimentLedgerFailure) as raised:
        ExperimentLedgerService(FileExperimentLedger()).record(_request(root))

    assert raised.value.code is ExperimentLedgerErrorCode.INVALID_WORKSPACE
    assert not tuple(workspace.rglob("*.json"))
