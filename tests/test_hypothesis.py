"""Evidence-backed hypothesis contracts, authority, and persistence."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.contracts import ComponentKind
from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_curation import (
    FailureCurationArtifact,
    FailureCurationService,
    FailureGroupInput,
    RecordFailureCurationInput,
)
from ofw.evaluation.failure_patterns import FailurePatternMiningService, failure_pattern_id
from ofw.evaluation.failure_workspace import (
    FailedOutcomeInput,
    FailureWorkspaceService,
    FileFailureCurationWorkspace,
    FileFailureWorkspace,
    RecordFailureInput,
)
from ofw.evolution.hypothesis import (
    FailurePatternReferenceInput,
    HarnessChangeTargetInput,
    HypothesisErrorCode,
    HypothesisFailure,
    HypothesisService,
    HypothesisStatus,
    RecordHypothesisInput,
)
from ofw.evolution.hypothesis_repository import FileHypothesisRepository
from ofw.preparation.contracts import (
    BaselineConfiguration,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import FileExperimentPolicyRepository, build_experiment_policy

_ROOT_CAUSE = "The agent finalizes before checking the updated incident state."


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=check,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "harness"
    root.mkdir()
    (root / "prompt.md").write_text("Check state before finalizing.\n", encoding="utf-8")
    (root / "tools.py").write_text("def check_state():\n    return True\n", encoding="utf-8")
    (root / "PROGRAM.md").write_text("# Program\n", encoding="utf-8")
    (root / "experiment_config.yaml").write_text("benchmark: itsm-bench\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "OpenFlywheel Test")
    _git(root, "config", "user.email", "ofw@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "prepared experiment")
    _git(root, "branch", "-m", "ofw/experiment-one")
    commit = _git(root, "rev-parse", "HEAD")
    _publish_policy(tmp_path, root, commit)
    return root, commit


def _publish_policy(tmp_path: Path, root: Path, commit: str) -> None:
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir(exist_ok=True)
    executable = tmp_path / "harbor"
    executable.touch(mode=0o700, exist_ok=True)
    request = PrepareWorkspaceInput(
        experiment_id="experiment-one",
        harness_root=root,
        base_ref="HEAD",
        worktree_parent=tmp_path,
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=Path("config.json"),
        expected_task_count=2,
        editable_paths=(Path("prompt.md"),),
        goal="Improve verifier-backed quality.",
        quality_target=1.0,
        max_iterations=3,
        no_improvement_limit=2,
        max_baseline_seconds=600,
    )
    prepared = PreparedGitWorkspace(
        branch_name="ofw/experiment-one",
        worktree_path=root,
        base_commit=commit,
        initialization_commit=commit,
        program_path=root / "PROGRAM.md",
    )
    baseline = BaselineConfiguration(
        model="openai/gpt-5.4-mini",
        task_ids=("task-1", "task-2"),
        benchmark_config_digest="sha256:" + "1" * 64,
        verifier="itsm-bench",
        environment="itsm-bench",
    )
    policy = build_experiment_policy(request, prepared, baseline)
    control = root / ".git/ofw/preparations/experiment-one"
    control.mkdir(parents=True)
    FileExperimentPolicyRepository().publish(control, policy)


def _diagnosis(
    root: Path,
    suffix: str,
    *,
    root_cause: str = _ROOT_CAUSE,
    supported: bool = True,
) -> str:
    evidence_status = (
        FailureEvidenceStatus.SUPPORTED if supported else FailureEvidenceStatus.INCONCLUSIVE
    )
    observation = FailureWorkspaceService(FileFailureWorkspace()).record(
        RecordFailureInput(
            workspace_root=root,
            outcome=FailedOutcomeInput(
                trace_id=f"trace-{suffix}",
                task_id=f"task-{suffix}",
                verifier_id="itsm-bench",
                evaluated_at=datetime(2026, 9, 1, 12, int(suffix), tzinfo=UTC),
                score=0.0,
                evidence=(f"score://{suffix}",),
                outcome_score_id=f"score-{suffix}",
            ),
            evidence_status=evidence_status,
            issue_type=FailureType.CONTROL_FLOW_FAILURE if supported else None,
            expected_outcome="The incident is closed.",
            actual_outcome="The incident remains open.",
            critical_observation_id=f"observation-{suffix}" if supported else None,
            evidence_observation_ids=(f"observation-{suffix}",) if supported else (),
            root_cause=root_cause if supported else None,
            counterfactual_action="Read state before finalizing." if supported else None,
            inconclusive_reason=None if supported else "The decisive span is unavailable.",
        )
    )
    return observation.artifact_id


def _service() -> HypothesisService:
    workspace = FileFailureWorkspace()
    return HypothesisService(
        pattern_miner=FailurePatternMiningService(workspace),
        repository=FileHypothesisRepository(),
    )


def _curation(root: Path, artifacts: tuple[str, ...]) -> tuple[str, str]:
    observation = FailureCurationService(FileFailureCurationWorkspace()).record(
        RecordFailureCurationInput(
            workspace_root=root,
            source_artifact_ids=artifacts,
            groups=(
                FailureGroupInput(
                    pattern_key="premature-completion",
                    title="Premature completion",
                    mechanism="The agent finalizes before verification.",
                    prevention="Require verification before completion.",
                    target_component=ComponentKind.PROMPT,
                    failure_artifact_ids=artifacts,
                ),
            ),
            deferred=(),
        )
    )
    artifact = FailureCurationArtifact.model_validate_json(
        (root / observation.relative_path).read_text(encoding="utf-8")
    )
    return observation.curation_id, artifact.groups[0].group_id


def _request(
    root: Path,
    commit: str,
    artifacts: tuple[str, ...],
    *,
    curation_artifacts: tuple[str, ...] | None = None,
) -> RecordHypothesisInput:
    selected_curation_artifacts = curation_artifacts or artifacts
    if len(selected_curation_artifacts) >= 2:
        curation_id, curation_group_id = _curation(root, selected_curation_artifacts)
    else:
        curation_id = "00000000-0000-0000-0000-000000000010"
        curation_group_id = "00000000-0000-0000-0000-000000000011"
    return RecordHypothesisInput(
        workspace_root=root,
        experiment_id="experiment-one",
        source_commit=commit,
        curation_id=curation_id,
        curation_group_id=curation_group_id,
        predicted_task_ids=("task-1", "task-2"),
        at_risk_task_ids=("task-3",),
        patterns=(
            FailurePatternReferenceInput(
                pattern_id=failure_pattern_id(
                    FailureType.CONTROL_FLOW_FAILURE,
                    _ROOT_CAUSE,
                ),
                diagnosis_artifact_ids=artifacts,
            ),
        ),
        statement="Require a state check before the agent finalizes.",
        rationale="Both supported failures finalize immediately after mutation.",
        target=HarnessChangeTargetInput(
            component_kind=ComponentKind.PROMPT,
            relative_paths=(Path("prompt.md"),),
        ),
        expected_effect="The agent verifies task completion before returning success.",
        regression_risks=("The extra check may increase latency.",),
    )


def _with_patterns(
    request: RecordHypothesisInput,
    patterns: tuple[FailurePatternReferenceInput, ...],
) -> RecordHypothesisInput:
    return RecordHypothesisInput(
        workspace_root=request.workspace_root,
        experiment_id=request.experiment_id,
        source_commit=request.source_commit,
        curation_id=request.curation_id,
        curation_group_id=request.curation_group_id,
        predicted_task_ids=request.predicted_task_ids,
        at_risk_task_ids=request.at_risk_task_ids,
        patterns=patterns,
        statement=request.statement,
        rationale=request.rationale,
        target=request.target,
        expected_effect=request.expected_effect,
        regression_risks=request.regression_risks,
    )


def _with_target(
    request: RecordHypothesisInput,
    target: HarnessChangeTargetInput,
) -> RecordHypothesisInput:
    return RecordHypothesisInput(
        workspace_root=request.workspace_root,
        experiment_id=request.experiment_id,
        source_commit=request.source_commit,
        curation_id=request.curation_id,
        curation_group_id=request.curation_group_id,
        predicted_task_ids=request.predicted_task_ids,
        at_risk_task_ids=request.at_risk_task_ids,
        patterns=request.patterns,
        statement=request.statement,
        rationale=request.rationale,
        target=target,
        expected_effect=request.expected_effect,
        regression_risks=request.regression_risks,
    )


def _with_curation_group(
    request: RecordHypothesisInput,
    curation_group_id: str,
) -> RecordHypothesisInput:
    return RecordHypothesisInput(
        workspace_root=request.workspace_root,
        experiment_id=request.experiment_id,
        source_commit=request.source_commit,
        curation_id=request.curation_id,
        curation_group_id=curation_group_id,
        predicted_task_ids=request.predicted_task_ids,
        at_risk_task_ids=request.at_risk_task_ids,
        patterns=request.patterns,
        statement=request.statement,
        rationale=request.rationale,
        target=request.target,
        expected_effect=request.expected_effect,
        regression_risks=request.regression_risks,
    )


def _evidence_case(
    case: str,
    first: str,
    second: str,
    other: str,
    inconclusive: str,
) -> tuple[str, ...]:
    if case == "missing":
        return first, "00000000-0000-0000-0000-000000000000"
    if case == "extra":
        return first, second, other
    if case == "misassigned":
        return first, other
    return first, inconclusive


def test_record_hypothesis_recomputes_evidence_and_is_deterministic(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)

    first = _service().record(request)
    repeated = _service().record(request)

    assert first == repeated
    assert first.status is HypothesisStatus.SUCCESS
    assert first.source_commit == commit
    assert first.curation_id == request.curation_id
    assert first.curation_group_id == request.curation_group_id
    assert first.pattern_count == 1
    assert first.diagnosis_count == 2
    assert first.target_paths == (Path("prompt.md"),)
    assert first.relative_path == Path(f".workspace/hypotheses/{first.hypothesis_id}.json")
    assert "stop" in first.next_actions[0].lower()
    assert _git(root, "status", "--short") == ""


def test_hypothesis_identity_canonicalizes_declared_order(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)
    reversed_request = _with_patterns(
        request,
        (
            FailurePatternReferenceInput(
                pattern_id=request.patterns[0].pattern_id,
                diagnosis_artifact_ids=tuple(reversed(artifacts)),
            ),
        ),
    )

    assert _service().record(reversed_request) == _service().record(request)


@pytest.mark.parametrize("case", ("missing", "extra", "misassigned", "inconclusive"))
def test_hypothesis_fails_closed_on_inexact_evidence(tmp_path: Path, case: str) -> None:
    root, commit = _workspace(tmp_path)
    first = _diagnosis(root, "1")
    second = _diagnosis(root, "2")
    other = _diagnosis(root, "3", root_cause="The tool result is ignored after mutation.")
    inconclusive = _diagnosis(root, "4", supported=False)
    artifacts = _evidence_case(case, first, second, other, inconclusive)
    curation_artifacts = (first, other) if case == "misassigned" else (first, second)
    request = _request(root, commit, artifacts, curation_artifacts=curation_artifacts)
    if case == "misassigned":
        request = _with_patterns(
            request,
            (
                FailurePatternReferenceInput(
                    pattern_id=failure_pattern_id(
                        FailureType.CONTROL_FLOW_FAILURE,
                        _ROOT_CAUSE,
                    ),
                    diagnosis_artifact_ids=(other,),
                ),
                FailurePatternReferenceInput(
                    pattern_id=failure_pattern_id(
                        FailureType.CONTROL_FLOW_FAILURE,
                        "The tool result is ignored after mutation.",
                    ),
                    diagnosis_artifact_ids=(first,),
                ),
            ),
        )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    expected = (
        HypothesisErrorCode.PATTERN_EVIDENCE_MISMATCH
        if case == "misassigned"
        else HypothesisErrorCode.CURATION_EVIDENCE_MISMATCH
    )
    assert raised.value.code is expected


@pytest.mark.parametrize(
    "drift",
    ("commit", "dirty", "untracked", "branch", "target-symlink"),
)
def test_hypothesis_rejects_stale_or_drifted_workspace(tmp_path: Path, drift: str) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)
    if drift == "commit":
        (root / "tools.py").write_text("# changed\n", encoding="utf-8")
        _git(root, "add", "tools.py")
        _git(root, "commit", "-qm", "move head")
    elif drift == "dirty":
        (root / "prompt.md").write_text("dirty\n", encoding="utf-8")
    elif drift == "untracked":
        (root / "untracked.txt").write_text("not prepared\n", encoding="utf-8")
    elif drift == "branch":
        _git(root, "branch", "-m", "wrong-branch")
    else:
        (root / "prompt.md").unlink()
        (root / "prompt.md").symlink_to(root / "tools.py")

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code in {
        HypothesisErrorCode.STALE_COMMIT,
        HypothesisErrorCode.DIRTY_WORKSPACE,
        HypothesisErrorCode.STALE_POLICY,
        HypothesisErrorCode.INVALID_TARGET,
    }


def test_hypothesis_rejects_semantically_tampered_diagnosis(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)
    path = root / ".workspace/failures" / f"{artifacts[0]}.json"
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace(
            "The incident remains open.",
            "The incident was altered after recording.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.PATTERN_EVIDENCE_MISMATCH


@pytest.mark.parametrize("path", (Path("tools.py"), Path("PROGRAM.md"), Path("prompt.md/child")))
def test_hypothesis_uses_exact_editable_allowlist_and_freezes_everything_else(
    tmp_path: Path,
    path: Path,
) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)
    request = _with_target(
        request,
        HarnessChangeTargetInput(
            component_kind=ComponentKind.TOOL,
            relative_paths=(path,),
        ),
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.TARGET_NOT_EDITABLE


def test_hypothesis_input_rejects_duplicates_escapes_empty_and_extra_fields(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifact = _diagnosis(root, "1")
    payload = _request(root, commit, (artifact,)).model_dump_json()
    extra = payload[:-1] + ',"unexpected":true}'
    with pytest.raises(ValidationError):
        RecordHypothesisInput.model_validate_json(extra)

    escaped = payload.replace('"relative_paths":["prompt.md"]', '"relative_paths":["../prompt.md"]')
    with pytest.raises(ValidationError):
        RecordHypothesisInput.model_validate_json(escaped)

    empty = payload.replace('"relative_paths":["prompt.md"]', '"relative_paths":[]')
    with pytest.raises(ValidationError):
        RecordHypothesisInput.model_validate_json(empty)

    duplicate = payload.replace(
        f'"diagnosis_artifact_ids":["{artifact}"]',
        f'"diagnosis_artifact_ids":["{artifact}","{artifact}"]',
    )
    with pytest.raises(ValidationError):
        RecordHypothesisInput.model_validate_json(duplicate)

    request = _request(root, commit, (artifact,))
    with pytest.raises(ValidationError):
        _with_patterns(request, (request.patterns[0], request.patterns[0]))
    with pytest.raises(ValidationError):
        HarnessChangeTargetInput(
            component_kind=ComponentKind.PROMPT,
            relative_paths=(Path("prompt.md"), Path("prompt.md")),
        )
    with pytest.raises(ValidationError):
        RecordHypothesisInput(
            workspace_root=request.workspace_root,
            experiment_id=request.experiment_id,
            source_commit=request.source_commit,
            curation_id=request.curation_id,
            curation_group_id=request.curation_group_id,
            predicted_task_ids=request.predicted_task_ids,
            at_risk_task_ids=request.at_risk_task_ids,
            patterns=request.patterns,
            statement=request.statement,
            rationale=request.rationale,
            target=request.target,
            expected_effect=request.expected_effect,
            regression_risks=("same risk", "same risk"),
        )


@pytest.mark.parametrize(
    ("field", "malformed"),
    (
        ("experiment_id", "Xexperiment-one!"),
        ("source_commit", "x" + "1" * 40 + "y"),
        ("curation_id", "x00000000-0000-0000-0000-000000000001y"),
        ("curation_group_id", "x00000000-0000-0000-0000-000000000002y"),
    ),
)
def test_hypothesis_input_rejects_prefixed_and_suffixed_identifiers(
    tmp_path: Path,
    field: str,
    malformed: str,
) -> None:
    root, commit = _workspace(tmp_path)
    artifact = _diagnosis(root, "1")
    request = _request(root, commit, (artifact,))
    originals: dict[str, str] = {
        "experiment_id": request.experiment_id,
        "source_commit": request.source_commit,
        "curation_id": request.curation_id,
        "curation_group_id": request.curation_group_id,
    }
    payload = request.model_dump_json().replace(
        f'"{field}":"{originals[field]}"',
        f'"{field}":"{malformed}"',
    )

    with pytest.raises(ValidationError, match=field):
        RecordHypothesisInput.model_validate_json(payload)


@pytest.mark.parametrize(
    ("field", "malformed"),
    (
        ("pattern_id", "xsha256:" + "1" * 64 + "y"),
        ("diagnosis_artifact_ids", ("x00000000-0000-0000-0000-000000000001y",)),
    ),
)
def test_pattern_reference_rejects_prefixed_and_suffixed_identifiers(
    field: str,
    malformed: str | tuple[str, ...],
) -> None:
    payload: dict[str, str | tuple[str, ...]] = {
        "pattern_id": "sha256:" + "1" * 64,
        "diagnosis_artifact_ids": ("00000000-0000-0000-0000-000000000001",),
    }
    payload[field] = malformed

    with pytest.raises(ValidationError):
        FailurePatternReferenceInput.model_validate(payload)


def test_hypothesis_input_accepts_its_exact_global_maximum() -> None:
    artifacts = tuple(f"00000000-0000-0000-0000-{index:012x}" for index in range(50))
    request = RecordHypothesisInput(
        workspace_root=Path("/prepared"),
        experiment_id="maximum",
        source_commit="1" * 40,
        curation_id="00000000-0000-0000-0000-000000000001",
        curation_group_id="00000000-0000-0000-0000-000000000002",
        predicted_task_ids=("task-1",),
        at_risk_task_ids=("task-2",),
        patterns=(
            FailurePatternReferenceInput(
                pattern_id="sha256:" + "1" * 64,
                diagnosis_artifact_ids=artifacts,
            ),
        ),
        statement="s" * 4000,
        rationale="r" * 4000,
        target=HarnessChangeTargetInput(
            component_kind=ComponentKind.SKILL,
            relative_paths=tuple(Path(f"skills/{index}.md") for index in range(50)),
        ),
        expected_effect="e" * 4000,
        regression_risks=tuple(f"risk-{index}" for index in range(10)),
    )

    assert len(request.patterns[0].diagnosis_artifact_ids) == 50
    assert len(request.target.relative_paths) == 50
    assert len(request.regression_risks) == 10


def test_hypothesis_predictions_must_be_disjoint() -> None:
    with pytest.raises(ValidationError):
        RecordHypothesisInput(
            workspace_root=Path("/prepared"),
            experiment_id="experiment-one",
            source_commit="1" * 40,
            curation_id="00000000-0000-0000-0000-000000000001",
            curation_group_id="00000000-0000-0000-0000-000000000002",
            predicted_task_ids=("task-1",),
            at_risk_task_ids=("task-1",),
            patterns=(
                FailurePatternReferenceInput(
                    pattern_id="sha256:" + "1" * 64,
                    diagnosis_artifact_ids=("00000000-0000-0000-0000-000000000001",),
                ),
            ),
            statement="s",
            rationale="r",
            target=HarnessChangeTargetInput(
                component_kind=ComponentKind.PROMPT,
                relative_paths=(Path("prompt.md"),),
            ),
            expected_effect="e",
            regression_risks=(),
        )


def test_hypothesis_rejects_an_incomplete_curation_group(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    first = _diagnosis(root, "1")
    second = _diagnosis(root, "2")
    request = _request(root, commit, (first, second))
    incomplete = _with_patterns(
        request,
        (
            FailurePatternReferenceInput(
                pattern_id=request.patterns[0].pattern_id,
                diagnosis_artifact_ids=(first,),
            ),
        ),
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(incomplete)

    assert raised.value.code is HypothesisErrorCode.CURATION_EVIDENCE_MISMATCH


def test_hypothesis_rejects_a_missing_curation_group(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _with_curation_group(
        _request(root, commit, artifacts),
        "00000000-0000-0000-0000-000000000099",
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.CURATION_GROUP_NOT_FOUND


def test_hypothesis_rejects_a_target_outside_the_curated_component(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _with_target(
        _request(root, commit, artifacts),
        HarnessChangeTargetInput(
            component_kind=ComponentKind.TOOL,
            relative_paths=(Path("prompt.md"),),
        ),
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.CURATION_EVIDENCE_MISMATCH


def test_hypothesis_rejects_a_tampered_curation_receipt(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))
    request = _request(root, commit, artifacts)
    path = root / ".workspace/failure-curations" / f"{request.curation_id}.json"
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace(
            "The agent finalizes before verification.",
            "The curation was changed after recording.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.CURATION_INVALID


def test_hypothesis_missing_policy_is_typed(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    (root / ".git/ofw/preparations/experiment-one/policy.json").unlink()
    artifacts = (_diagnosis(root, "1"), _diagnosis(root, "2"))

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(_request(root, commit, artifacts))

    assert raised.value.code is HypothesisErrorCode.POLICY_SNAPSHOT_REQUIRED


def test_hypothesis_concurrent_identical_writes_are_idempotent(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    request = _request(root, commit, (_diagnosis(root, "1"), _diagnosis(root, "2")))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: _service().record(request), range(2)))

    assert results[0] == results[1]


def test_hypothesis_conflicting_existing_artifact_is_rejected(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    request = _request(root, commit, (_diagnosis(root, "1"), _diagnosis(root, "2")))
    recorded = _service().record(request)
    path = root / recorded.relative_path
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.HYPOTHESIS_CONFLICT


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_hypothesis_rejects_non_regular_artifact_target(tmp_path: Path, kind: str) -> None:
    root, commit = _workspace(tmp_path)
    request = _request(root, commit, (_diagnosis(root, "1"), _diagnosis(root, "2")))
    recorded = _service().record(request)
    path = root / recorded.relative_path
    path.unlink()
    if kind == "symlink":
        path.symlink_to(root / "prompt.md")
    else:
        os.mkfifo(path)

    with pytest.raises(HypothesisFailure) as raised:
        _service().record(request)

    assert raised.value.code is HypothesisErrorCode.WRITE_FAILED


@pytest.mark.parametrize(
    "invalid",
    ("missing-root", "missing-marker", "missing-target", "directory"),
)
def test_hypothesis_repository_rejects_invalid_workspace_objects(
    tmp_path: Path,
    invalid: str,
) -> None:
    root, commit = _workspace(tmp_path)
    repository = FileHypothesisRepository()
    policy = repository.load_policy(root, "experiment-one")
    target = Path("prompt.md")
    if invalid == "missing-root":
        root = tmp_path / "missing"
    elif invalid == "missing-marker":
        (root / "PROGRAM.md").unlink()
    elif invalid == "missing-target":
        target = Path("missing.md")
    else:
        target = Path("directory")
        (root / target).mkdir()

    with pytest.raises(HypothesisFailure) as raised:
        repository.validate_workspace(root, policy, commit, (target,))

    assert raised.value.code in {
        HypothesisErrorCode.STALE_POLICY,
        HypothesisErrorCode.INVALID_TARGET,
    }


def test_hypothesis_repository_sanitizes_git_failure(tmp_path: Path) -> None:
    root, commit = _workspace(tmp_path)
    repository = FileHypothesisRepository()
    policy = repository.load_policy(root, "experiment-one")
    (root / ".git").rename(root / ".git-moved")

    with pytest.raises(HypothesisFailure) as raised:
        repository.validate_workspace(root, policy, commit, (Path("prompt.md"),))

    assert raised.value.code is HypothesisErrorCode.STALE_POLICY
    assert "fatal" not in str(raised.value).lower()
