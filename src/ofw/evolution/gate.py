"""Pure deterministic promotion gate over evaluated run receipts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum

from ofw.evaluation.outcome import (
    EvaluatedRunReceipt,
    EvaluatedTaskReceipt,
    RunSide,
    VerifierVerdict,
)
from ofw.evolution.candidate import candidate_policy_digest
from ofw.preparation.policy import ExperimentPolicySnapshot


class PromotionStatus(StrEnum):
    ACCEPT = "accept"
    ACCEPTED = "accept"
    REJECT = "reject"
    REJECTED = "reject"
    INCONCLUSIVE = "inconclusive"


class PromotionReason(StrEnum):
    IDENTITY_MISMATCH = "identity_mismatch"
    POLICY_MISMATCH = "identity_mismatch"
    CONTROLS_MISMATCH = "identity_mismatch"
    GIT_IDENTITY_MISMATCH = "identity_mismatch"
    RECEIPT_MISMATCH = "receipt_mismatch"
    TASK_PARTITION_MISMATCH = "task_partition_mismatch"
    TASK_MISMATCH = "task_partition_mismatch"
    UNSUPPORTED_OUTCOME = "unsupported_outcome"
    ERROR_OUTCOME = "error_outcome"
    ABSTAIN_OUTCOME = "abstain_outcome"
    UNVERIFIED_OUTCOME = "unverified_outcome"
    MISSING_COST = "missing_cost"
    MISSING_LATENCY = "missing_latency"
    COST_LIMIT_EXCEEDED = "cost_limit_exceeded"
    LATENCY_LIMIT_EXCEEDED = "latency_limit_exceeded"
    PASS_REGRESSION = "pass_regression"
    QUALITY_REGRESSION = "quality_regression"
    NO_IMPROVEMENT = "no_improvement"
    IMPROVEMENT = "improvement"


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision_id: str
    policy_digest: str
    accepted_run_id: str
    candidate_run_id: str
    status: PromotionStatus
    reasons: tuple[PromotionReason, ...]
    task_ids: tuple[str, ...]
    accepted_passes: tuple[str, ...]
    candidate_passes: tuple[str, ...]
    accepted_quality: float
    candidate_quality: float
    accepted_cost_usd: float | None
    candidate_cost_usd: float | None
    accepted_latency_seconds: float | None
    candidate_latency_seconds: float | None
    canonical_json: str

    def recomputed_id(self) -> str:
        """Return the identity of this immutable canonical gate decision."""
        return (
            f"sha256:{hashlib.sha256(self.canonical_json.encode('utf-8')).hexdigest()}"
        )


def decide_promotion(
    policy: ExperimentPolicySnapshot,
    accepted_run: EvaluatedRunReceipt,
    candidate_run: EvaluatedRunReceipt,
) -> PromotionDecision:
    identity_reasons = _identity_reasons(policy, accepted_run, candidate_run)
    partition_reasons = _partition_reasons(policy, accepted_run, candidate_run)
    receipt_reasons = _receipt_reasons(policy, accepted_run, candidate_run)
    reasons = _ordered_reasons(identity_reasons + partition_reasons + receipt_reasons)
    if reasons:
        return _decision(
            policy, accepted_run, candidate_run, PromotionStatus.INCONCLUSIVE, reasons
        )

    outcome_reasons = _outcome_reasons(accepted_run, candidate_run)
    metric_reasons = _metric_reasons(policy, accepted_run, candidate_run)
    reasons = _ordered_reasons(outcome_reasons + metric_reasons)
    if reasons:
        status = (
            PromotionStatus.REJECT
            if _has_limit_violation(reasons)
            else PromotionStatus.INCONCLUSIVE
        )
        return _decision(policy, accepted_run, candidate_run, status, reasons)

    return _decide_quality(policy, accepted_run, candidate_run)


def _decide_quality(
    policy: ExperimentPolicySnapshot,
    accepted_run: EvaluatedRunReceipt,
    candidate_run: EvaluatedRunReceipt,
) -> PromotionDecision:

    accepted_passes = _passes(accepted_run)
    candidate_passes = _passes(candidate_run)
    if not set(accepted_passes).issubset(candidate_passes):
        return _decision(
            policy,
            accepted_run,
            candidate_run,
            PromotionStatus.REJECT,
            (PromotionReason.PASS_REGRESSION,),
        )
    if not _has_exact_improvement(accepted_run, candidate_run):
        return _decision(
            policy,
            accepted_run,
            candidate_run,
            PromotionStatus.REJECT,
            (PromotionReason.NO_IMPROVEMENT,),
        )
    return _decision(
        policy,
        accepted_run,
        candidate_run,
        PromotionStatus.ACCEPT,
        (PromotionReason.IMPROVEMENT,),
    )


def _has_exact_improvement(
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return any(_is_exact_improvement(accepted, item) for item in candidate.outcome_receipts)


def _is_exact_improvement(
    accepted: EvaluatedRunReceipt,
    candidate_task: EvaluatedTaskReceipt,
) -> bool:
    accepted_task = next(
        (item for item in accepted.outcome_receipts if item.task_id == candidate_task.task_id),
        None,
    )
    return (
        accepted_task is not None
        and accepted_task.verdict is not VerifierVerdict.PASS
        and candidate_task.verdict is VerifierVerdict.PASS
    )


def _identity_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    expected_policy = candidate_policy_digest(policy)
    mismatch = _authority_identity_mismatch(
        expected_policy, policy, accepted, candidate
    )
    if not mismatch:
        mismatch = _run_identity_mismatch(accepted, candidate)
    return (PromotionReason.IDENTITY_MISMATCH,) if mismatch else ()


def _authority_identity_mismatch(
    expected_policy: str,
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return (
        _policy_identity_mismatch(expected_policy, accepted, candidate)
        or _controls_identity_mismatch(policy, accepted, candidate)
        or _side_identity_mismatch(accepted, candidate)
    )


def _run_identity_mismatch(
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return _same_run(accepted, candidate) or _same_git_identity(accepted, candidate)


def _policy_identity_mismatch(
    expected_policy: str,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return (
        accepted.policy_digest != expected_policy
        or candidate.policy_digest != expected_policy
    )


def _controls_identity_mismatch(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return (
        accepted.controls_digest != policy.controls_digest
        or candidate.controls_digest != policy.controls_digest
    )


def _side_identity_mismatch(
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return (
        accepted.side is not RunSide.ACCEPTED or candidate.side is not RunSide.CANDIDATE
    )


def _same_run(accepted: EvaluatedRunReceipt, candidate: EvaluatedRunReceipt) -> bool:
    return accepted.run_id == candidate.run_id


def _same_git_identity(
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> bool:
    return (
        accepted.evaluated_commit == candidate.evaluated_commit
        or accepted.evaluated_tree == candidate.evaluated_tree
    )


def _partition_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    if accepted.task_ids != policy.task_ids or candidate.task_ids != policy.task_ids:
        return (PromotionReason.TASK_PARTITION_MISMATCH,)
    if not _partition_is_valid(accepted) or not _partition_is_valid(candidate):
        return (PromotionReason.TASK_PARTITION_MISMATCH,)
    return ()


def _partition_is_valid(receipt: EvaluatedRunReceipt) -> bool:
    task_ids, outcome_ids, blocker_ids = _partition_values(receipt)
    result_ids = outcome_ids + blocker_ids
    if not _has_exact_partition(result_ids, task_ids):
        return False
    return _is_ordered(outcome_ids, task_ids) and _is_ordered(blocker_ids, task_ids)


def _partition_values(
    receipt: EvaluatedRunReceipt,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(str(task_id) for task_id in receipt.task_ids),
        tuple(item.task_id for item in receipt.outcome_receipts),
        tuple(item.task_id for item in receipt.blockers),
    )


def _has_exact_partition(
    result_ids: tuple[str, ...], task_ids: tuple[str, ...]
) -> bool:
    return (
        len(result_ids) == len(task_ids)
        and len(set(result_ids)) == len(result_ids)
        and set(result_ids) == set(task_ids)
    )


def _is_ordered(result_ids: tuple[str, ...], task_ids: tuple[str, ...]) -> bool:
    return result_ids == tuple(task_id for task_id in task_ids if task_id in result_ids)


def _receipt_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    return _one_receipt_reasons(policy, accepted) + _one_receipt_reasons(
        policy, candidate
    )


def _one_receipt_reasons(
    policy: ExperimentPolicySnapshot,
    receipt: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    if receipt.receipt_id != receipt.recomputed_id() or not _task_receipt_ids_unique(
        receipt
    ):
        return (PromotionReason.RECEIPT_MISMATCH,)
    if any(
        not _task_receipt_is_valid(task, policy.verifier)
        for task in receipt.outcome_receipts
    ):
        return (PromotionReason.RECEIPT_MISMATCH,)
    return ()


def _task_receipt_ids_unique(receipt: EvaluatedRunReceipt) -> bool:
    score_ids = tuple(item.score_id for item in receipt.outcome_receipts)
    trace_ids = tuple(item.trace_id for item in receipt.outcome_receipts)
    return len(score_ids) == len(set(score_ids)) and len(trace_ids) == len(
        set(trace_ids)
    )


def _task_receipt_is_valid(task: EvaluatedTaskReceipt, verifier: str) -> bool:
    return (
        _verifier_matches(task.verifier_id, verifier)
        and _score_is_valid(task)
        and _metrics_are_valid(task)
    )


def _verifier_matches(verifier_id: str, verifier: str) -> bool:
    return verifier_id == verifier or verifier_id.startswith(verifier + "@")


def _score_is_valid(task: EvaluatedTaskReceipt) -> bool:
    expected = (
        1.0
        if task.verdict is VerifierVerdict.PASS
        else 0.0
        if task.verdict is VerifierVerdict.FAIL
        else None
    )
    return task.normalized_score == expected


def _metrics_are_valid(task: EvaluatedTaskReceipt) -> bool:
    return (
        _metric_is_valid(task.normalized_score, 0.0, 1.0)
        and _metric_is_valid(task.cost_usd, 0.0, 1_000_000.0)
        and _metric_is_valid(task.latency_seconds, 0.0, 172800.0)
    )


def _metric_is_valid(value: float | None, minimum: float, maximum: float) -> bool:
    return value is None or math.isfinite(value) and minimum <= value <= maximum


def _outcome_reasons(
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    return _run_outcome_reasons(accepted) + _run_outcome_reasons(candidate)


def _run_outcome_reasons(receipt: EvaluatedRunReceipt) -> tuple[PromotionReason, ...]:
    reasons: list[PromotionReason] = []
    for blocker in receipt.blockers:
        reasons.append(_blocker_reason(blocker.code))
    for task in receipt.outcome_receipts:
        if task.verdict is VerifierVerdict.ERROR:
            reasons.append(PromotionReason.ERROR_OUTCOME)
        elif task.verdict is VerifierVerdict.ABSTAIN:
            reasons.append(PromotionReason.ABSTAIN_OUTCOME)
    return tuple(reasons)


def _blocker_reason(code: str) -> PromotionReason:
    return (
        PromotionReason.UNSUPPORTED_OUTCOME
        if "unsupported" in code
        else PromotionReason.UNVERIFIED_OUTCOME
    )


def _metric_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> tuple[PromotionReason, ...]:
    reasons: list[PromotionReason] = []
    if policy.max_cost_per_task_usd is not None:
        reason = _cost_reason(policy.max_cost_per_task_usd, accepted, candidate)
        if reason is not None:
            reasons.append(reason)
    if policy.max_latency_seconds is not None:
        reason = _latency_reason(policy.max_latency_seconds, accepted, candidate)
        if reason is not None:
            reasons.append(reason)
    return tuple(reasons)


def _cost_reason(
    limit: float,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> PromotionReason | None:
    if _exceeds_cost(candidate, limit):
        return PromotionReason.COST_LIMIT_EXCEEDED
    if _has_missing_cost(accepted) or _has_missing_cost(candidate):
        return PromotionReason.MISSING_COST
    return None


def _latency_reason(
    limit: float,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
) -> PromotionReason | None:
    if _exceeds_latency(candidate, limit):
        return PromotionReason.LATENCY_LIMIT_EXCEEDED
    if _has_missing_latency(accepted) or _has_missing_latency(candidate):
        return PromotionReason.MISSING_LATENCY
    return None


def _has_limit_violation(reasons: tuple[PromotionReason, ...]) -> bool:
    return (
        PromotionReason.COST_LIMIT_EXCEEDED in reasons
        or PromotionReason.LATENCY_LIMIT_EXCEEDED in reasons
    )


def _has_missing_cost(receipt: EvaluatedRunReceipt) -> bool:
    return any(item.cost_usd is None for item in receipt.outcome_receipts)


def _has_missing_latency(receipt: EvaluatedRunReceipt) -> bool:
    return any(item.latency_seconds is None for item in receipt.outcome_receipts)


def _exceeds_cost(receipt: EvaluatedRunReceipt, limit: float) -> bool:
    return any(
        item.cost_usd is not None and item.cost_usd > limit
        for item in receipt.outcome_receipts
    )


def _exceeds_latency(receipt: EvaluatedRunReceipt, limit: float) -> bool:
    return any(
        item.latency_seconds is not None and item.latency_seconds > limit
        for item in receipt.outcome_receipts
    )


def _decision(
    policy: ExperimentPolicySnapshot,
    accepted: EvaluatedRunReceipt,
    candidate: EvaluatedRunReceipt,
    status: PromotionStatus,
    reasons: tuple[PromotionReason, ...],
) -> PromotionDecision:
    accepted_passes = _passes(accepted)
    candidate_passes = _passes(candidate)
    accepted_cost = _total_cost(accepted)
    candidate_cost = _total_cost(candidate)
    accepted_latency = _total_latency(accepted)
    candidate_latency = _total_latency(candidate)
    accepted_quality = _quality(accepted)
    candidate_quality = _quality(candidate)
    canonical_json = json.dumps(
        (
            ("policy_digest", candidate_policy_digest(policy)),
            ("controls_digest", policy.controls_digest),
            ("accepted_receipt", accepted.model_dump_json()),
            ("candidate_receipt", candidate.model_dump_json()),
            (
                "metrics",
                (
                    ("accepted_quality", accepted_quality),
                    ("candidate_quality", candidate_quality),
                    ("accepted_cost_usd", accepted_cost),
                    ("candidate_cost_usd", candidate_cost),
                    ("accepted_latency_seconds", accepted_latency),
                    ("candidate_latency_seconds", candidate_latency),
                ),
            ),
            ("status", status.value),
            ("reasons", tuple(reason.value for reason in reasons)),
        ),
        separators=(",", ":"),
    )
    decision_id = f"sha256:{hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()}"
    return PromotionDecision(
        decision_id=decision_id,
        policy_digest=candidate_policy_digest(policy),
        accepted_run_id=accepted.run_id,
        candidate_run_id=candidate.run_id,
        status=status,
        reasons=reasons,
        task_ids=policy.task_ids,
        accepted_passes=accepted_passes,
        candidate_passes=candidate_passes,
        accepted_quality=accepted_quality,
        candidate_quality=candidate_quality,
        accepted_cost_usd=accepted_cost,
        candidate_cost_usd=candidate_cost,
        accepted_latency_seconds=accepted_latency,
        candidate_latency_seconds=candidate_latency,
        canonical_json=canonical_json,
    )


def _passes(receipt: EvaluatedRunReceipt) -> tuple[str, ...]:
    return tuple(
        item.task_id
        for item in receipt.outcome_receipts
        if item.verdict is VerifierVerdict.PASS
    )


def _quality(receipt: EvaluatedRunReceipt) -> float:
    return sum(item.normalized_score or 0.0 for item in receipt.outcome_receipts) / len(
        receipt.task_ids
    )


def _total_cost(receipt: EvaluatedRunReceipt) -> float | None:
    if receipt.blockers:
        return None
    values = tuple(item.cost_usd for item in receipt.outcome_receipts)
    return _total_metric(values)


def _total_latency(receipt: EvaluatedRunReceipt) -> float | None:
    if receipt.blockers:
        return None
    values = tuple(item.latency_seconds for item in receipt.outcome_receipts)
    return _total_metric(values)


def _total_metric(values: tuple[float | None, ...]) -> float | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _ordered_reasons(
    reasons: tuple[PromotionReason, ...],
) -> tuple[PromotionReason, ...]:
    return tuple(reason for reason in PromotionReason if reason in reasons)
