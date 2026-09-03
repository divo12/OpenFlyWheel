"""Generalized deterministic Harbor execution for candidate and baseline runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from ofw.preparation.contracts import (
    BaselineRun,
    ExperimentControls,
    ExperimentRun,
    ExperimentSummary,
    PreparationErrorCode,
    PreparationFailure,
)
from ofw.preparation.harbor import HarborBaselineRunner, HarborExperimentRunner


class _EnvironmentCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str
    environment: str
    release: str
    session: str


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/openai/v1")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example.test")


def _benchmark(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "benchmark"
    root.mkdir()
    adapter = root / "agents/ofw_hermes.py"
    adapter.parent.mkdir()
    adapter.write_text('SOURCE = "OFW_HERMES_SOURCE"\n')
    config = root / "config.json"
    config.write_text(
        """{
  "agents": [{
    "name": "agents.ofw_hermes:OfwHermes",
    "model_name": "openai/gpt-5.4-mini"
  }],
  "tasks": [{"path": "task-1"}, {"path": "task-2"}]
}
"""
    )
    executable = tmp_path / "harbor"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
name = args[args.index("--job-name") + 1]
root = Path(args[args.index("--jobs-dir") + 1]) / name
root.mkdir(parents=True)
(root / "environment.json").write_text(json.dumps({
    "source": os.environ["OFW_HERMES_SOURCE"],
    "environment": os.environ["HERMES_LANGFUSE_ENV"],
    "release": os.environ["HERMES_LANGFUSE_RELEASE"],
    "session": os.environ["HERMES_LANGFUSE_SESSION_ID"],
}))
for index, reward in enumerate((1.0, 0.0), start=1):
    trial = root / f"task-{index}__trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "task_name": f"display-{index}",
        "task_id": f"task-{index}",
        "task_checksum": f"checksum-{index}",
        "exception_info": None,
        "agent_execution": {
            "started_at": f"2026-09-02T10:0{index}:00Z",
            "finished_at": f"2026-09-02T10:0{index}:30Z"
        },
        "verifier": {"finished_at": f"2026-09-02T10:0{index}:31Z"},
        "verifier_result": {"rewards": {"reward": reward}}
    }))
(root / "result.json").write_text(json.dumps({
    "finished_at": "2026-09-02T10:03:00Z",
    "n_total_trials": 2
}))
"""
    )
    executable.chmod(0o755)
    return root, executable, config


def test_generalized_harbor_runner_freezes_controls_environment_and_trials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    runner = HarborExperimentRunner()

    controls = runner.validate(benchmark, executable, config.relative_to(benchmark))
    run = ExperimentRun(
        run_id="candidate-one",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=benchmark / "jobs/candidate-one",
        log_path=tmp_path / "candidate.log",
        source_root=source,
        release="a" * 40,
        session_id="sha256-" + "b" * 64,
        controls=controls,
    )
    pid = runner.start(run)
    waited, status = os.waitpid(pid, 0)
    summary = runner.summarize(run)

    assert controls == ExperimentControls(
        model="openai/gpt-5.4-mini",
        task_ids=("task-1", "task-2"),
        benchmark_config_digest=controls.benchmark_config_digest,
        verifier="itsm-bench",
        environment="itsm-bench",
        concurrency=1,
        max_retries=0,
    )
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert isinstance(summary, ExperimentSummary)
    assert tuple(trial.task_id for trial in summary.trials) == ("task-1", "task-2")
    assert summary.trials[0].reward == 1.0
    assert summary.trials[1].reward == 0.0
    assert summary.trials[0].started_at == datetime(2026, 9, 2, 10, 1, tzinfo=UTC)
    assert summary.trials[0].evidence == (
        "harbor://candidate-one/task-1__trial/result.json",
        "harbor://candidate-one/task-1__trial/verifier",
    )
    environment = _EnvironmentCapture.model_validate_json(
        (run.job_path / "environment.json").read_text()
    )
    assert environment == _EnvironmentCapture(
        source=str(source),
        environment="itsm-bench",
        release="a" * 40,
        session="sha256-" + "b" * 64,
    )


def test_generalized_harbor_runner_rechecks_controls_immediately_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    runner = HarborExperimentRunner()
    controls = runner.validate(benchmark, executable, config.relative_to(benchmark))
    run = ExperimentRun(
        run_id="candidate-one",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=benchmark / "jobs/candidate-one",
        log_path=tmp_path / "candidate.log",
        source_root=source,
        release="a" * 40,
        session_id="sha256-" + "b" * 64,
        controls=controls,
    )
    config.write_text(config.read_text().replace("gpt-5.4-mini", "different-model"))

    with pytest.raises(PreparationFailure) as raised:
        runner.start(run)

    assert raised.value.code is PreparationErrorCode.INVALID_HARBOR_CONFIG
    assert not run.job_path.exists()


def test_harbor_rejects_existing_jobs_and_baseline_launch_recomputes_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    experiment = HarborExperimentRunner()
    controls = experiment.validate(benchmark, executable, config.relative_to(benchmark))
    job = benchmark / "jobs/candidate-one"
    job.mkdir(parents=True)
    run = ExperimentRun(
        run_id="candidate-one",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=job,
        log_path=tmp_path / "candidate.log",
        source_root=source,
        release="a" * 40,
        session_id="candidate-session",
        controls=controls,
    )

    with pytest.raises(PreparationFailure) as existing:
        experiment.start(run)
    baseline_run = BaselineRun(
        experiment_id="baseline",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=benchmark / "jobs/baseline",
        log_path=tmp_path / "baseline.log",
        worktree_path=source,
        initialization_commit="a" * 40,
    )
    pid = HarborBaselineRunner().start(baseline_run)
    waited, status = os.waitpid(pid, 0)

    assert existing.value.code is PreparationErrorCode.LAUNCH_FAILED
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert baseline_run.job_path.is_dir()


@pytest.mark.parametrize(
    ("started_at", "finished_at", "evaluated_at"),
    (
        (
            "2026-09-02T10:01:00",
            "2026-09-02T10:01:30Z",
            "2026-09-02T10:01:31Z",
        ),
        (
            "2026-09-02T10:01:30Z",
            "2026-09-02T10:01:00Z",
            "2026-09-02T10:01:31Z",
        ),
        (
            "2026-09-02T10:01:00Z",
            "2026-09-02T10:01:30Z",
            "2026-09-02T10:01:29Z",
        ),
    ),
)
def test_generalized_harbor_runner_maps_invalid_trial_time_to_typed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started_at: str,
    finished_at: str,
    evaluated_at: str,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    runner = HarborExperimentRunner()
    controls = runner.validate(benchmark, executable, config.relative_to(benchmark))
    job = benchmark / "jobs/candidate-one"
    trial = job / "task-1__trial"
    trial.mkdir(parents=True)
    (job / "result.json").write_text('{"finished_at":"2026-09-02T10:03:00Z","n_total_trials":1}')
    (trial / "result.json").write_text(
        f"""{{
  "task_id": "task-1",
  "task_name": "display name",
  "task_checksum": "checksum-1",
  "exception_info": null,
  "agent_execution": {{"started_at": "{started_at}", "finished_at": "{finished_at}"}},
  "verifier": {{"finished_at": "{evaluated_at}"}},
  "verifier_result": {{"rewards": {{"reward": 1.0}}}}
}}"""
    )
    run = ExperimentRun(
        run_id="candidate-one",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=job,
        log_path=tmp_path / "candidate.log",
        source_root=source,
        release="a" * 40,
        session_id="candidate-session",
        controls=controls,
    )

    with pytest.raises(PreparationFailure) as raised:
        runner.summarize(run)

    assert raised.value.code is PreparationErrorCode.INVALID_BASELINE_RESULT
    assert raised.value.subject == "trial timestamps"


def test_generalized_harbor_runner_rejects_trials_without_mapping_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    runner = HarborExperimentRunner()
    controls = runner.validate(benchmark, executable, config.relative_to(benchmark))
    job = benchmark / "jobs/candidate-one"
    trial = job / "task-1__trial"
    trial.mkdir(parents=True)
    (job / "result.json").write_text('{"finished_at":"2026-09-02T10:03:00Z","n_total_trials":1}')
    (trial / "result.json").write_text(
        '{"exception_info":null,"verifier_result":{"rewards":{"reward":1.0}}}'
    )
    run = ExperimentRun(
        run_id="candidate-one",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=job,
        log_path=tmp_path / "candidate.log",
        source_root=source,
        release="a" * 40,
        session_id="candidate-session",
        controls=controls,
    )

    with pytest.raises(PreparationFailure) as raised:
        runner.summarize(run)

    assert raised.value.code is PreparationErrorCode.INVALID_BASELINE_RESULT
