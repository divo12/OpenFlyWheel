"""Compact local failure-workspace tests."""

from __future__ import annotations

import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from pydantic import ValidationError

import ofw.evaluation.failure_workspace as failure_workspace_module
from ofw.contracts import ComponentKind, Sha256Digest
from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_curation import (
    DeferredFailureInput,
    FailureCuration,
    FailureCurationArtifact,
    FailureCurationErrorCode,
    FailureCurationFailure,
    FailureCurationService,
    FailureGroup,
    FailureGroupInput,
    FailureGroupMember,
    RecordFailureCurationInput,
)
from ofw.evaluation.failure_workspace import (
    FailedOutcomeInput,
    FailureArtifact,
    FailureRecordObservation,
    FailureRecordStatus,
    FailureWorkspaceErrorCode,
    FailureWorkspaceFailure,
    FailureWorkspaceService,
    FileFailureCurationWorkspace,
    FileFailureWorkspace,
    RecordFailureInput,
)
from ofw.evaluation.outcome import TaskId
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId

_EVALUATED_AT = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
_ARTIFACT_LIMIT_BYTES = 64 * 1024
RecordResult = FailureRecordObservation | FailureWorkspaceErrorCode
RecordedFailures = tuple[tuple[str, str, str], tuple[Path, Path, Path]]


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
    *,
    trace_id: str = "trace-1",
    task_id: str = "task-1",
    outcome_score_id: str = "outcome-score-1",
    critical_observation_id: str = "observation-7",
) -> RecordFailureInput:
    return RecordFailureInput(
        workspace_root=root,
        outcome=FailedOutcomeInput(
            trace_id=trace_id,
            task_id=task_id,
            verifier_id="itsm-bench@v1",
            evaluated_at=_EVALUATED_AT,
            score=0.0,
            evidence=("harbor://trial-1/verifier/result",),
            outcome_score_id=outcome_score_id,
        ),
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
        expected_outcome="Incident INC-123 is closed.",
        actual_outcome="Incident INC-123 remains open.",
        critical_observation_id=critical_observation_id,
        evidence_observation_ids=(critical_observation_id, "observation-9"),
        root_cause=root_cause,
        counterfactual_action="Read the incident state before finalizing.",
        inconclusive_reason=None,
    )


def _curation_request(root: Path, artifact_ids: tuple[str, str, str]) -> RecordFailureCurationInput:
    return RecordFailureCurationInput(
        workspace_root=root,
        source_artifact_ids=artifact_ids,
        groups=(
            FailureGroupInput(
                pattern_key="finalizes-before-verification",
                title="Finalizes before verifying state",
                mechanism="The control loop treats a successful mutation as completion.",
                prevention="Require a state read after mutation and before finalization.",
                target_component=ComponentKind.PROMPT,
                failure_artifact_ids=(artifact_ids[0], artifact_ids[1]),
            ),
        ),
        deferred=(
            DeferredFailureInput(
                failure_artifact_id=artifact_ids[2],
                reason="No second task supports this mechanism yet.",
            ),
        ),
    )


def _record_failures(root: Path) -> RecordedFailures:
    service = FailureWorkspaceService(FileFailureWorkspace())
    receipts = tuple(
        service.record(
            _request(
                root,
                trace_id=f"trace-{index}",
                task_id=f"task-{index}",
                outcome_score_id=f"score-{index}",
                critical_observation_id=f"observation-{index}",
            )
        )
        for index in range(1, 4)
    )
    return (
        (receipts[0].artifact_id, receipts[1].artifact_id, receipts[2].artifact_id),
        (
            root / receipts[0].relative_path,
            root / receipts[1].relative_path,
            root / receipts[2].relative_path,
        ),
    )


def _group_member(index: int) -> FailureGroupMember:
    return FailureGroupMember(
        artifact_id=str(UUID(int=index)),
        artifact_digest=Sha256Digest(f"sha256:{index:064x}"),
        trace_id=TraceId(f"trace-{index}"),
        task_id=TaskId(f"task-{index}"),
        outcome_score_id=ScoreId(f"score-{index}"),
        critical_observation_id=ObservationId(f"observation-{index}"),
    )


def _oversized_curation() -> FailureCuration:
    members = tuple(_group_member(index) for index in range(1, 51))
    groups = tuple(
        FailureGroup(
            id=str(UUID(int=100 + index)),
            pattern_key=f"pattern-{index}",
            title="T" * 160,
            mechanism="M" * 1000,
            prevention="P" * 1000,
            target_component=ComponentKind.PROMPT,
            issue_type=FailureType.CONTROL_FLOW_FAILURE,
            members=(members[index * 2], members[index * 2 + 1]),
        )
        for index in range(25)
    )
    return FailureCuration(
        id=str(UUID(int=999)),
        source_artifact_ids=tuple(member.artifact_id for member in members),
        groups=groups,
        deferred=(),
    )


def _expected_artifact(artifact_id: str) -> FailureArtifact:
    return FailureArtifact(
        artifact_id=artifact_id,
        content_digest="sha256:e712d9d50d334533c1dd8ec75011478d588392d806a1e99633e386aede9e5ba8",
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


def test_published_artifact_fsyncs_the_failure_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_workspace(tmp_path)
    failures = root / ".workspace/failures"
    original = os.fsync
    synced_after_publication = False

    def fsync(descriptor: int) -> None:
        nonlocal synced_after_publication
        opened = os.fstat(descriptor)
        if stat.S_ISDIR(opened.st_mode) and failures.exists():
            current = failures.stat()
            if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                synced_after_publication = any(failures.glob("*.json")) and not any(
                    failures.glob(".ofw-*.tmp")
                )
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)

    FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert synced_after_publication


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

    def prepare_then_swap(
        prepared_root: Path,
        workspace: Path,
        failures: Path,
    ) -> failure_workspace_module._DirectoryChainIdentity:
        identity = original(prepared_root, workspace, failures)
        failures.rmdir()
        failures.symlink_to(outside, target_is_directory=True)
        return identity

    monkeypatch.setattr(
        failure_workspace_module,
        "_prepare_workspace_directories",
        prepare_then_swap,
    )

    with pytest.raises(FailureWorkspaceFailure):
        FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert tuple(outside.iterdir()) == ()


def test_workspace_directory_swap_after_validation_cannot_receive_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _prepared_workspace(tmp_path)
    replacement = tmp_path / "replacement"
    displaced = tmp_path / "validated-workspace"
    (replacement / "failures").mkdir(parents=True)
    original = failure_workspace_module._prepare_workspace_directories

    def prepare_then_swap(
        prepared_root: Path,
        workspace: Path,
        failures: Path,
    ) -> failure_workspace_module._DirectoryChainIdentity:
        identity = original(prepared_root, workspace, failures)
        workspace.rename(displaced)
        replacement.rename(workspace)
        return identity

    monkeypatch.setattr(
        failure_workspace_module,
        "_prepare_workspace_directories",
        prepare_then_swap,
    )

    with pytest.raises(FailureWorkspaceFailure) as raised:
        FailureWorkspaceService(FileFailureWorkspace()).record(_request(root))

    assert raised.value.code is FailureWorkspaceErrorCode.WRITE_FAILED
    assert not tuple(displaced.rglob("*.json"))
    assert not tuple((root / ".workspace").rglob("*.json"))


def test_curates_recorded_failures_without_copying_trace_content(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    artifact_ids, _ = _record_failures(root)
    service = FailureCurationService(FileFailureCurationWorkspace())

    observation = service.record(_curation_request(root, artifact_ids))

    artifact_path = root / observation.relative_path
    artifact = FailureCurationArtifact.model_validate_json(artifact_path.read_text())
    assert (
        tuple(member.task_id for member in artifact.groups[0].members),
        artifact.deferred[0].source.artifact_id,
        artifact_path.read_text().find("The agent finalized before reading state."),
        service.record(_curation_request(root, artifact_ids)),
        len(tuple(artifact_path.parent.glob("*.json"))),
        _git(root, "status", "--short"),
    ) == (("task-1", "task-2"), artifact_ids[2], -1, observation, 1, "")


def test_curation_rejects_a_missing_or_tampered_failure_artifact(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    artifact_ids, artifact_paths = _record_failures(root)
    missing_path = artifact_paths[0]
    missing_path.unlink()
    service = FailureCurationService(FileFailureCurationWorkspace())

    with pytest.raises(FailureCurationFailure) as missing:
        service.record(_curation_request(root, artifact_ids))

    assert missing.value.code is FailureCurationErrorCode.SOURCE_NOT_FOUND
    missing_path.write_text("{}", encoding="utf-8")

    with pytest.raises(FailureCurationFailure) as invalid:
        service.record(_curation_request(root, artifact_ids))

    assert invalid.value.code is FailureCurationErrorCode.SOURCE_INVALID
    assert not (root / ".workspace/failure-curations").exists()


def test_curation_does_not_follow_a_failure_artifact_symlink(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    artifact_ids, artifact_paths = _record_failures(root)
    source_path = artifact_paths[0]
    outside = tmp_path / "outside.json"
    source_path.replace(outside)
    source_path.symlink_to(outside)

    with pytest.raises(FailureCurationFailure) as raised:
        FailureCurationService(FileFailureCurationWorkspace()).record(
            _curation_request(root, artifact_ids)
        )

    assert raised.value.code is FailureCurationErrorCode.SOURCE_INVALID
    assert outside.read_bytes()
    assert not (root / ".workspace/failure-curations").exists()


def test_oversized_curation_fails_before_workspace_mutation(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)

    with pytest.raises(FailureCurationFailure) as raised:
        FileFailureCurationWorkspace().store(root, _oversized_curation())

    assert raised.value.code is FailureCurationErrorCode.ARTIFACT_TOO_LARGE
    assert not (root / ".workspace/failure-curations").exists()


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
