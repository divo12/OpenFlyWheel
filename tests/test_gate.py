import hashlib
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ofw.evaluation.outcome import (
    EvaluatedRunBlocker,
    EvaluatedRunReceipt,
    EvaluatedTaskReceipt,
    RunSide,
    VerifierVerdict,
)
from ofw.evolution.candidate import candidate_policy_digest
from ofw.evolution.gate import PromotionReason, PromotionStatus, decide_promotion
from ofw.preparation.contracts import (
    BaselineConfiguration,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)
from ofw.preparation.policy import ExperimentPolicySnapshot, build_experiment_policy

COMMIT = "a" * 40
TREE = "b" * 40


def _policy(
    *, max_cost_per_task_usd: float | None = None, max_latency_seconds: float | None = None
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
    return build_experiment_policy(
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


def _task(
    task_id: str,
    verdict: VerifierVerdict,
    *,
    cost_usd: float | None = 0.25,
    latency_seconds: float | None = 1.5,
    verifier_id: str = "verifier@checksum",
) -> EvaluatedTaskReceipt:
    score = (
        1.0 if verdict is VerifierVerdict.PASS else 0.0 if verdict is VerifierVerdict.FAIL else None
    )
    return EvaluatedTaskReceipt(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        score_id=f"score-{task_id}",
        verdict=verdict,
        verifier_id=verifier_id,
        normalized_score=score,
        cost_usd=cost_usd,
        latency_seconds=latency_seconds,
    )


def _blocker(task_id: str, code: str = "unverified") -> EvaluatedRunBlocker:
    return EvaluatedRunBlocker(task_id=task_id, code=code, subject="evidence")


def _receipt(
    policy: ExperimentPolicySnapshot,
    outcomes: tuple[EvaluatedTaskReceipt, ...],
    blockers: tuple[EvaluatedRunBlocker, ...] = (),
    *,
    side: RunSide = RunSide.CANDIDATE,
    run_id: str = "candidate-run",
    evaluated_commit: str = COMMIT,
    evaluated_tree: str = TREE,
    policy_digest: str | None = None,
    controls_digest: str | None = None,
) -> EvaluatedRunReceipt:
    return EvaluatedRunReceipt.build(
        run_id=run_id,
        side=side,
        policy_digest=policy_digest or candidate_policy_digest(policy),
        controls_digest=controls_digest or policy.controls_digest,
        evaluated_commit=evaluated_commit,
        evaluated_tree=evaluated_tree,
        task_ids=policy.task_ids,
        outcome_receipts=outcomes,
        blockers=blockers,
    )


def _runs(
    policy: ExperimentPolicySnapshot,
    accepted_verdicts: tuple[VerifierVerdict, ...],
    candidate_verdicts: tuple[VerifierVerdict, ...],
) -> tuple[EvaluatedRunReceipt, EvaluatedRunReceipt]:
    accepted = _receipt(
        policy,
        tuple(
            _task(task_id, verdict)
            for task_id, verdict in zip(policy.task_ids, accepted_verdicts, strict=True)
        ),
        side=RunSide.ACCEPTED,
        run_id="accepted-run",
    )
    candidate = _receipt(
        policy,
        tuple(
            _task(task_id, verdict)
            for task_id, verdict in zip(policy.task_ids, candidate_verdicts, strict=True)
        ),
        run_id="candidate-run",
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    return accepted, candidate


def _rehashed(receipt: EvaluatedRunReceipt) -> EvaluatedRunReceipt:
    return EvaluatedRunReceipt.model_construct(
        receipt_id=receipt.recomputed_id(),
        run_id=receipt.run_id,
        side=receipt.side,
        policy_digest=receipt.policy_digest,
        controls_digest=receipt.controls_digest,
        evaluated_commit=receipt.evaluated_commit,
        evaluated_tree=receipt.evaluated_tree,
        task_ids=receipt.task_ids,
        outcome_receipts=receipt.outcome_receipts,
        blockers=receipt.blockers,
    )


def test_verdict_transition_matrix() -> None:
    policy = _policy()
    cases = (
        (
            (VerifierVerdict.PASS, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            (VerifierVerdict.PASS, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            PromotionStatus.REJECT,
            PromotionReason.NO_IMPROVEMENT,
        ),
        (
            (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            (VerifierVerdict.PASS, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            PromotionStatus.ACCEPT,
            PromotionReason.IMPROVEMENT,
        ),
        (
            (VerifierVerdict.PASS, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
            PromotionStatus.REJECT,
            PromotionReason.PASS_REGRESSION,
        ),
        (
            (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            (VerifierVerdict.ABSTAIN, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            PromotionStatus.INCONCLUSIVE,
            PromotionReason.ABSTAIN_OUTCOME,
        ),
        (
            (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            (VerifierVerdict.ERROR, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
            PromotionStatus.INCONCLUSIVE,
            PromotionReason.ERROR_OUTCOME,
        ),
    )
    for accepted_verdicts, candidate_verdicts, status, reason in cases:
        accepted, candidate = _runs(policy, accepted_verdicts, candidate_verdicts)
        decision = decide_promotion(policy, accepted, candidate)
        assert decision.status is status
        assert reason in decision.reasons


def test_unsupported_and_unverified_blockers_are_inconclusive() -> None:
    policy = _policy()
    accepted = _receipt(
        policy,
        tuple(_task(task_id, VerifierVerdict.FAIL) for task_id in policy.task_ids),
        side=RunSide.ACCEPTED,
        run_id="accepted-run",
    )
    candidate = _receipt(
        policy,
        (_task("task-1", VerifierVerdict.FAIL), _task("task-2", VerifierVerdict.PASS)),
        (_blocker("task-3", "unsupported_reward"),),
        run_id="candidate-run",
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    decision = decide_promotion(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert decision.reasons == (PromotionReason.UNSUPPORTED_OUTCOME,)
    all_blocked = _receipt(
        policy,
        (),
        tuple(_blocker(task_id) for task_id in policy.task_ids),
        run_id="candidate-run",
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    assert decide_promotion(policy, accepted, all_blocked).status is PromotionStatus.INCONCLUSIVE


def test_interleaved_outcomes_and_blockers_follow_each_partition_order() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    interleaved = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=candidate.task_ids,
        outcome_receipts=(candidate.outcome_receipts[0], candidate.outcome_receipts[2]),
        blockers=(_blocker("task-2"),),
    )
    decision = decide_promotion(policy, accepted, _rehashed(interleaved))
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert decision.reasons == (PromotionReason.UNVERIFIED_OUTCOME,)


@pytest.mark.parametrize(
    "field", ("policy_digest", "controls_digest", "evaluated_commit", "evaluated_tree")
)
def test_identity_mismatch_is_inconclusive(field: str) -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    if field == "policy_digest":
        candidate = _receipt(
            policy,
            candidate.outcome_receipts,
            run_id=candidate.run_id,
            evaluated_commit="c" * 40,
            evaluated_tree="d" * 40,
            policy_digest="sha256:" + "e" * 64,
        )
    elif field == "controls_digest":
        candidate = _receipt(
            policy,
            candidate.outcome_receipts,
            run_id=candidate.run_id,
            evaluated_commit="c" * 40,
            evaluated_tree="d" * 40,
            controls_digest="sha256:" + "e" * 64,
        )
    elif field == "evaluated_commit":
        candidate = _receipt(
            policy,
            candidate.outcome_receipts,
            run_id=candidate.run_id,
            evaluated_commit=COMMIT,
            evaluated_tree="d" * 40,
        )
    else:
        candidate = _receipt(
            policy,
            candidate.outcome_receipts,
            run_id=candidate.run_id,
            evaluated_commit="c" * 40,
            evaluated_tree=TREE,
        )
    decision = decide_promotion(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert PromotionReason.IDENTITY_MISMATCH in decision.reasons


def test_wrong_sides_and_run_ids_are_inconclusive() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    wrong_side = _receipt(
        policy,
        candidate.outcome_receipts,
        side=RunSide.ACCEPTED,
        run_id=candidate.run_id,
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    same_run = _receipt(
        policy,
        candidate.outcome_receipts,
        run_id=accepted.run_id,
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    assert (
        PromotionReason.IDENTITY_MISMATCH in decide_promotion(policy, accepted, wrong_side).reasons
    )
    assert PromotionReason.IDENTITY_MISMATCH in decide_promotion(policy, accepted, same_run).reasons


@pytest.mark.parametrize(
    "task_ids",
    (
        ("task-1", "task-2"),
        ("task-2", "task-1", "task-3"),
        ("task-1", "task-2", "task-3", "task-4"),
    ),
)
def test_task_set_must_match_policy_exactly(task_ids: tuple[str, ...]) -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    outcomes = tuple(_task(task_id, VerifierVerdict.FAIL) for task_id in task_ids)
    tampered = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=task_ids,
        outcome_receipts=outcomes,
        blockers=candidate.blockers,
    )
    decision = decide_promotion(policy, accepted, tampered)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert PromotionReason.TASK_PARTITION_MISMATCH in decision.reasons


def test_duplicate_receipt_id_and_tampered_receipt_hash_are_inconclusive() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    duplicate = EvaluatedTaskReceipt.model_construct(
        task_id=candidate.outcome_receipts[0].task_id,
        trace_id=candidate.outcome_receipts[0].trace_id,
        score_id=candidate.outcome_receipts[1].score_id,
        verdict=candidate.outcome_receipts[0].verdict,
        verifier_id=candidate.outcome_receipts[0].verifier_id,
        normalized_score=candidate.outcome_receipts[0].normalized_score,
        cost_usd=candidate.outcome_receipts[0].cost_usd,
        latency_seconds=candidate.outcome_receipts[0].latency_seconds,
    )
    tampered = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=candidate.task_ids,
        outcome_receipts=(duplicate,) + candidate.outcome_receipts[1:],
        blockers=candidate.blockers,
    )
    assert PromotionReason.RECEIPT_MISMATCH in decide_promotion(policy, accepted, tampered).reasons
    duplicate_partition = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=candidate.task_ids,
        outcome_receipts=(
            candidate.outcome_receipts[0],
            candidate.outcome_receipts[0],
            candidate.outcome_receipts[2],
        ),
        blockers=(),
    )
    assert (
        PromotionReason.TASK_PARTITION_MISMATCH
        in decide_promotion(policy, accepted, duplicate_partition).reasons
    )


def test_receipts_must_name_the_configured_verifier() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    invalid = _receipt(
        policy,
        (
            _task("task-1", VerifierVerdict.FAIL, verifier_id="other-verifier"),
            candidate.outcome_receipts[1],
            candidate.outcome_receipts[2],
        ),
        run_id=candidate.run_id,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
    )
    assert PromotionReason.RECEIPT_MISMATCH in decide_promotion(policy, accepted, invalid).reasons


@pytest.mark.parametrize(
    ("cost", "latency", "reason"),
    ((None, 1.5, PromotionReason.MISSING_COST), (0.25, None, PromotionReason.MISSING_LATENCY)),
)
def test_configured_metrics_require_explicit_measurements(
    cost: float | None, latency: float | None, reason: PromotionReason
) -> None:
    policy = _policy(
        max_cost_per_task_usd=1.0 if cost is None else None,
        max_latency_seconds=1.0 if latency is None else None,
    )
    accepted = _receipt(
        policy,
        (
            _task("task-1", VerifierVerdict.FAIL, cost_usd=cost, latency_seconds=latency),
            _task("task-2", VerifierVerdict.FAIL, cost_usd=cost, latency_seconds=latency),
            _task("task-3", VerifierVerdict.FAIL, cost_usd=cost, latency_seconds=latency),
        ),
        side=RunSide.ACCEPTED,
        run_id="accepted-run",
    )
    candidate = _receipt(
        policy,
        (
            _task("task-1", VerifierVerdict.FAIL, cost_usd=cost, latency_seconds=latency),
            _task("task-2", VerifierVerdict.PASS, cost_usd=cost, latency_seconds=latency),
            _task("task-3", VerifierVerdict.FAIL, cost_usd=cost, latency_seconds=latency),
        ),
        run_id="candidate-run",
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    decision = decide_promotion(policy, accepted, candidate)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert reason in decision.reasons


@pytest.mark.parametrize(
    ("field", "limit", "reason"),
    (
        ("cost_usd", 1.0, PromotionReason.COST_LIMIT_EXCEEDED),
        ("latency_seconds", 1.0, PromotionReason.LATENCY_LIMIT_EXCEEDED),
    ),
)
def test_candidate_metric_violation_wins_over_missing_accepted_metric(
    field: str, limit: float, reason: PromotionReason
) -> None:
    policy = _policy(
        max_cost_per_task_usd=limit if field == "cost_usd" else None,
        max_latency_seconds=limit if field == "latency_seconds" else None,
    )
    accepted = _receipt(
        policy,
        tuple(
            _task(task_id, VerifierVerdict.FAIL, cost_usd=None, latency_seconds=None)
            for task_id in policy.task_ids
        ),
        side=RunSide.ACCEPTED,
        run_id="accepted-run",
    )
    candidate = _receipt(
        policy,
        tuple(
            _task(
                task_id,
                verdict,
                cost_usd=limit + 0.01 if field == "cost_usd" else 0.25,
                latency_seconds=limit + 0.01 if field == "latency_seconds" else 1.5,
            )
            for task_id, verdict in zip(
                policy.task_ids,
                (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
                strict=True,
            )
        ),
        run_id="candidate-run",
        evaluated_commit="c" * 40,
        evaluated_tree="d" * 40,
    )
    decision = decide_promotion(policy, accepted, candidate)
    assert decision.status is PromotionStatus.REJECT
    assert reason in decision.reasons


def test_incomplete_run_metrics_are_not_reported_as_totals() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    incomplete = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=candidate.task_ids,
        outcome_receipts=(candidate.outcome_receipts[0],),
        blockers=(_blocker("task-2"), _blocker("task-3")),
    )
    decision = decide_promotion(policy, accepted, _rehashed(incomplete))
    assert decision.candidate_cost_usd is None
    assert decision.candidate_latency_seconds is None


@pytest.mark.parametrize(
    ("field", "limit", "reason"),
    (
        ("cost_usd", 1.0, PromotionReason.COST_LIMIT_EXCEEDED),
        ("latency_seconds", 1.0, PromotionReason.LATENCY_LIMIT_EXCEEDED),
    ),
)
def test_candidate_metric_limits_are_inclusive_and_excess_rejects(
    field: str, limit: float, reason: PromotionReason
) -> None:
    policy = _policy(
        max_cost_per_task_usd=limit if field == "cost_usd" else None,
        max_latency_seconds=limit if field == "latency_seconds" else None,
    )
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    candidate_verdicts = (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL)
    boundary = tuple(
        _task(
            task_id,
            verdict,
            cost_usd=limit if field == "cost_usd" else 0.25,
            latency_seconds=limit if field == "latency_seconds" else 1.5,
        )
        for task_id, verdict in zip(policy.task_ids, candidate_verdicts, strict=True)
    )
    boundary_candidate = _receipt(
        policy, boundary, run_id="candidate-run", evaluated_commit="c" * 40, evaluated_tree="d" * 40
    )
    assert decide_promotion(policy, accepted, boundary_candidate).status is PromotionStatus.ACCEPT
    excess = tuple(
        _task(
            task_id,
            verdict,
            cost_usd=limit + 0.01 if field == "cost_usd" else 0.25,
            latency_seconds=limit + 0.01 if field == "latency_seconds" else 1.5,
        )
        for task_id, verdict in zip(policy.task_ids, candidate_verdicts, strict=True)
    )
    excess_candidate = _receipt(
        policy, excess, run_id="candidate-run", evaluated_commit="c" * 40, evaluated_tree="d" * 40
    )
    decision = decide_promotion(policy, accepted, excess_candidate)
    assert decision.status is PromotionStatus.REJECT
    assert reason in decision.reasons


def test_non_finite_tampered_metric_is_inconclusive() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    task = EvaluatedTaskReceipt.model_construct(
        task_id=candidate.outcome_receipts[1].task_id,
        trace_id=candidate.outcome_receipts[1].trace_id,
        score_id=candidate.outcome_receipts[1].score_id,
        verdict=candidate.outcome_receipts[1].verdict,
        verifier_id=candidate.outcome_receipts[1].verifier_id,
        normalized_score=math.nan,
        cost_usd=candidate.outcome_receipts[1].cost_usd,
        latency_seconds=candidate.outcome_receipts[1].latency_seconds,
    )
    tampered = EvaluatedRunReceipt.model_construct(
        receipt_id=candidate.receipt_id,
        run_id=candidate.run_id,
        side=candidate.side,
        policy_digest=candidate.policy_digest,
        controls_digest=candidate.controls_digest,
        evaluated_commit=candidate.evaluated_commit,
        evaluated_tree=candidate.evaluated_tree,
        task_ids=candidate.task_ids,
        outcome_receipts=(candidate.outcome_receipts[0], task, candidate.outcome_receipts[2]),
        blockers=candidate.blockers,
    )
    tampered = _rehashed(tampered)
    decision = decide_promotion(policy, accepted, tampered)
    assert decision.status is PromotionStatus.INCONCLUSIVE
    assert PromotionReason.RECEIPT_MISMATCH in decision.reasons


def test_decision_is_immutable_deterministic_and_golden() -> None:
    policy = _policy()
    accepted, candidate = _runs(
        policy,
        (VerifierVerdict.FAIL, VerifierVerdict.FAIL, VerifierVerdict.FAIL),
        (VerifierVerdict.FAIL, VerifierVerdict.PASS, VerifierVerdict.FAIL),
    )
    first = decide_promotion(policy, accepted, candidate)
    second = decide_promotion(policy, accepted, candidate)
    assert first == second
    assert (
        first.decision_id == "sha256:" + hashlib.sha256(first.canonical_json.encode()).hexdigest()
    )
    assert (
        first.decision_id
        == "sha256:e069074fc405ed919291bc16626a3c82865d8bb9a43bd5b8468ee1858529792f"
    )
    with pytest.raises(FrozenInstanceError):
        first.status = PromotionStatus.ACCEPT  # type: ignore[misc]
