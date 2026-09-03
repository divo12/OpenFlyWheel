import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ofw.evaluation.outcome import VerifierVerdict
from ofw.evolution.candidate import (
    CandidateBlocker,
    CandidateBlockerCode,
    CandidateExecutionObservation,
    CandidateId,
    CandidateOutcomeReceipt,
    CandidatePhase,
    CandidateStatus,
    candidate_policy_digest,
)
from ofw.evolution.gate import (
    PromotionDecision,
    PromotionReason,
    PromotionStatus,
    decide_promotion,
)
from ofw.preparation.contracts import (
    BaselineConfiguration,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import ExperimentPolicySnapshot, build_experiment_policy

COMMIT = "a" * 40
TREE = "b" * 40


def _policy(
    *,
    max_cost_per_task_usd: float | None = None,
    max_latency_seconds: float | None = None,
) -> ExperimentPolicySnapshot:
    request = PrepareWorkspaceInput(
        experiment_id="experiment-one",
        harness_root=Path("/tmp/accepted"),
        base_ref="HEAD",
        worktree_parent=Path("/tmp/candidates"),
        benchmark_root=Path("/tmp/benchmark"),
        harbor_executable=Path("/tmp/harbor"),
        harbor_config=Path("config.json"),
        expected_task_count=3,
        editable_paths=(Path("prompt.md"),),
        goal="Improve verifier-backed quality.",
        quality_target=1.0,
        max_iterations=3,
        no_improvement_limit=2,
        max_cost_per_task_usd=max_cost_per_task_usd,
        max_latency_seconds=max_latency_seconds,
        max_baseline_seconds=600,
    )
    policy = build_experiment_policy(
        request,
        PreparedGitWorkspace(
            branch_name="ofw/experiment-one",
            worktree_path=Path("/tmp/accepted"),
            base_commit=COMMIT,
            initialization_commit=COMMIT,
            program_path=Path("/tmp/accepted/PROGRAM.md"),
        ),
        BaselineConfiguration(
            model="model",
            task_ids=("task-1", "task-2", "task-3"),
            benchmark_config_digest="sha256:" + "c" * 64,
            verifier="verifier",
            environment="environment",
        ),
    )
    return policy


def _receipt(task_id: str, verdict: VerifierVerdict, suffix: str) -> CandidateOutcomeReceipt:
    return CandidateOutcomeReceipt(
        task_id=task_id,
        trace_id=f"trace-{suffix}",
        score_id=f"score-{suffix}",
        verdict=verdict,
    )


def _run(
    policy: ExperimentPolicySnapshot,
    outcomes: tuple[tuple[str, VerifierVerdict], ...],
    *,
    candidate_id: str | None = "",
    source_commit: str = COMMIT,
    candidate_tree: str | None = TREE,
    candidate_commit: str | None = "",
    experiment_id: str | None = None,
    status: CandidateStatus | None = None,
    receipt_suffixes: tuple[str, ...] | None = None,
    blockers: tuple[CandidateBlocker, ...] = (),
) -> CandidateExecutionObservation:
    suffixes = receipt_suffixes or tuple(str(index) for index in range(len(outcomes)))
    receipts = tuple(
        _receipt(task, verdict, suffixes[index]) for index, (task, verdict) in enumerate(outcomes)
    )
    if candidate_id == "":
        candidate_id = CandidateId.build(
            policy_digest=candidate_policy_digest(policy),
            hypothesis_id="sha256:" + "f" * 64,
            source_commit=source_commit,
            candidate_tree=candidate_tree or "",
            controls_digest=policy.controls_digest,
        ).value
    if candidate_commit == "":
        candidate_commit = "e" * 40 if candidate_tree == TREE else "f" * 40
    return CandidateExecutionObservation(
        status=status or (CandidateStatus.WARNING if blockers else CandidateStatus.SUCCESS),
        summary="complete",
        next_actions=(),
        artifacts=(),
        phase=CandidatePhase.COMPLETE,
        experiment_id=experiment_id or policy.experiment_id,
        hypothesis_id="sha256:" + "f" * 64,
        source_commit=source_commit,
        candidate_id=candidate_id,
        candidate_tree=candidate_tree,
        candidate_commit=candidate_commit,
        session_id=candidate_id,
        outcome_receipts=receipts,
        blockers=blockers,
    )


def _decision(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> PromotionDecision:
    return decide_promotion(policy, accepted, candidate)


@pytest.mark.parametrize(
    ("accepted", "candidate", "status", "reason"),
    (
        (
            ("pass", "fail"),
            ("pass", "fail"),
            PromotionStatus.REJECT,
            PromotionReason.NO_IMPROVEMENT,
        ),
        (
            ("fail", "fail"),
            ("fail", "fail"),
            PromotionStatus.REJECT,
            PromotionReason.NO_IMPROVEMENT,
        ),
        (("fail", "fail"), ("fail", "pass"), PromotionStatus.ACCEPT, PromotionReason.IMPROVEMENT),
        (
            ("pass", "fail"),
            ("fail", "fail"),
            PromotionStatus.REJECT,
            PromotionReason.PASS_REGRESSION,
        ),
        (
            ("pass", "fail"),
            ("abstain", "fail"),
            PromotionStatus.INCONCLUSIVE,
            PromotionReason.ABSTAIN_OUTCOME,
        ),
        (
            ("pass", "fail"),
            ("error", "fail"),
            PromotionStatus.INCONCLUSIVE,
            PromotionReason.ERROR_OUTCOME,
        ),
    ),
)
def test_verdict_matrix(
    accepted: tuple[str, str],
    candidate: tuple[str, str],
    status: PromotionStatus,
    reason: PromotionReason,
) -> None:
    policy = _policy()
    verdicts: dict[str, VerifierVerdict] = {
        name: VerifierVerdict(name) for name in ("pass", "fail", "abstain", "error")
    }
    accepted_run = _run(
        policy,
        (
            ("task-1", verdicts[accepted[0]]),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate_run = _run(
        policy,
        (
            ("task-1", verdicts[candidate[0]]),
            ("task-2", verdicts[candidate[1]]),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
    )
    if candidate[1] in ("abstain", "error"):
        candidate_run = _run(
            policy,
            (("task-1", verdicts[candidate[0]]), ("task-3", VerifierVerdict.FAIL)),
            candidate_tree="c" * 40,
            blockers=(
                CandidateBlocker(
                    task_id="task-2", code=CandidateBlockerCode.UNVERIFIED, subject=candidate[1]
                ),
            ),
        )
    decision = _decision(policy, accepted_run, candidate_run)
    assert decision.status is status
    assert reason in decision.reasons


def test_unsupported_and_incomplete_runs_are_inconclusive() -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(
        policy,
        (("task-1", VerifierVerdict.FAIL), ("task-2", VerifierVerdict.PASS)),
        candidate_tree="c" * 40,
        blockers=(
            CandidateBlocker(
                task_id="task-3", code=CandidateBlockerCode.UNSUPPORTED_REWARD, subject="reward"
            ),
        ),
    )
    decision = _decision(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert decision.reasons == (PromotionReason.UNSUPPORTED_OUTCOME,)


def test_duplicate_receipt_ids_and_terminal_errors_are_inconclusive() -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    duplicate = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.PASS),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
        receipt_suffixes=("same", "same", "last"),
    )
    assert PromotionReason.TASK_SET_MISMATCH in _decision(policy, accepted, duplicate).reasons
    failed = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.PASS),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
        status=CandidateStatus.ERROR,
    )
    assert _decision(policy, accepted, failed).reasons == (PromotionReason.UNVERIFIED_OUTCOME,)


def test_equal_count_with_a_swapped_pass_is_a_regression() -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.PASS),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.PASS),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
    )
    decision = _decision(policy, accepted, candidate)
    assert decision.status is PromotionStatus.REJECT
    assert decision.reasons == (PromotionReason.PASS_REGRESSION,)


@pytest.mark.parametrize(
    "outcomes",
    (
        (("task-1", VerifierVerdict.FAIL), ("task-2", VerifierVerdict.FAIL)),
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-1", VerifierVerdict.PASS),
            ("task-2", VerifierVerdict.FAIL),
        ),
        (
            ("task-2", VerifierVerdict.FAIL),
            ("task-1", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
            ("task-4", VerifierVerdict.PASS),
        ),
    ),
)
def test_task_set_must_be_exact_and_ordered(
    outcomes: tuple[tuple[str, VerifierVerdict], ...],
) -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(policy, outcomes, candidate_tree="c" * 40)
    decision = _decision(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert PromotionReason.TASK_SET_MISMATCH in decision.reasons


@pytest.mark.parametrize(
    "field",
    ("experiment_id", "source_commit", "candidate_id", "candidate_tree", "candidate_commit"),
)
def test_identity_mismatch_is_inconclusive(field: str) -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.PASS),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
    )
    if field == "experiment_id":
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            candidate_tree="c" * 40,
            experiment_id="other-experiment",
        )
    elif field == "candidate_id":
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            candidate_id=None,
        )
    elif field == "candidate_tree":
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            candidate_id="sha256:" + "1" * 64,
            candidate_tree=None,
        )
    elif field == "candidate_commit":
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            candidate_tree="c" * 40,
            candidate_commit=None,
        )
    else:
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            source_commit="0" * 40,
        )
    decision = _decision(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert PromotionReason.IDENTITY_MISMATCH in decision.reasons


def test_configured_metrics_fail_closed_and_absent_limits_do_not_block() -> None:
    for cost, latency, reason in (
        (1.0, None, PromotionReason.MISSING_COST),
        (None, 1.0, PromotionReason.MISSING_LATENCY),
    ):
        policy = _policy(max_cost_per_task_usd=cost, max_latency_seconds=latency)
        accepted = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.FAIL),
                ("task-3", VerifierVerdict.FAIL),
            ),
        )
        candidate = _run(
            policy,
            (
                ("task-1", VerifierVerdict.FAIL),
                ("task-2", VerifierVerdict.PASS),
                ("task-3", VerifierVerdict.FAIL),
            ),
            candidate_tree="c" * 40,
        )
        decision = _decision(policy, accepted, candidate)
        assert decision.status is PromotionStatus.INCONCLUSIVE
        assert reason in decision.reasons


def test_reasons_are_stably_ordered_and_decision_is_immutable() -> None:
    policy = _policy(max_cost_per_task_usd=1.0, max_latency_seconds=1.0)
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(
        policy,
        (("task-1", VerifierVerdict.FAIL), ("task-2", VerifierVerdict.PASS)),
        candidate_tree="c" * 40,
        blockers=(
            CandidateBlocker(
                task_id="task-3", code=CandidateBlockerCode.UNVERIFIED, subject="missing"
            ),
        ),
    )
    first = _decision(policy, accepted, candidate)
    second = _decision(policy, accepted, candidate)
    assert first == second
    assert first.reasons == tuple(reason for reason in PromotionReason if reason in first.reasons)
    with pytest.raises(FrozenInstanceError):
        first.status = PromotionStatus.ACCEPT  # type: ignore[misc]


def test_fixed_golden_decision_digest() -> None:
    policy = _policy()
    accepted = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.FAIL),
            ("task-3", VerifierVerdict.FAIL),
        ),
    )
    candidate = _run(
        policy,
        (
            ("task-1", VerifierVerdict.FAIL),
            ("task-2", VerifierVerdict.PASS),
            ("task-3", VerifierVerdict.FAIL),
        ),
        candidate_tree="c" * 40,
    )
    decision = _decision(policy, accepted, candidate)
    assert (
        decision.decision_id
        == "sha256:" + hashlib.sha256(decision.canonical_json.encode()).hexdigest()
    )
    assert (
        decision.decision_id
        == "sha256:771ef0e4b9a2b1632dda135c7219fe3d9e1f0fe01463a6d545e3d5e56dfbfe14"
    )
