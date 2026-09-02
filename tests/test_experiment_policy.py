"""Canonical experiment policy publication and reload boundaries."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.preparation.contracts import (
    BaselineConfiguration,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import (
    ExperimentPolicyErrorCode,
    ExperimentPolicyFailure,
    ExperimentPolicySnapshot,
    FileExperimentPolicyRepository,
    build_experiment_policy,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "harness"
    root.mkdir()
    (root / "prompt.md").write_text("Original prompt.\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "OpenFlywheel Test")
    _git(root, "config", "user.email", "ofw@example.test")
    _git(root, "add", "prompt.md")
    _git(root, "commit", "-qm", "initial")
    return root, _git(root, "rev-parse", "HEAD")


def _request(
    tmp_path: Path,
    root: Path,
    *,
    goal: str = "Improve verifier-backed quality.",
) -> PrepareWorkspaceInput:
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    executable = tmp_path / "harbor"
    executable.touch(mode=0o700)
    return PrepareWorkspaceInput(
        experiment_id="experiment-one",
        harness_root=root,
        base_ref="HEAD",
        worktree_parent=tmp_path,
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=Path("config.json"),
        expected_task_count=2,
        editable_paths=(Path("prompt.md"),),
        goal=goal,
        quality_target=0.9,
        max_iterations=4,
        no_improvement_limit=2,
        max_cost_per_task_usd=1.5,
        max_latency_seconds=120.0,
        max_baseline_seconds=600,
    )


def _snapshot(
    tmp_path: Path,
    *,
    goal: str = "Improve verifier-backed quality.",
) -> tuple[Path, ExperimentPolicySnapshot]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, commit = _repository(tmp_path)
    request = _request(tmp_path, root, goal=goal)
    prepared = PreparedGitWorkspace(
        branch_name="ofw/experiment-one",
        worktree_path=root,
        base_commit=commit,
        initialization_commit=commit,
        program_path=root / "PROGRAM.md",
    )
    baseline = BaselineConfiguration(
        model="openai/gpt-5.4-mini",
        task_ids=("tasks/a", "tasks/b"),
        benchmark_config_digest=f"sha256:{hashlib.sha256(b'config').hexdigest()}",
        verifier="itsm-bench",
        environment="itsm-bench",
    )
    return root, build_experiment_policy(request, prepared, baseline)


def _control_directory(root: Path) -> Path:
    path = root / ".git/ofw/preparations/experiment-one"
    path.mkdir(parents=True)
    return path


def test_policy_is_derived_from_validated_preparation_inputs(tmp_path: Path) -> None:
    _, policy = _snapshot(tmp_path)

    assert policy.experiment_id == "experiment-one"
    assert policy.editable_paths == (Path("prompt.md"),)
    assert policy.task_ids == ("tasks/a", "tasks/b")
    assert policy.model == "openai/gpt-5.4-mini"
    assert policy.verifier == "itsm-bench"
    assert policy.environment == "itsm-bench"
    assert policy.concurrency == 1
    assert policy.max_retries == 0
    assert policy.controls_digest.startswith("sha256:")
    assert policy.controls_digest == policy.recomputed_controls_digest()


def test_policy_schema_rejects_extra_fields_and_tampered_controls(tmp_path: Path) -> None:
    _, policy = _snapshot(tmp_path)
    payload = policy.model_dump_json()
    extra = payload[:-1] + ',"unexpected":true}'

    with pytest.raises(ValidationError):
        ExperimentPolicySnapshot.model_validate_json(extra)

    tampered = payload.replace(policy.model, "different-model")
    with pytest.raises(ValidationError):
        ExperimentPolicySnapshot.model_validate_json(tampered)

    escaped = payload.replace('"editable_paths":["prompt.md"]', '"editable_paths":["../x"]')
    with pytest.raises(ValidationError):
        ExperimentPolicySnapshot.model_validate_json(escaped)


def test_policy_publish_is_atomic_idempotent_and_conflict_detecting(tmp_path: Path) -> None:
    root, policy = _snapshot(tmp_path)
    control = _control_directory(root)
    repository = FileExperimentPolicyRepository()

    first = repository.publish(control, policy)
    repeated = repository.publish(control, policy)

    assert first == repeated == control / "policy.json"
    assert repository.load(root, "experiment-one") == policy
    _, conflicting = _snapshot(tmp_path / "conflict", goal="Conflicting goal.")
    with pytest.raises(ExperimentPolicyFailure) as raised:
        repository.publish(control, conflicting)
    assert raised.value.code is ExperimentPolicyErrorCode.POLICY_CONFLICT


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_policy_rejects_non_regular_existing_file(tmp_path: Path, kind: str) -> None:
    root, policy = _snapshot(tmp_path)
    control = _control_directory(root)
    path = control / "policy.json"
    if kind == "symlink":
        path.symlink_to(root / "prompt.md")
    else:
        os.mkfifo(path)

    with pytest.raises(ExperimentPolicyFailure) as raised:
        FileExperimentPolicyRepository().publish(control, policy)

    assert raised.value.code is ExperimentPolicyErrorCode.POLICY_WRITE_FAILED


def test_policy_reload_rejects_missing_oversized_and_invalid_files(tmp_path: Path) -> None:
    root, _ = _snapshot(tmp_path)
    control = _control_directory(root)
    repository = FileExperimentPolicyRepository()

    with pytest.raises(ExperimentPolicyFailure) as missing:
        repository.load(root, "experiment-one")
    assert missing.value.code is ExperimentPolicyErrorCode.POLICY_SNAPSHOT_REQUIRED

    path = control / "policy.json"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ExperimentPolicyFailure) as oversized:
        repository.load(root, "experiment-one")
    assert oversized.value.code is ExperimentPolicyErrorCode.POLICY_TOO_LARGE

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ExperimentPolicyFailure) as invalid:
        repository.load(root, "experiment-one")
    assert invalid.value.code is ExperimentPolicyErrorCode.POLICY_INVALID


def test_policy_publish_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, policy = _snapshot(tmp_path)
    control = _control_directory(root)
    calls: list[int] = []
    original = os.fsync

    def capture(descriptor: int) -> None:
        calls.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", capture)

    FileExperimentPolicyRepository().publish(control, policy)

    assert len(calls) >= 2
