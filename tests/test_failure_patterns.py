"""Bounded deterministic failure-pattern mining tests."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_patterns import (
    FailureDiagnosisRecord,
    FailurePatternMiningError,
    FailurePatternMiningErrorCode,
    FailurePatternMiningService,
    FailurePatternMiningStatus,
    FailurePatternOrdering,
    MineFailurePatternsInput,
    failure_pattern_id,
    normalize_root_cause,
)
from ofw.evaluation.failure_workspace import (
    FailedOutcomeInput,
    FailureWorkspaceService,
    FileFailureWorkspace,
    RecordFailureInput,
)

_EVALUATED_AT = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)


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
    (root / "experiment_config.yaml").write_text(
        "benchmark: itsm-bench\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "PROGRAM.md", "experiment_config.yaml")
    _git(root, "commit", "-qm", "prepare")
    return root


def _request(
    root: Path,
    *,
    trace_id: str,
    task_id: str,
    evaluated_at: datetime,
    root_cause: str | None,
    evidence_status: FailureEvidenceStatus = FailureEvidenceStatus.SUPPORTED,
    issue_type: FailureType | None = FailureType.CONTROL_FLOW_FAILURE,
) -> RecordFailureInput:
    supported = evidence_status is FailureEvidenceStatus.SUPPORTED
    critical = f"observation-{task_id}" if supported else None
    return RecordFailureInput(
        workspace_root=root,
        outcome=FailedOutcomeInput(
            trace_id=trace_id,
            task_id=task_id,
            verifier_id="itsm-bench@v1",
            evaluated_at=evaluated_at,
            score=0.0,
            evidence=(f"harbor://{task_id}/verifier/result",),
            outcome_score_id=f"score-{trace_id}",
        ),
        evidence_status=evidence_status,
        issue_type=issue_type if supported else None,
        expected_outcome="Incident is closed.",
        actual_outcome="Incident remains open.",
        critical_observation_id=critical,
        evidence_observation_ids=() if critical is None else (critical,),
        root_cause=root_cause if supported else None,
        counterfactual_action="Read state before finalizing." if supported else None,
        inconclusive_reason=None if supported else "The trace omitted the final tool result.",
    )


def _record(service: FailureWorkspaceService, request: RecordFailureInput) -> str:
    return service.record(request).artifact_id


def test_normalization_and_fingerprint_ignore_volatile_values() -> None:
    first = (
        "Session /tmp/ofw-run-123/trace.json failed for request "
        "request-id-Abc123Xyz987654 at line 7"
    )
    second = (
        "Session /var/tmp/ofw-run-999/trace.json failed for request rq-9876543210AbCdEf at line 42"
    )

    assert normalize_root_cause(first) == normalize_root_cause(second)
    assert "Abc123" not in normalize_root_cause(first)
    assert failure_pattern_id(FailureType.CONTROL_FLOW_FAILURE, first) == failure_pattern_id(
        FailureType.CONTROL_FLOW_FAILURE,
        second,
    )
    assert failure_pattern_id(FailureType.POLICY_FAILURE, first) != failure_pattern_id(
        FailureType.CONTROL_FLOW_FAILURE,
        first,
    )


def test_mines_exact_patterns_and_keeps_inconclusive_diagnoses_separate(
    tmp_path: Path,
) -> None:
    root = _prepared_workspace(tmp_path)
    recorder = FailureWorkspaceService(FileFailureWorkspace())
    first = _record(
        recorder,
        _request(
            root,
            trace_id="trace-1",
            task_id="task-1",
            evaluated_at=_EVALUATED_AT,
            root_cause="The agent stopped after attempt 1 in /tmp/run-1/state.json.",
        ),
    )
    second = _record(
        recorder,
        _request(
            root,
            trace_id="trace-2",
            task_id="task-2",
            evaluated_at=_EVALUATED_AT + timedelta(minutes=1),
            root_cause="The agent stopped after attempt 9 in /var/tmp/run-2/state.json.",
        ),
    )
    third = _record(
        recorder,
        _request(
            root,
            trace_id="trace-3",
            task_id="task-3",
            evaluated_at=_EVALUATED_AT + timedelta(minutes=2),
            root_cause="The agent ignored the required approval.",
            issue_type=FailureType.POLICY_FAILURE,
        ),
    )
    inconclusive = _record(
        recorder,
        _request(
            root,
            trace_id="trace-4",
            task_id="task-4",
            evaluated_at=_EVALUATED_AT + timedelta(minutes=3),
            root_cause=None,
            evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
            issue_type=None,
        ),
    )
    request = MineFailurePatternsInput(
        workspace_root=root,
        artifact_ids=(third, inconclusive, second, first),
    )

    result = FailurePatternMiningService(FileFailureWorkspace()).mine(request)

    assert result.status is FailurePatternMiningStatus.SUCCESS
    assert result.ordering is FailurePatternOrdering.OCCURRENCES_TASKS_LATEST_FINGERPRINT
    assert result.source_artifact_count == 4
    assert result.inconclusive_artifact_ids == (inconclusive,)
    assert len(result.patterns) == 2
    repeated, policy = result.patterns
    assert repeated.occurrence_count == 2
    assert repeated.issue_type is FailureType.CONTROL_FLOW_FAILURE
    assert repeated.task_ids == ("task-1", "task-2")
    assert repeated.trace_ids == ("trace-1", "trace-2")
    assert repeated.artifact_ids == tuple(sorted((first, second)))
    assert repeated.first_evaluated_at == _EVALUATED_AT
    assert repeated.last_evaluated_at == _EVALUATED_AT + timedelta(minutes=1)
    assert policy.occurrence_count == 1
    assert result.artifacts == tuple(pattern.pattern_id for pattern in result.patterns)


def test_reader_loads_only_explicit_artifact_ids(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    recorder = FailureWorkspaceService(FileFailureWorkspace())
    selected = _record(
        recorder,
        _request(
            root,
            trace_id="trace-selected",
            task_id="task-selected",
            evaluated_at=_EVALUATED_AT,
            root_cause="Selected failure.",
        ),
    )
    _record(
        recorder,
        _request(
            root,
            trace_id="trace-unselected",
            task_id="task-unselected",
            evaluated_at=_EVALUATED_AT,
            root_cause="Unselected failure.",
        ),
    )

    result = FailurePatternMiningService(FileFailureWorkspace()).mine(
        MineFailurePatternsInput(workspace_root=root, artifact_ids=(selected,))
    )

    assert result.source_artifact_count == 1
    assert result.patterns[0].artifact_ids == (selected,)


def test_missing_or_tampered_artifact_fails_closed(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    recorder = FailureWorkspaceService(FileFailureWorkspace())
    artifact_id = _record(
        recorder,
        _request(
            root,
            trace_id="trace-1",
            task_id="task-1",
            evaluated_at=_EVALUATED_AT,
            root_cause="The agent finalized early.",
        ),
    )
    service = FailurePatternMiningService(FileFailureWorkspace())

    with pytest.raises(FailurePatternMiningError) as missing:
        service.mine(
            MineFailurePatternsInput(
                workspace_root=root,
                artifact_ids=("11111111-1111-1111-1111-111111111111",),
            )
        )
    assert missing.value.code is FailurePatternMiningErrorCode.ARTIFACT_NOT_FOUND

    artifact_path = root / ".workspace/failures" / f"{artifact_id}.json"
    artifact_path.write_text('{"schema_version":1}', encoding="utf-8")
    with pytest.raises(FailurePatternMiningError) as invalid:
        service.mine(MineFailurePatternsInput(workspace_root=root, artifact_ids=(artifact_id,)))
    assert invalid.value.code is FailurePatternMiningErrorCode.INVALID_ARTIFACT


def test_pattern_request_is_strict_bounded_and_immutable(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    request = MineFailurePatternsInput(
        workspace_root=root,
        artifact_ids=("11111111-1111-1111-1111-111111111111",),
    )

    with pytest.raises(ValidationError):
        MineFailurePatternsInput(
            workspace_root=root,
            artifact_ids=(request.artifact_ids[0], request.artifact_ids[0]),
        )
    with pytest.raises(ValidationError):
        MineFailurePatternsInput(
            workspace_root=root,
            artifact_ids=tuple(f"00000000-0000-0000-0000-{index:012d}" for index in range(51)),
        )
    with pytest.raises(ValidationError):
        MineFailurePatternsInput(
            workspace_root=Path("relative"),
            artifact_ids=request.artifact_ids,
        )
    with pytest.raises(ValidationError):
        MineFailurePatternsInput.model_validate_json(
            request.model_dump_json().removesuffix("}") + ',"unexpected":true}'
        )
    with pytest.raises(ValidationError):
        request.artifact_ids = ()


def test_pattern_output_rejects_a_reader_contract_violation(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    request = MineFailurePatternsInput(
        workspace_root=root,
        artifact_ids=("11111111-1111-1111-1111-111111111111",),
    )

    class EmptyReader:
        def read(
            self,
            workspace_root: Path,
            artifact_ids: tuple[str, ...],
        ) -> tuple[FailureDiagnosisRecord, ...]:
            del workspace_root, artifact_ids
            return ()

    with pytest.raises(FailurePatternMiningError) as raised:
        FailurePatternMiningService(EmptyReader()).mine(request)

    assert raised.value.code is FailurePatternMiningErrorCode.INVALID_READER_RESULT


def test_pattern_summary_is_immutable(tmp_path: Path) -> None:
    root = _prepared_workspace(tmp_path)
    recorder = FailureWorkspaceService(FileFailureWorkspace())
    artifact_id = _record(
        recorder,
        _request(
            root,
            trace_id="trace-1",
            task_id="task-1",
            evaluated_at=_EVALUATED_AT,
            root_cause="The agent finalized early.",
        ),
    )
    result = FailurePatternMiningService(FileFailureWorkspace()).mine(
        MineFailurePatternsInput(workspace_root=root, artifact_ids=(artifact_id,))
    )

    with pytest.raises(ValidationError):
        result.patterns[0].occurrence_count = 2
