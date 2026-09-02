"""Isolated, re-entrant preparation of an ITSM harness workspace."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from ofw.preparation import (
    BaselineConfiguration,
    BaselineRun,
    BaselineSummary,
    PreparationErrorCode,
    PreparationFailure,
    PreparationPhase,
    PreparationStatus,
    PrepareWorkspaceInput,
    WorkspacePreparationObservation,
    WorkspacePreparationService,
)
from ofw.preparation.harbor import HarborBaselineRunner
from ofw.preparation.policy import (
    ExperimentPolicyErrorCode,
    ExperimentPolicyFailure,
    ExperimentPolicySnapshot,
    FileExperimentPolicyRepository,
)
from ofw.preparation.worktree import GitWorktreeGateway


class _EnvironmentCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str
    environment: str
    release: str
    session: str


class _FailingRunner:
    def validate(self, request: PrepareWorkspaceInput) -> BaselineConfiguration:
        return BaselineConfiguration(
            model="openai/gpt-5.4-mini",
            task_ids=("task-1",),
            benchmark_config_digest="sha256:" + "1" * 64,
            verifier="itsm-bench",
            environment="itsm-bench",
        )

    def start(self, run: BaselineRun) -> int:
        raise PreparationFailure(PreparationErrorCode.LAUNCH_FAILED, "harbor")

    def summarize(self, run: BaselineRun) -> BaselineSummary | None:
        return None


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _harness_repository(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "prompt.md").write_text("Verify the outcome.\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "OpenFlywheel Test")
    _git(root, "config", "user.email", "ofw@example.test")
    _git(root, "add", "prompt.md")
    _git(root, "commit", "-qm", "baseline harness")
    return root


def _fake_harbor(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-harbor"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
job_name = arguments[arguments.index("--job-name") + 1]
jobs_dir = Path(arguments[arguments.index("--jobs-dir") + 1])
root = jobs_dir / job_name
root.mkdir(parents=True)
(Path.cwd() / "invocations.txt").write_text(
    (Path.cwd() / "invocations.txt").read_text() + "run\\n"
    if (Path.cwd() / "invocations.txt").exists()
    else "run\\n",
    encoding="utf-8",
)
(root / "environment.json").write_text(json.dumps({
    "source": os.environ["OFW_HERMES_SOURCE"],
    "environment": os.environ["HERMES_LANGFUSE_ENV"],
    "release": os.environ["HERMES_LANGFUSE_RELEASE"],
    "session": os.environ["HERMES_LANGFUSE_SESSION_ID"],
}), encoding="utf-8")
trials = (("task-pass", 1.0), ("task-fail", 0.0))
for index, (task_name, reward) in enumerate(trials):
    trial = root / f"{task_name}__trial"
    (trial / "verifier").mkdir(parents=True)
    result = {
        "task_name": f"fixture/{task_name}",
        "task_checksum": f"checksum-{index}",
        "exception_info": None,
        "agent_execution": {
            "started_at": "2026-08-27T20:00:00Z",
            "finished_at": "2026-08-27T20:01:00Z",
        },
        "verifier": {
            "started_at": "2026-08-27T20:01:00Z",
            "finished_at": "2026-08-27T20:01:01Z",
        },
        "verifier_result": {"rewards": {"reward": reward}},
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (trial / "verifier" / "reward.txt").write_text(str(int(reward)), encoding="utf-8")
    (trial / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
(root / "result.json").write_text(json.dumps({
    "finished_at": "2026-08-27T20:01:02Z",
    "n_total_trials": 2,
}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _benchmark_repository(tmp_path: Path, executable: Path) -> tuple[Path, Path]:
    root = tmp_path / "itsm-bench"
    root.mkdir()
    adapter = root / "agents/ofw_hermes.py"
    adapter.parent.mkdir()
    adapter.write_text(
        'HERMES_SOURCE_ENVIRONMENT = "OFW_HERMES_SOURCE"\n',
        encoding="utf-8",
    )
    config = root / "config.json"
    config.write_text(
        """{
  "agents": [
    {
      "name": "agents.ofw_hermes:OfwHermes",
      "model_name": "openai/gpt-5.4-mini"
    }
  ],
  "tasks": [
    {"path": "tasks/task-pass"},
    {"path": "tasks/task-fail"}
  ]
}
""",
        encoding="utf-8",
    )
    assert executable.is_absolute()
    return root, config


def _request(
    harness_root: Path,
    worktree_parent: Path,
    benchmark_root: Path,
    harbor_executable: Path,
    harbor_config: Path,
    *,
    goal: str = "Reach full ITSM verifier pass rate.",
    expected_task_count: int = 2,
) -> PrepareWorkspaceInput:
    return PrepareWorkspaceInput(
        experiment_id="itsm-hermes-demo",
        harness_root=harness_root,
        base_ref="HEAD",
        worktree_parent=worktree_parent,
        benchmark_root=benchmark_root,
        harbor_executable=harbor_executable,
        harbor_config=harbor_config.relative_to(benchmark_root),
        expected_task_count=expected_task_count,
        editable_paths=(Path("prompt.md"),),
        goal=goal,
        quality_target=1.0,
        max_iterations=5,
        no_improvement_limit=3,
        max_cost_per_task_usd=1.0,
        max_latency_seconds=600.0,
        max_baseline_seconds=60,
    )


def _service() -> WorkspacePreparationService:
    return WorkspacePreparationService(
        runner=HarborBaselineRunner(),
        workspace=GitWorktreeGateway(),
        base_program="# Base program\n\nBaseline is complete.\n",
        itsm_program="## ITSM program\n\nUse verifier outcomes.\n",
    )


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/openai/v1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.test")


def _wait_until_ready(
    service: WorkspacePreparationService,
    request: PrepareWorkspaceInput,
) -> WorkspacePreparationObservation:
    for _ in range(100):
        observation = service.prepare(request)
        if observation.phase is PreparationPhase.READY:
            return observation
        time.sleep(0.02)
    pytest.fail("preparation did not become ready")


def test_prepare_workspace_creates_isolated_branch_commit_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    (harness_root / "local-notes.txt").write_text("preserve me\n", encoding="utf-8")
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)
    _credentials(monkeypatch)

    first = _service().prepare(request)

    assert first.status is PreparationStatus.WARNING
    assert first.phase is PreparationPhase.RUNNING
    assert _git(harness_root, "branch", "--show-current") in ("main", "master")
    assert (harness_root / "local-notes.txt").read_text(encoding="utf-8") == "preserve me\n"

    ready = _wait_until_ready(_service(), request)
    worktree = worktree_parent / "hermes-ofw-itsm-hermes-demo"
    initialization_commit = _git(worktree, "rev-parse", "HEAD")
    environment = _EnvironmentCapture.model_validate_json(
        (benchmark_root / "jobs/itsm-hermes-demo/environment.json").read_text(encoding="utf-8")
    )

    assert ready.status is PreparationStatus.SUCCESS
    assert ready.phase is PreparationPhase.READY
    assert ready.branch_name == "ofw/itsm-hermes-demo"
    assert ready.worktree_path == worktree
    assert ready.initialization_commit == initialization_commit
    assert ready.terminal_trials == 2
    assert ready.verifier_passes == 1
    assert ready.verifier_failures == 1
    assert ready.unverified_trials == 0
    assert _git(worktree, "status", "--short") == ""
    assert _git(worktree, "show", "--format=", "--name-only", "HEAD").splitlines() == [
        "PROGRAM.md",
        "experiment_config.yaml",
    ]
    assert environment == _EnvironmentCapture(
        source=str(worktree),
        environment="itsm-bench",
        release=initialization_commit,
        session="itsm-hermes-demo",
    )
    policy = FileExperimentPolicyRepository().load(worktree, "itsm-hermes-demo")
    assert policy.base_commit == ready.base_commit
    assert policy.initialization_commit == ready.initialization_commit
    assert policy.editable_paths == (Path("prompt.md"),)
    assert policy.task_ids == ("tasks/task-pass", "tasks/task-fail")
    assert policy.model == "openai/gpt-5.4-mini"
    assert policy.concurrency == 1
    assert policy.max_retries == 0
    assert policy.controls_digest == policy.recomputed_controls_digest()
    assert (benchmark_root / "invocations.txt").read_text(encoding="utf-8") == "run\n"
    persisted_text = "\n".join(
        (
            (worktree / "PROGRAM.md").read_text(encoding="utf-8"),
            (worktree / "experiment_config.yaml").read_text(encoding="utf-8"),
            (
                harness_root
                / ".git/ofw/preparations/itsm-hermes-demo/state.json"
            ).read_text(encoding="utf-8"),
            (
                harness_root
                / ".git/ofw/preparations/itsm-hermes-demo/baseline.log"
            ).read_text(encoding="utf-8"),
            (
                harness_root
                / ".git/ofw/preparations/itsm-hermes-demo/policy.json"
            ).read_text(encoding="utf-8"),
        )
    )
    assert "test-openai-key" not in persisted_text
    assert "sk-lf-test" not in persisted_text
    experiment_config = (worktree / "experiment_config.yaml").read_text(encoding="utf-8")
    assert f'  root: "{benchmark_root}"' in experiment_config
    assert '  job_name: "itsm-hermes-demo"' in experiment_config

    repeated = _service().prepare(request)

    assert repeated == ready
    assert (benchmark_root / "invocations.txt").read_text(encoding="utf-8") == "run\n"


def test_prepare_workspace_rejects_reused_id_with_different_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    _credentials(monkeypatch)
    service = _service()
    first = _request(harness_root, worktree_parent, benchmark_root, harbor, config)
    conflicting = _request(
        harness_root,
        worktree_parent,
        benchmark_root,
        harbor,
        config,
        goal="A different goal.",
    )
    _wait_until_ready(service, first)

    result = service.prepare(conflicting)

    assert result.status is PreparationStatus.ERROR
    assert result.phase is PreparationPhase.FAILED
    assert result.error_code is PreparationErrorCode.REQUEST_CONFLICT
    assert (benchmark_root / "invocations.txt").read_text(encoding="utf-8") == "run\n"


def test_prepare_workspace_rejects_task_count_before_creating_branch(tmp_path: Path) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    invalid = _request(
        harness_root,
        worktree_parent,
        benchmark_root,
        harbor,
        config,
        expected_task_count=3,
    )

    result = _service().prepare(invalid)

    assert result.status is PreparationStatus.ERROR
    assert result.error_code is PreparationErrorCode.TASK_COUNT_MISMATCH
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""


@pytest.mark.parametrize(
    "content",
    (b"x" * (2 * 1024 * 1024 + 1), b"\xff"),
    ids=("oversized", "invalid-utf8"),
)
def test_prepare_workspace_sanitizes_invalid_harbor_config_reads(
    tmp_path: Path,
    content: bytes,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    config.write_bytes(content)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.INVALID_HARBOR_CONFIG
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""


def test_prepare_workspace_rejects_oversized_model_before_creating_branch(tmp_path: Path) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "openai/gpt-5.4-mini",
            "m" * 257,
        ),
        encoding="utf-8",
    )
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.INVALID_HARBOR_CONFIG
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""


def test_policy_publication_failure_resumes_without_recreating_git_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)
    _credentials(monkeypatch)
    original = FileExperimentPolicyRepository.publish
    attempts = 0

    def fail_once(
        repository: FileExperimentPolicyRepository,
        control_directory: Path,
        policy: ExperimentPolicySnapshot,
    ) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExperimentPolicyFailure(
                ExperimentPolicyErrorCode.POLICY_WRITE_FAILED,
                request.experiment_id,
            )
        return original(repository, control_directory, policy)

    monkeypatch.setattr(FileExperimentPolicyRepository, "publish", fail_once)
    service = _service()

    first = service.prepare(request)
    ready = _wait_until_ready(service, request)

    assert first.error_code is PreparationErrorCode.POLICY_WRITE_FAILED
    assert ready.phase is PreparationPhase.READY
    assert attempts == 2
    assert (benchmark_root / "invocations.txt").read_text(encoding="utf-8") == "run\n"


def test_policy_conflict_remains_typed_and_non_retryable_after_git_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)
    _credentials(monkeypatch)

    def conflict(
        repository: FileExperimentPolicyRepository,
        control_directory: Path,
        policy: ExperimentPolicySnapshot,
    ) -> Path:
        del repository, control_directory, policy
        raise ExperimentPolicyFailure(
            ExperimentPolicyErrorCode.POLICY_CONFLICT,
            request.experiment_id,
        )

    monkeypatch.setattr(FileExperimentPolicyRepository, "publish", conflict)
    service = _service()

    first = service.prepare(request)
    repeated = service.prepare(request)

    assert first.error_code is repeated.error_code is PreparationErrorCode.POLICY_CONFLICT
    assert first.retry is not None and "do not retry" in first.retry.lower()
    assert first.stop_when is not None and "new experiment" in first.stop_when.lower()
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo")

def test_prepare_workspace_input_rejects_relative_roots(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        PrepareWorkspaceInput(
            experiment_id="demo",
            harness_root=Path("relative"),
            base_ref="HEAD",
            worktree_parent=tmp_path,
            benchmark_root=tmp_path,
            harbor_executable=tmp_path / "harbor",
            harbor_config=Path("config.json"),
            expected_task_count=1,
            editable_paths=(Path("prompt.md"),),
            goal="Improve.",
            quality_target=1.0,
            max_iterations=1,
            no_improvement_limit=1,
            max_baseline_seconds=60,
        )


def test_prepare_workspace_input_rejects_nul_and_oversized_editable_paths(tmp_path: Path) -> None:
    for editable in (Path("bad\x00path"), Path("p" * 1025)):
        with pytest.raises(ValidationError):
            PrepareWorkspaceInput(
                experiment_id="demo",
                harness_root=tmp_path,
                base_ref="HEAD",
                worktree_parent=tmp_path,
                benchmark_root=tmp_path,
                harbor_executable=tmp_path / "harbor",
                harbor_config=Path("config.json"),
                expected_task_count=1,
                editable_paths=(editable,),
                goal="Improve.",
                quality_target=1.0,
                max_iterations=1,
                no_improvement_limit=1,
                max_baseline_seconds=60,
            )


def test_prepare_workspace_input_rejects_oversized_raw_path_before_normalization(
    tmp_path: Path,
) -> None:
    redundant = "./" * 600 + "prompt.md"
    payload = f"""{{
  "experiment_id": "demo",
  "harness_root": "{tmp_path}",
  "base_ref": "HEAD",
  "worktree_parent": "{tmp_path}",
  "benchmark_root": "{tmp_path}",
  "harbor_executable": "{tmp_path / 'harbor'}",
  "harbor_config": "config.json",
  "expected_task_count": 1,
  "editable_paths": ["{redundant}"],
  "goal": "Improve.",
  "quality_target": 1.0,
  "max_iterations": 1,
  "no_improvement_limit": 1,
  "max_baseline_seconds": 60
}}"""

    with pytest.raises(ValidationError):
        PrepareWorkspaceInput.model_validate_json(payload)


def test_legacy_preparation_state_requires_a_fresh_preparation_id(tmp_path: Path) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)
    control = GitWorktreeGateway().control_directory(harness_root, request.experiment_id)
    control.mkdir(parents=True)
    (control / "state.json").write_text('{"schema_version":1}\n', encoding="utf-8")

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.POLICY_SNAPSHOT_REQUIRED
    assert result.retry is not None and "new experiment id" in result.retry.lower()
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""


def test_prepare_workspace_input_accepts_json_path_strings(tmp_path: Path) -> None:
    config = PrepareWorkspaceInput.model_validate_json(
        f"""{{
  "experiment_id": "demo",
  "harness_root": "{tmp_path / 'harness'}",
  "base_ref": "HEAD",
  "worktree_parent": "{tmp_path / 'worktrees'}",
  "benchmark_root": "{tmp_path / 'itsm'}",
  "harbor_executable": "{tmp_path / 'harbor'}",
  "harbor_config": "config.json",
  "expected_task_count": 1,
  "editable_paths": ["prompt.md"],
  "goal": "Improve.",
  "quality_target": 1.0,
  "max_iterations": 1,
  "no_improvement_limit": 1,
  "max_baseline_seconds": 60
}}"""
    )

    assert config.harness_root == tmp_path / "harness"
    assert config.editable_paths == (Path("prompt.md"),)


def test_launch_failure_persists_after_initialization_commit(tmp_path: Path) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    request = _request(
        harness_root,
        worktree_parent,
        benchmark_root,
        harbor,
        config,
        expected_task_count=1,
    )
    service = WorkspacePreparationService(
        runner=_FailingRunner(),
        workspace=GitWorktreeGateway(),
        base_program="# Base\n",
        itsm_program="## ITSM\n",
    )

    first = service.prepare(request)
    repeated = service.prepare(request)

    assert first.error_code is PreparationErrorCode.LAUNCH_FAILED
    assert repeated.error_code is PreparationErrorCode.LAUNCH_FAILED
    assert first.worktree_path == worktree_parent / "hermes-ofw-itsm-hermes-demo"
    assert first.initialization_commit is not None
    assert repeated == first
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo")


def test_prepare_workspace_refuses_an_existing_experiment_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    _git(harness_root, "branch", "ofw/itsm-hermes-demo")
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    _credentials(monkeypatch)
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.BRANCH_EXISTS
    assert not (worktree_parent / "hermes-ofw-itsm-hermes-demo").exists()


def test_prepare_workspace_requires_worktree_aware_hermes_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    (benchmark_root / "agents/ofw_hermes.py").unlink()
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    _credentials(monkeypatch)
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.INVALID_HARBOR_CONFIG
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""


def test_prepare_workspace_reports_missing_credentials_before_creating_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_root = _harness_repository(tmp_path)
    harbor = _fake_harbor(tmp_path)
    benchmark_root, config = _benchmark_repository(tmp_path, harbor)
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    for name in (
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "AZURE_OPENAI_BASE_URL",
        "HERMES_LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "HERMES_LANGFUSE_SECRET_KEY",
        "LANGFUSE_SECRET_KEY",
        "HERMES_LANGFUSE_BASE_URL",
        "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    request = _request(harness_root, worktree_parent, benchmark_root, harbor, config)

    result = _service().prepare(request)

    assert result.error_code is PreparationErrorCode.MISSING_ENVIRONMENT
    assert _git(harness_root, "branch", "--list", "ofw/itsm-hermes-demo") == ""
