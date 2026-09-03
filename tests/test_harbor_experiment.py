"""Generalized deterministic Harbor execution for candidate and baseline runs."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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


def _cancel_run(tmp_path: Path) -> ExperimentRun:
    return ExperimentRun(
        run_id="candidate-one",
        benchmark_root=tmp_path,
        harbor_executable=Path(sys.executable),
        harbor_config=tmp_path / "config.json",
        job_path=tmp_path / "jobs/candidate-one",
        log_path=tmp_path / "candidate.log",
        source_root=tmp_path,
        release="a" * 40,
        session_id="candidate-session",
        controls=ExperimentControls(
            model="model",
            task_ids=("task-1",),
            benchmark_config_digest="sha256:" + "b" * 64,
            verifier="itsm-bench",
            environment="itsm-bench",
            concurrency=1,
            max_retries=0,
        ),
        started_at=datetime.now(UTC),
    )


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
        controls=controls,
    )
    pid = HarborBaselineRunner().start(baseline_run)
    waited, status = os.waitpid(pid, 0)

    assert existing.value.code is PreparationErrorCode.LAUNCH_FAILED
    assert waited == pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert baseline_run.job_path.is_dir()


def test_baseline_launch_rejects_controls_changed_after_the_persisted_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark, executable, config = _benchmark(tmp_path)
    source = tmp_path / "candidate"
    source.mkdir()
    _credentials(monkeypatch)
    controls = HarborExperimentRunner().validate(
        benchmark,
        executable,
        config.relative_to(benchmark),
    )
    run = BaselineRun(
        experiment_id="baseline",
        benchmark_root=benchmark,
        harbor_executable=executable,
        harbor_config=config,
        job_path=benchmark / "jobs/baseline",
        log_path=tmp_path / "baseline.log",
        worktree_path=source,
        initialization_commit="a" * 40,
        controls=controls,
    )
    config.write_text(config.read_text().replace("gpt-5.4-mini", "different-model"))

    with pytest.raises(PreparationFailure) as raised:
        HarborBaselineRunner().start(run)

    assert raised.value.code is PreparationErrorCode.INVALID_HARBOR_CONFIG
    assert not run.job_path.exists()


def test_generalized_harbor_cancel_terminates_and_reaps_the_process_group(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(  # nosec B603
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
    )
    run = _cancel_run(tmp_path)
    try:
        HarborExperimentRunner().cancel(run, process.pid)
        return_code = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert return_code < 0


def test_generalized_harbor_cancel_handles_absent_and_failed_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = HarborExperimentRunner()
    run = _cancel_run(tmp_path)
    runner.cancel(run, None)
    process = subprocess.Popen(  # nosec B603
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
    )
    try:
        run = _cancel_run(tmp_path)

        def missing(process_id: int, signal_number: int) -> None:
            del process_id, signal_number
            raise ProcessLookupError

        monkeypatch.setattr(os, "killpg", missing)
        runner.cancel(run, process.pid)

        def denied(process_id: int, signal_number: int) -> None:
            del process_id, signal_number
            raise PermissionError

        monkeypatch.setattr(os, "killpg", denied)
        with pytest.raises(PreparationFailure) as raised:
            runner.cancel(run, process.pid)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    assert raised.value.code is PreparationErrorCode.LAUNCH_FAILED


def test_generalized_harbor_cancel_skips_a_reused_process_identity(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(  # nosec B603
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
    )
    run = replace(_cancel_run(tmp_path), started_at=datetime.now(UTC) - timedelta(days=1))
    try:
        HarborExperimentRunner().cancel(run, process.pid)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=5)


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
