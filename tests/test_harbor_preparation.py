"""Bounded normalization of Harbor baseline results."""

from pathlib import Path

import pytest

from ofw.preparation import (
    BaselineRun,
    BaselineSummary,
    ExperimentControls,
    PreparationErrorCode,
    PreparationFailure,
)
from ofw.preparation.harbor import HarborBaselineRunner


def _run(tmp_path: Path, job_path: Path) -> BaselineRun:
    return BaselineRun(
        experiment_id="demo",
        benchmark_root=tmp_path,
        harbor_executable=tmp_path / "harbor",
        harbor_config=Path("config.json"),
        job_path=job_path,
        log_path=tmp_path / "baseline.log",
        worktree_path=tmp_path / "worktree",
        initialization_commit="0" * 40,
        controls=ExperimentControls(
            model="openai/gpt-5.4-mini",
            task_ids=(),
            benchmark_config_digest="sha256:" + "0" * 64,
            verifier="itsm-bench",
            environment="itsm-bench",
            concurrency=1,
            max_retries=0,
        ),
    )


def test_harbor_summary_keeps_unsupported_and_errored_rewards_unverified(
    tmp_path: Path,
) -> None:
    job_path = tmp_path / "jobs/demo"
    job_path.mkdir(parents=True)
    (job_path / "result.json").write_text(
        '{"finished_at":"2026-08-27T20:01:02Z","n_total_trials":3}',
        encoding="utf-8",
    )
    trial_payloads = (
        '{"exception_info":null,"verifier_result":{"rewards":{"reward":0.5}}}',
        '{"exception_info":"agent failed","verifier_result":{"rewards":{"reward":0.0}}}',
        '{"exception_info":null,"verifier_result":null}',
    )
    for index, payload in enumerate(trial_payloads):
        trial = job_path / f"task-{index}"
        trial.mkdir()
        (trial / "result.json").write_text(payload, encoding="utf-8")

    summary = HarborBaselineRunner().summarize(_run(tmp_path, job_path))

    assert summary == BaselineSummary(
        terminal_trials=3,
        verifier_passes=0,
        verifier_failures=0,
        unverified_trials=3,
        unsupported_reward_trials=1,
    )


def test_harbor_summary_rejects_oversized_results(tmp_path: Path) -> None:
    job_path = tmp_path / "jobs/demo"
    job_path.mkdir(parents=True)
    (job_path / "result.json").write_text("x" * (9 * 1024 * 1024), encoding="utf-8")

    with pytest.raises(PreparationFailure) as raised:
        HarborBaselineRunner().summarize(_run(tmp_path, job_path))

    assert raised.value.code is PreparationErrorCode.INVALID_BASELINE_RESULT


def test_harbor_summary_rejects_more_results_than_declared_trials(tmp_path: Path) -> None:
    job_path = tmp_path / "jobs/demo"
    job_path.mkdir(parents=True)
    (job_path / "result.json").write_text(
        '{"finished_at":"2026-08-27T20:01:02Z","n_total_trials":1}',
        encoding="utf-8",
    )
    for index in range(2):
        trial = job_path / f"task-{index}"
        trial.mkdir()
        (trial / "result.json").write_text(
            '{"exception_info":null,"verifier_result":{"rewards":{"reward":1.0}}}',
            encoding="utf-8",
        )

    with pytest.raises(PreparationFailure) as raised:
        HarborBaselineRunner().summarize(_run(tmp_path, job_path))

    assert raised.value.code is PreparationErrorCode.INVALID_BASELINE_RESULT
