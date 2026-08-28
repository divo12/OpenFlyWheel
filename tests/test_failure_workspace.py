"""Compact local failure-workspace tests."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

import ofw.evaluation.failure_workspace as failure_workspace_module
from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_workspace import (
    FailedOutcomeInput,
    FailureArtifact,
    FailureRecordObservation,
    FailureRecordStatus,
    FailureWorkspaceErrorCode,
    FailureWorkspaceFailure,
    FailureWorkspaceService,
    FileFailureWorkspace,
    RecordFailureInput,
)

_EVALUATED_AT = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
_ARTIFACT_LIMIT_BYTES = 64 * 1024
RecordResult = FailureRecordObservation | FailureWorkspaceErrorCode


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
    root_cause: str = "The agent finalized before reading state.",
) -> RecordFailureInput:
    critical = "observation-7"
    return RecordFailureInput(
        workspace_root=root,
        outcome=FailedOutcomeInput(
            trace_id="trace-1",
            task_id="task-1",
            verifier_id="itsm-bench@v1",
            evaluated_at=_EVALUATED_AT,
            score=0.0,
            evidence=("harbor://trial-1/verifier/result",),
            outcome_score_id="outcome-score-1",
        ),
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
        expected_outcome="Incident INC-123 is closed.",
        actual_outcome="Incident INC-123 remains open.",
        critical_observation_id=critical,
        evidence_observation_ids=(critical, "observation-9"),
        root_cause=root_cause,
        counterfactual_action="Read the incident state before finalizing.",
        inconclusive_reason=None,
    )


def _expected_artifact(artifact_id: str) -> FailureArtifact:
    return FailureArtifact(
        artifact_id=artifact_id,
        trace_id="trace-1",
        task_id="task-1",
        verifier_id="itsm-bench@v1",
        evaluated_at=_EVALUATED_AT,
        normalized_score=0.0,
        outcome_score_id="outcome-score-1",
        outcome_evidence=("harbor://trial-1/verifier/result",),
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
        expected_outcome="Incident INC-123 is closed.",
        actual_outcome="Incident INC-123 remains open.",
        critical_observation_id="observation-7",
        evidence_observation_ids=("observation-7", "observation-9"),
        root_cause="The agent finalized before reading state.",
        counterfactual_action="Read the incident state before finalizing.",
        inconclusive_reason=None,
    )


def _oversized_request(root: Path) -> RecordFailureInput:
    base = _request(root)
    large_text = "🧪" * 4000
    outcome = FailedOutcomeInput(
        trace_id=base.outcome.trace_id,
        task_id=base.outcome.task_id,
        verifier_id=base.outcome.verifier_id,
        evaluated_at=base.outcome.evaluated_at,
        score=base.outcome.score,
        evidence=tuple("🧪" * 1024 for _ in range(10)),
        outcome_score_id=base.outcome.outcome_score_id,
    )
    return RecordFailureInput(
        workspace_root=base.workspace_root,
        outcome=outcome,
        evidence_status=base.evidence_status,
        issue_type=base.issue_type,
        expected_outcome=large_text,
        actual_outcome=large_text,
        critical_observation_id=base.critical_observation_id,
        evidence_observation_ids=base.evidence_observation_ids,
        root_cause=large_text,
        counterfactual_action=large_text,
        inconclusive_reason=base.inconclusive_reason,
    )


def _record_after_barrier(
    service: FailureWorkspaceService,
    request: RecordFailureInput,
    barrier: Barrier,
) -> FailureRecordObservation | FailureWorkspaceErrorCode:
    barrier.wait()
    try:
        return service.record(request)
    except FailureWorkspaceFailure as error:
        return error.code


def _run_concurrent_requests(
    service: FailureWorkspaceService,
    first: RecordFailureInput,
    second: RecordFailureInput,
    barrier: Barrier,
) -> tuple[RecordResult, RecordResult]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(_record_after_barrier, service, first, barrier)
        second_result = executor.submit(_record_after_barrier, service, second, barrier)
    return first_result.result(), second_result.result()


def _success_count(results: tuple[RecordResult, ...]) -> int:
    return sum(isinstance(result, FailureRecordObservation) for result in results)


def test_records_one_typed_failure_without_dirtying_the_git_worktree(
    tmp_path: Path,
) -> None:
    root = _prepared_workspace(tmp_path)
    observation = FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))
    artifact_path = root / observation.relative_path
    artifact = FailureArtifact.model_validate_json(artifact_path.read_text(encoding="utf-8"))

    assert observation == FailureRecordObservation(
        status=FailureRecordStatus.SUCCESS,
        summary="Stored one failure diagnosis in the local workspace.",
        next_actions=("Retain the artifact path for failure analysis.",),
        artifacts=(str(observation.relative_path), observation.artifact_id),
        trace_id="trace-1",
        task_id="task-1",
        artifact_id=observation.artifact_id,
        relative_path=observation.relative_path,
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
    )
    assert artifact == _expected_artifact(observation.artifact_id)
    assert artifact_path.stat().st_size <= 64 * 1024


def test_failure_workspace_is_ignored_runtime_state(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert (root / ".workspace/.gitignore").read_text(encoding="utf-8") == "*\n"
    assert _git(root, "status", "--short") == ""


def test_recording_is_idempotent(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    service = FailureWorkspaceService(FileFailureWorkspace())
    first = service.record(_request(root))
    artifact_path = root / first.relative_path

    assert service.record(_request(root)) == first
    assert len(tuple(artifact_path.parent.glob("*.json"))) == 1


def test_conflicting_rewrites_fail_closed(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    service = FailureWorkspaceService(FileFailureWorkspace())
    first = service.record(_request(root))
    artifact_path = root / first.relative_path
    original = artifact_path.read_text(encoding="utf-8")

    with pytest.raises(FailureWorkspaceFailure) as raised:
        service.record(_request(root, "A different diagnosis."))

    assert raised.value.code is FailureWorkspaceErrorCode.ARTIFACT_CONFLICT
    assert artifact_path.read_text(encoding="utf-8") == original


def test_concurrent_conflicting_rewrites_publish_exactly_one_diagnosis(
    tmp_path: Path,
) -> None:
    root = _prepared_workspace(tmp_path)
    service = FailureWorkspaceService(FileFailureWorkspace())
    barrier = Barrier(2)
    requests = (_request(root), _request(root, "A concurrent diagnosis."))

    results = _run_concurrent_requests(service, requests[0], requests[1], barrier)

    assert _success_count(results) == 1
    assert results.count(FailureWorkspaceErrorCode.ARTIFACT_CONFLICT) == 1


def test_oversized_existing_artifact_fails_closed(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    service = FailureWorkspaceService(FileFailureWorkspace())
    first = service.record(_request(root))
    artifact_path = root / first.relative_path
    artifact_path.write_bytes(b"x" * (_ARTIFACT_LIMIT_BYTES + 1))

    with pytest.raises(FailureWorkspaceFailure) as raised:
        service.record(_request(root))

    assert raised.value.code is FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE


def test_oversized_new_artifact_fails_before_workspace_creation(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)

    with pytest.raises(FailureWorkspaceFailure) as raised:
        FailureWorkspaceService(FileFailureWorkspace()).record(_oversized_request(root))

    assert raised.value.code is FailureWorkspaceErrorCode.ARTIFACT_TOO_LARGE
    assert not (root / ".workspace").exists()


def test_recording_requires_a_prepared_workspace(tmp_path: Path) -> None:
    root = tmp_path / "ordinary-directory"
    root.mkdir()

    with pytest.raises(FailureWorkspaceFailure) as raised:
        FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert raised.value.code is FailureWorkspaceErrorCode.INVALID_WORKSPACE
    assert not (root / ".workspace").exists()


def test_workspace_symlink_cannot_escape_the_prepared_root(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".workspace").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FailureWorkspaceFailure) as raised:
        FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert raised.value.code is FailureWorkspaceErrorCode.INVALID_WORKSPACE
    assert tuple(outside.iterdir()) == ()


def test_symlink_swap_after_validation_cannot_redirect_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = failure_workspace_module._prepare_workspace_directories

    def prepare_then_swap(prepared_root: Path, workspace: Path, failures: Path) -> None:
        original(prepared_root, workspace, failures)
        failures.rmdir()
        failures.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        failure_workspace_module,
        "_prepare_workspace_directories",
        prepare_then_swap,
    )

    with pytest.raises(FailureWorkspaceFailure):
        FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert tuple(outside.iterdir()) == ()


def test_record_input_rejects_relative_workspace_and_extra_fields(tmp_path: Path) -> None:
    request = _request(_prepared_workspace(tmp_path))

    assert RecordFailureInput.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        RecordFailureInput.model_validate_json(
            request.model_dump_json().removesuffix("}") + ',"unexpected":"field"}'
        )
    with pytest.raises(ValidationError):
        RecordFailureInput(
            workspace_root=Path("relative"),
            outcome=request.outcome,
            evidence_status=request.evidence_status,
            issue_type=request.issue_type,
            expected_outcome=request.expected_outcome,
            actual_outcome=request.actual_outcome,
            critical_observation_id=request.critical_observation_id,
            evidence_observation_ids=request.evidence_observation_ids,
            root_cause=request.root_cause,
            counterfactual_action=request.counterfactual_action,
            inconclusive_reason=request.inconclusive_reason,
        )
