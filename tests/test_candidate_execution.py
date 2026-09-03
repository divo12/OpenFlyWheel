"""Candidate identity, Git isolation, execution, and authoritative receipts."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import ofw.evolution.candidate_service as candidate_service_module
from ofw.contracts import ComponentKind, Sha256Digest
from ofw.evaluation.langfuse import OutcomeScoreSubmission
from ofw.evaluation.outcome import OutcomeEvaluation
from ofw.evolution.candidate import (
    CandidateBlockerCode,
    CandidateErrorCode,
    CandidateExecutionInput,
    CandidateFailure,
    CandidateId,
    CandidatePhase,
    CandidateStatus,
    TraceMatch,
    TraceMatchRequest,
    candidate_policy_digest,
)
from ofw.evolution.candidate_git import CandidateGitGateway
from ofw.evolution.candidate_langfuse import LangfuseCandidateTraceLocator
from ofw.evolution.candidate_service import CandidateExecutionService
from ofw.evolution.hypothesis import (
    FailurePatternReference,
    HarnessChangeTarget,
    HarnessHypothesis,
    HypothesisArtifact,
    HypothesisId,
)
from ofw.evolution.hypothesis_repository import FileHypothesisRepository
from ofw.observability.langfuse.domain import (
    JsonDocument,
    ObservationId,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    PageCursor,
    ProjectId,
    ScoreId,
    TraceId,
)
from ofw.observability.langfuse.trace_query import ObservationRead
from ofw.preparation.contracts import (
    BaselineConfiguration,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    ExperimentTrial,
    PreparationErrorCode,
    PreparationFailure,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import (
    ExperimentPolicySnapshot,
    FileExperimentPolicyRepository,
    build_experiment_policy,
)


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=check,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _authority(tmp_path: Path) -> tuple[Path, ExperimentPolicySnapshot, HarnessHypothesis]:
    root = tmp_path / "accepted"
    root.mkdir()
    (root / "prompt.md").write_text("Original prompt.\n", encoding="utf-8")
    (root / "tools.py").write_text("def tool() -> bool:\n    return True\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "PROGRAM.md").write_text("# Managed\n", encoding="utf-8")
    (root / "experiment_config.yaml").write_text("managed: true\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "OpenFlywheel Test")
    _git(root, "config", "user.email", "ofw@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "accepted experiment")
    _git(root, "branch", "-m", "ofw/experiment-one")
    commit = _git(root, "rev-parse", "HEAD")
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    executable = tmp_path / "harbor"
    executable.touch(mode=0o700)
    request = PrepareWorkspaceInput(
        experiment_id="experiment-one",
        harness_root=root,
        base_ref="HEAD",
        worktree_parent=tmp_path,
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=Path("config.json"),
        expected_task_count=2,
        editable_paths=(Path("prompt.md"), Path("tools.py")),
        goal="Improve verifier-backed quality.",
        quality_target=1.0,
        max_iterations=3,
        no_improvement_limit=2,
        max_baseline_seconds=600,
    )
    policy = build_experiment_policy(
        request,
        PreparedGitWorkspace(
            branch_name="ofw/experiment-one",
            worktree_path=root,
            base_commit=commit,
            initialization_commit=commit,
            program_path=root / "PROGRAM.md",
        ),
        BaselineConfiguration(
            model="openai/gpt-5.4-mini",
            task_ids=("task-1", "task-2"),
            benchmark_config_digest="sha256:" + "a" * 64,
            verifier="itsm-bench",
            environment="itsm-bench",
        ),
    )
    hypothesis = HarnessHypothesis(
        id=HypothesisId("sha256:" + "b" * 64),
        experiment_id=policy.experiment_id,
        source_commit=commit,
        patterns=(
            FailurePatternReference(
                "sha256:" + "c" * 64,
                ("00000000-0000-0000-0000-000000000000",),
            ),
        ),
        statement="Require a state check before finalizing.",
        rationale="Supported failures share the same cause.",
        target=HarnessChangeTarget(ComponentKind.PROMPT, (Path("prompt.md"),)),
        expected_effect="The agent verifies completion.",
        regression_risks=(),
    )
    hypothesis = replace(
        hypothesis,
        id=HypothesisArtifact.from_hypothesis(hypothesis).recomputed_id(),
    )
    control = root / ".git/ofw/preparations/experiment-one"
    control.mkdir(parents=True)
    FileExperimentPolicyRepository().publish(control, policy)
    FileHypothesisRepository().store(root, hypothesis)
    return root, policy, hypothesis


class _FakeRunner:
    def __init__(self, controls: ExperimentControls) -> None:
        self.controls = controls
        self.runs: list[ExperimentRun] = []
        self.summary: ExperimentSummary | None = None
        self.failure: PreparationFailure | None = None
        self.start_failure: PreparationFailure | None = None
        self.start_count = 0

    def validate(
        self,
        benchmark_root: Path,
        harbor_executable: Path,
        harbor_config: Path,
    ) -> ExperimentControls:
        if self.failure is not None:
            raise self.failure
        return self.controls

    def start(self, run: ExperimentRun) -> int:
        self.start_count += 1
        if self.start_failure is not None:
            raise self.start_failure
        self.runs.append(run)
        return 123

    def summarize(self, run: ExperimentRun) -> ExperimentSummary | None:
        return self.summary


class _FakeTraceLocator:
    def __init__(self) -> None:
        self.requests: list[TraceMatchRequest] = []
        self.blocker: CandidateBlockerCode | None = None

    def locate(self, request: TraceMatchRequest) -> TraceMatch:
        self.requests.append(request)
        if self.blocker is not None:
            return TraceMatch(trace_id=None, blocker=self.blocker)
        return TraceMatch(trace_id=f"trace-{request.task_id}", blocker=None)


class _FakeOutcomeStore:
    def __init__(self) -> None:
        self.outcomes: list[OutcomeEvaluation] = []
        self.failure: Exception | None = None

    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        if self.failure is not None:
            raise self.failure
        self.outcomes.append(outcome)
        return OutcomeScoreSubmission(
            ScoreId(f"score-{outcome.task_id.value}"),
            outcome.trace_id,
        )


class _ObservationReader:
    def __init__(self, page: ObservationPage) -> None:
        self.page = page
        self.queries: list[ObservationRead] = []

    def read_observations(self, query: ObservationRead) -> ObservationPage:
        self.queries.append(query)
        return self.page


def _root_observation(trace_id: str, suffix: str) -> ObservationRecord:
    raw = JsonDocument("{}")
    return ObservationRecord(
        id=ObservationId(f"observation-{suffix}"),
        trace_id=TraceId(trace_id),
        start_time=datetime(2026, 9, 2, 10, 1, tzinfo=UTC),
        end_time=None,
        project_id=ProjectId("project-1"),
        parent_observation_id=None,
        type=ObservationType.AGENT,
        is_root=True,
        name="agent",
        level=None,
        version=None,
        environment="itsm-bench",
        user_id=None,
        session_id="candidate-session",
        created_at=None,
        updated_at=None,
        metadata=None,
        usage=None,
        costs=None,
        total_cost=None,
        tags=(),
        release="d" * 40,
        trace_name=None,
        raw=raw,
        digest=Sha256Digest("sha256:" + "0" * 64),
    )


def _controls(policy: ExperimentPolicySnapshot) -> ExperimentControls:
    return ExperimentControls(
        model=policy.model,
        task_ids=policy.task_ids,
        benchmark_config_digest=policy.benchmark_config_digest,
        verifier=policy.verifier,
        environment=policy.environment,
        concurrency=policy.concurrency,
        max_retries=policy.max_retries,
    )


def _candidate_request(
    tmp_path: Path,
    root: Path,
    hypothesis: HarnessHypothesis,
) -> CandidateExecutionInput:
    benchmark = tmp_path / "benchmark"
    executable = tmp_path / "harbor"
    config = benchmark / "config.json"
    config.write_text("{}")
    return CandidateExecutionInput(
        workspace_root=root,
        worktree_parent=tmp_path / "candidates",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config.relative_to(benchmark),
        experiment_id=hypothesis.experiment_id,
        hypothesis_id=hypothesis.id.value,
    )


def test_candidate_id_binds_policy_hypothesis_source_tree_and_controls() -> None:
    candidate = CandidateId.build(
        policy_digest="sha256:" + "1" * 64,
        hypothesis_id="sha256:" + "2" * 64,
        source_commit="3" * 40,
        candidate_tree="4" * 40,
        controls_digest="sha256:" + "5" * 64,
    )

    expected = hashlib.sha256(
        (
            '{"schema_version":1,"policy_digest":"sha256:'
            + "1" * 64
            + '","hypothesis_id":"sha256:'
            + "2" * 64
            + '","source_commit":"'
            + "3" * 40
            + '","candidate_tree":"'
            + "4" * 40
            + '","controls_digest":"sha256:'
            + "5" * 64
            + '"}'
        ).encode("utf-8")
    ).hexdigest()

    assert candidate.value == f"sha256:{expected}"
    assert str(candidate) == candidate.value


def test_candidate_id_changes_when_any_authority_input_changes() -> None:
    values = {
        "policy_digest": "sha256:" + "1" * 64,
        "hypothesis_id": "sha256:" + "2" * 64,
        "source_commit": "3" * 40,
        "candidate_tree": "4" * 40,
        "controls_digest": "sha256:" + "5" * 64,
    }
    baseline = CandidateId.build(**values)

    for field, replacement in (
        ("policy_digest", "sha256:" + "a" * 64),
        ("hypothesis_id", "sha256:" + "b" * 64),
        ("source_commit", "c" * 40),
        ("candidate_tree", "d" * 40),
        ("controls_digest", "sha256:" + "e" * 64),
    ):
        changed = dict(values)
        changed[field] = replacement
        assert CandidateId.build(**changed) != baseline


@pytest.mark.parametrize("trace_id", ("", "invalid trace", "x" * 257))
def test_trace_match_rejects_invalid_trace_identifiers(trace_id: str) -> None:
    with pytest.raises(CandidateFailure) as raised:
        TraceMatch(trace_id=trace_id, blocker=None)

    assert raised.value.code is CandidateErrorCode.INVALID_RESULT


def test_candidate_input_rejects_extra_relative_and_escaped_paths(tmp_path: Path) -> None:
    root, _, hypothesis = _authority(tmp_path)
    request = _candidate_request(tmp_path, root, hypothesis)
    payload = request.model_dump_json()

    with pytest.raises(ValidationError):
        CandidateExecutionInput.model_validate_json(payload[:-1] + ',"unexpected":true}')
    with pytest.raises(ValidationError):
        CandidateExecutionInput.model_validate_json(
            payload.replace(str(request.workspace_root), "relative-workspace")
        )
    with pytest.raises(ValidationError):
        CandidateExecutionInput.model_validate_json(
            payload.replace('"harbor_config":"config.json"', '"harbor_config":"../config.json"')
        )


def test_candidate_git_gateway_isolates_validates_and_commits_one_tree(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    worktree_parent = tmp_path / "candidates"
    worktree_parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, worktree_parent, policy, hypothesis)

    (workspace.worktree_path / "prompt.md").write_text("Check state before finalizing.\n")
    tree = gateway.inspect(workspace, policy, hypothesis)
    candidate_id = CandidateId.build(
        policy_digest=candidate_policy_digest(policy),
        hypothesis_id=hypothesis.id.value,
        source_commit=workspace.source_commit,
        candidate_tree=tree.tree_id,
        controls_digest=policy.controls_digest,
    )
    committed = gateway.commit(workspace, tree, candidate_id, policy.experiment_id)

    assert (root / "prompt.md").read_text() == "Original prompt.\n"
    assert _git(workspace.worktree_path, "rev-parse", "HEAD^") == policy.initialization_commit
    assert _git(workspace.worktree_path, "rev-parse", "HEAD^{tree}") == tree.tree_id
    message = _git(workspace.worktree_path, "show", "-s", "--format=%B", committed.commit)
    assert message.count("OFW-Experiment: experiment-one") == 1
    assert message.count(f"OFW-Run: {candidate_id.value}") == 1


def test_candidate_git_gateway_rejects_an_empty_candidate(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, parent, policy, hypothesis)

    with pytest.raises(CandidateFailure) as raised:
        gateway.inspect(workspace, policy, hypothesis)

    assert raised.value.code is CandidateErrorCode.EMPTY_CANDIDATE
    assert _git(workspace.worktree_path, "rev-parse", "HEAD") == policy.initialization_commit


def test_candidate_git_gateway_refuses_an_existing_candidate_worktree(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    gateway.prepare(root, parent, policy, hypothesis)

    with pytest.raises(CandidateFailure) as raised:
        gateway.prepare(root, parent, policy, hypothesis)

    assert raised.value.code is CandidateErrorCode.WORKTREE_EXISTS


def test_candidate_git_gateway_rechecks_the_tree_before_commit(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, parent, policy, hypothesis)
    target = workspace.worktree_path / "prompt.md"
    target.write_text("first candidate\n")
    tree = gateway.inspect(workspace, policy, hypothesis)
    target.write_text("changed after sealing\n")
    candidate_id = CandidateId.build(
        policy_digest=candidate_policy_digest(policy),
        hypothesis_id=hypothesis.id.value,
        source_commit=workspace.source_commit,
        candidate_tree=tree.tree_id,
        controls_digest=policy.controls_digest,
    )

    with pytest.raises(CandidateFailure) as raised:
        gateway.commit(workspace, tree, candidate_id, policy.experiment_id)

    assert raised.value.code is CandidateErrorCode.STALE_COMMIT
    assert _git(workspace.worktree_path, "rev-parse", "HEAD") == policy.initialization_commit


def test_candidate_git_gateway_rejects_hard_linked_targets(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, parent, policy, hypothesis)
    candidate_target = workspace.worktree_path / "prompt.md"
    candidate_target.unlink()
    os.link(root / "prompt.md", candidate_target)
    candidate_target.write_text("shared mutation\n")

    with pytest.raises(CandidateFailure) as raised:
        gateway.inspect(workspace, policy, hypothesis)

    assert raised.value.code is CandidateErrorCode.UNSAFE_PATH


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("out-of-scope", CandidateErrorCode.OUT_OF_SCOPE),
        ("rename", CandidateErrorCode.UNSAFE_PATH),
        ("managed", CandidateErrorCode.MANAGED_PATH),
        ("workspace", CandidateErrorCode.MANAGED_PATH),
        ("credential", CandidateErrorCode.CREDENTIAL_PATH),
        ("symlink", CandidateErrorCode.UNSAFE_PATH),
    ),
)
def test_candidate_git_gateway_freezes_everything_except_exact_hypothesis_targets(
    tmp_path: Path,
    mutation: str,
    expected: CandidateErrorCode,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, parent, policy, hypothesis)
    candidate = workspace.worktree_path
    if mutation == "out-of-scope":
        (candidate / "tools.py").write_text("unsafe = True\n")
    elif mutation == "rename":
        _git(candidate, "mv", "prompt.md", "renamed.md")
    elif mutation == "managed":
        (candidate / "PROGRAM.md").write_text("changed\n")
    elif mutation == "workspace":
        (candidate / ".workspace").mkdir()
        (candidate / ".workspace/state.json").write_text("{}")
    elif mutation == "credential":
        (candidate / ".env").write_text("TOKEN=secret\n")
    else:
        (candidate / "prompt.md").unlink()
        (candidate / "prompt.md").symlink_to(candidate / "PROGRAM.md")

    with pytest.raises(CandidateFailure) as raised:
        gateway.inspect(workspace, policy, hypothesis)

    assert raised.value.code is expected
    assert _git(candidate, "rev-parse", "HEAD") == policy.initialization_commit


@pytest.mark.parametrize("invalid", ("missing", "file"))
def test_candidate_git_gateway_rejects_invalid_workspace_roots(
    tmp_path: Path,
    invalid: str,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "missing"
    if invalid == "file":
        parent.write_text("not a directory\n")

    with pytest.raises(CandidateFailure) as raised:
        CandidateGitGateway().prepare(root, parent, policy, hypothesis)

    assert raised.value.code is CandidateErrorCode.INVALID_WORKSPACE


@pytest.mark.parametrize("drift", ("accepted", "candidate"))
def test_candidate_git_gateway_rejects_stale_or_unrelated_ancestry(
    tmp_path: Path,
    drift: str,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    parent = tmp_path / "candidates"
    parent.mkdir()
    gateway = CandidateGitGateway()
    workspace = gateway.prepare(root, parent, policy, hypothesis)
    target = root if drift == "accepted" else workspace.worktree_path
    (target / "prompt.md").write_text("changed\n")
    _git(target, "add", "prompt.md")
    _git(target, "commit", "-qm", "unauthorized commit")

    with pytest.raises(CandidateFailure) as raised:
        gateway.inspect(workspace, policy, hypothesis)

    assert raised.value.code is CandidateErrorCode.STALE_COMMIT


@pytest.mark.parametrize("drift", ("experiment", "source", "branch", "target"))
def test_candidate_git_gateway_rejects_policy_or_hypothesis_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    if drift == "experiment":
        hypothesis = replace(hypothesis, experiment_id="different-experiment")
    elif drift == "source":
        hypothesis = replace(hypothesis, source_commit="f" * 40)
    elif drift == "branch":
        _git(root, "branch", "-m", "wrong-branch")
    else:
        hypothesis = replace(
            hypothesis,
            target=HarnessChangeTarget(ComponentKind.TOOL, (Path("missing.py"),)),
        )

    with pytest.raises(CandidateFailure) as raised:
        CandidateGitGateway().validate_accepted(root, policy, hypothesis)

    assert raised.value.code in {
        CandidateErrorCode.STALE_COMMIT,
        CandidateErrorCode.STALE_POLICY,
    }


def test_candidate_service_creates_launches_polls_and_replays_authoritative_receipts(
    tmp_path: Path,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    runner = _FakeRunner(_controls(policy))
    locator = _FakeTraceLocator()
    outcomes = _FakeOutcomeStore()
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=locator,
        outcome_store=outcomes,
    )

    editing = service.execute(request)
    assert editing.status is CandidateStatus.WARNING
    assert editing.phase is CandidatePhase.EDITING
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("Check state before finalizing.\n")

    running = service.execute(request)
    assert running.phase is CandidatePhase.RUNNING
    assert running.candidate_id is not None
    assert running.candidate_commit is not None
    assert len(runner.runs) == 1
    still_running = service.execute(request)
    assert still_running.phase is CandidatePhase.RUNNING
    assert still_running.candidate_id == running.candidate_id
    assert len(runner.runs) == 1
    runner.summary = ExperimentSummary(
        trials=(
            ExperimentTrial(
                task_id="task-1",
                task_checksum="checksum-1",
                exception=False,
                verdict=None,
                reward=1.0,
                started_at=datetime(2026, 9, 2, 10, 1, tzinfo=UTC),
                finished_at=datetime(2026, 9, 2, 10, 1, 30, tzinfo=UTC),
                evaluated_at=datetime(2026, 9, 2, 10, 1, 31, tzinfo=UTC),
                evidence=("harbor://candidate/task-1/result.json",),
            ),
            ExperimentTrial(
                task_id="task-2",
                task_checksum="checksum-2",
                exception=False,
                verdict=None,
                reward=0.0,
                started_at=datetime(2026, 9, 2, 10, 2, tzinfo=UTC),
                finished_at=datetime(2026, 9, 2, 10, 2, 30, tzinfo=UTC),
                evaluated_at=datetime(2026, 9, 2, 10, 2, 31, tzinfo=UTC),
                evidence=("harbor://candidate/task-2/result.json",),
            ),
        )
    )

    complete = service.execute(request)
    repeated = service.execute(request)

    assert complete == repeated
    assert complete.status is CandidateStatus.SUCCESS
    assert complete.phase is CandidatePhase.COMPLETE
    assert complete.verifier_passes == 1
    assert complete.verifier_failures == 1
    assert complete.unverified_trials == 0
    assert tuple(receipt.task_id for receipt in complete.outcome_receipts) == (
        "task-1",
        "task-2",
    )
    assert len(runner.runs) == 1
    assert len(locator.requests) == 2
    assert len(outcomes.outcomes) == 2
    state = next((root / ".git/ofw/candidates").glob("*/state.json")).read_text()
    assert "input" not in state
    assert "output" not in state
    assert "test-openai-key" not in state


@pytest.mark.parametrize(
    ("trace_ids", "cursor", "trace_id", "blocker"),
    (
        (("trace-1",), None, "trace-1", None),
        ((), None, None, CandidateBlockerCode.TRACE_NOT_FOUND),
        (("trace-1", "trace-2"), None, None, CandidateBlockerCode.TRACE_AMBIGUOUS),
        (("trace-1",), "next", None, CandidateBlockerCode.TRACE_AMBIGUOUS),
    ),
)
def test_trace_locator_requires_exactly_one_complete_structural_match(
    trace_ids: tuple[str, ...],
    cursor: str | None,
    trace_id: str | None,
    blocker: CandidateBlockerCode | None,
) -> None:
    page = ObservationPage(
        tuple(_root_observation(value, str(index)) for index, value in enumerate(trace_ids)),
        None if cursor is None else PageCursor(cursor),
    )
    reader = _ObservationReader(page)
    locator = LangfuseCandidateTraceLocator(reader)
    request = TraceMatchRequest(
        task_id="task-1",
        session_id="candidate-session",
        environment="itsm-bench",
        release="d" * 40,
        started_at=datetime(2026, 9, 2, 10, 1, tzinfo=UTC),
        finished_at=datetime(2026, 9, 2, 10, 2, tzinfo=UTC),
    )

    assert locator.locate(request) == TraceMatch(trace_id=trace_id, blocker=blocker)
    query = reader.queries[0]
    assert query.session_id == request.session_id
    assert query.environment == request.environment
    assert query.release == request.release
    assert query.limit == 2
    assert query.is_root_observation is True
    assert query.window is not None
    assert query.window.start == request.started_at
    assert query.window.end == request.finished_at
    assert tuple(field.value for field in query.fields) == ("core", "basic", "trace_context")


def test_candidate_service_rejects_controls_drift_before_commit_or_launch(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    drifted = ExperimentControls(
        model="different-model",
        task_ids=policy.task_ids,
        benchmark_config_digest=policy.benchmark_config_digest,
        verifier=policy.verifier,
        environment=policy.environment,
        concurrency=policy.concurrency,
        max_retries=policy.max_retries,
    )
    runner = _FakeRunner(drifted)
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=_FakeTraceLocator(),
        outcome_store=_FakeOutcomeStore(),
    )
    editing = service.execute(request)
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("changed\n")

    rejected = service.execute(request)

    assert rejected.error_code is CandidateErrorCode.CONTROLS_DRIFT
    assert _git(editing.worktree_path, "rev-parse", "HEAD") == policy.initialization_commit
    assert runner.runs == []


def test_candidate_service_sanitizes_missing_runtime_credentials(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    runner = _FakeRunner(_controls(policy))
    runner.failure = PreparationFailure(
        PreparationErrorCode.MISSING_ENVIRONMENT,
        "OPENAI_API_KEY|AZURE_OPENAI_API_KEY",
    )
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=_FakeTraceLocator(),
        outcome_store=_FakeOutcomeStore(),
    )
    editing = service.execute(request)
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("changed\n")

    rejected = service.execute(request)

    assert rejected.error_code is CandidateErrorCode.MISSING_ENVIRONMENT
    assert "secret" not in rejected.summary


def test_candidate_service_persists_a_terminal_launch_failure(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    runner = _FakeRunner(_controls(policy))
    runner.start_failure = PreparationFailure(PreparationErrorCode.LAUNCH_FAILED, "harbor")
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=_FakeTraceLocator(),
        outcome_store=_FakeOutcomeStore(),
    )
    editing = service.execute(request)
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("changed\n")

    failed = service.execute(request)
    repeated = service.execute(request)

    assert failed == repeated
    assert failed.phase is CandidatePhase.FAILED
    assert failed.error_code is CandidateErrorCode.LAUNCH_FAILED
    assert failed.candidate_commit is not None
    assert runner.start_count == 1


def test_candidate_service_persists_timeout_and_ignores_late_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    runner = _FakeRunner(_controls(policy))
    outcomes = _FakeOutcomeStore()
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=_FakeTraceLocator(),
        outcome_store=outcomes,
    )
    editing = service.execute(request)
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("changed\n")
    running = service.execute(request)
    assert running.phase is CandidatePhase.RUNNING

    class _ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _ExpiredDateTime:
            del tz
            return cls(2100, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(candidate_service_module, "datetime", _ExpiredDateTime)
    timed_out = service.execute(request)
    runner.summary = ExperimentSummary(())
    repeated = service.execute(request)

    assert timed_out == repeated
    assert timed_out.phase is CandidatePhase.FAILED
    assert timed_out.error_code is CandidateErrorCode.CANDIDATE_TIMEOUT
    assert outcomes.outcomes == []


def test_candidate_service_rejects_a_missing_hypothesis_receipt(tmp_path: Path) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    request = _candidate_request(tmp_path, root, hypothesis)
    missing = CandidateExecutionInput(
        workspace_root=request.workspace_root,
        worktree_parent=request.worktree_parent,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=request.harbor_config,
        experiment_id=request.experiment_id,
        hypothesis_id="sha256:" + "f" * 64,
    )
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=_FakeRunner(_controls(policy)),
        trace_locator=_FakeTraceLocator(),
        outcome_store=_FakeOutcomeStore(),
    )

    rejected = service.execute(missing)

    assert rejected.error_code is CandidateErrorCode.STALE_POLICY


def test_candidate_service_rejects_a_reused_hypothesis_with_different_runtime_paths(
    tmp_path: Path,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=_FakeRunner(_controls(policy)),
        trace_locator=_FakeTraceLocator(),
        outcome_store=_FakeOutcomeStore(),
    )
    service.execute(request)
    conflicting = CandidateExecutionInput(
        workspace_root=request.workspace_root,
        worktree_parent=request.worktree_parent,
        benchmark_root=request.benchmark_root,
        harbor_executable=request.harbor_executable,
        harbor_config=Path("different.json"),
        experiment_id=request.experiment_id,
        hypothesis_id=request.hypothesis_id,
    )

    rejected = service.execute(conflicting)

    assert rejected.error_code is CandidateErrorCode.REQUEST_CONFLICT


def test_candidate_service_keeps_unsupported_and_ambiguous_trials_unverified(
    tmp_path: Path,
) -> None:
    root, policy, hypothesis = _authority(tmp_path)
    (tmp_path / "candidates").mkdir()
    request = _candidate_request(tmp_path, root, hypothesis)
    runner = _FakeRunner(_controls(policy))
    locator = _FakeTraceLocator()
    locator.blocker = CandidateBlockerCode.TRACE_AMBIGUOUS
    outcomes = _FakeOutcomeStore()
    service = CandidateExecutionService(
        workspace=CandidateGitGateway(),
        hypotheses=FileHypothesisRepository(),
        runner=runner,
        trace_locator=locator,
        outcome_store=outcomes,
    )
    editing = service.execute(request)
    assert editing.worktree_path is not None
    (editing.worktree_path / "prompt.md").write_text("changed\n")
    service.execute(request)
    runner.summary = ExperimentSummary(
        trials=(
            ExperimentTrial(
                task_id="task-1",
                task_checksum="checksum-1",
                exception=False,
                verdict=None,
                reward=0.5,
                started_at=datetime(2026, 9, 2, 10, 1, tzinfo=UTC),
                finished_at=datetime(2026, 9, 2, 10, 1, 30, tzinfo=UTC),
                evaluated_at=datetime(2026, 9, 2, 10, 1, 31, tzinfo=UTC),
                evidence=("harbor://candidate/task-1/result.json",),
            ),
            ExperimentTrial(
                task_id="task-2",
                task_checksum="checksum-2",
                exception=False,
                verdict=None,
                reward=0.0,
                started_at=datetime(2026, 9, 2, 10, 2, tzinfo=UTC),
                finished_at=datetime(2026, 9, 2, 10, 2, 30, tzinfo=UTC),
                evaluated_at=datetime(2026, 9, 2, 10, 2, 31, tzinfo=UTC),
                evidence=("harbor://candidate/task-2/result.json",),
            ),
        )
    )

    complete = service.execute(request)

    assert complete.outcome_receipts == ()
    assert complete.status is CandidateStatus.WARNING
    assert tuple(blocker.code for blocker in complete.blockers) == (
        CandidateBlockerCode.UNSUPPORTED_REWARD,
        CandidateBlockerCode.TRACE_AMBIGUOUS,
    )
    assert complete.unverified_trials == 2
    assert len(locator.requests) == 1
    assert outcomes.outcomes == []
