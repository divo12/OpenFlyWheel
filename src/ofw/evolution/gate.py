"""Pure deterministic admission gate for an executed candidate."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum

from ofw.evaluation.outcome import VerifierVerdict
from ofw.evolution.candidate import (
    CandidateBlockerCode,
    CandidateExecutionObservation,
    CandidateId,
    CandidateOutcomeReceipt,
    CandidateStatus,
    candidate_policy_digest,
)
from ofw.preparation.policy import ExperimentPolicySnapshot


class PromotionStatus(StrEnum):
    ACCEPT = "accept"
    ACCEPTED = "accept"
    REJECT = "reject"
    REJECTED = "reject"
    INCONCLUSIVE = "inconclusive"


class PromotionReason(StrEnum):
    IDENTITY_MISMATCH = "identity_mismatch"
    TASK_SET_MISMATCH = "task_set_mismatch"
    UNSUPPORTED_OUTCOME = "unsupported_outcome"
    ERROR_OUTCOME = "error_outcome"
    ABSTAIN_OUTCOME = "abstain_outcome"
    UNVERIFIED_OUTCOME = "unverified_outcome"
    MISSING_COST = "missing_cost"
    MISSING_LATENCY = "missing_latency"
    PASS_REGRESSION = "pass_regression"
    NO_IMPROVEMENT = "no_improvement"
    IMPROVEMENT = "improvement"


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision_id: str
    policy_id: str
    accepted_run_id: str
    candidate_run_id: str
    status: PromotionStatus
    reasons: tuple[PromotionReason, ...]
    task_ids: tuple[str, ...]
    accepted_passes: tuple[str, ...]
    candidate_passes: tuple[str, ...]
    accepted_quality: float
    candidate_quality: float
    accepted_cost: float | None
    candidate_cost: float | None
    accepted_latency: float | None
    candidate_latency: float | None
    canonical_json: str


def decide_promotion(
    policy: ExperimentPolicySnapshot,
    accepted_run: CandidateExecutionObservation,
    candidate_run: CandidateExecutionObservation,
) -> PromotionDecision:
    """Return the same decision for the same policy and immutable run receipts."""
    identity_reasons = _identity_reasons(policy, accepted_run, candidate_run)
    task_reasons = _task_reasons(policy, accepted_run, candidate_run)
    reasons = _ordered_reasons(identity_reasons + task_reasons)
    if reasons:
        return _decision(policy, accepted_run, candidate_run, PromotionStatus.INCONCLUSIVE, reasons)

    outcome_reasons = _outcome_reasons(accepted_run, candidate_run)
    metric_reasons = _metric_reasons(policy)
    reasons = _ordered_reasons(outcome_reasons + metric_reasons)
    if reasons:
        return _decision(policy, accepted_run, candidate_run, PromotionStatus.INCONCLUSIVE, reasons)

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
    if len(candidate_passes) <= len(accepted_passes):
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


def _identity_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> tuple[PromotionReason, ...]:
    if _missing_git_identity(accepted, candidate):
        return (PromotionReason.IDENTITY_MISMATCH,)
    if _authority_identity_mismatch(policy, accepted, candidate):
        return (PromotionReason.IDENTITY_MISMATCH,)
    if not _candidate_id_matches(policy, accepted) or not _candidate_id_matches(policy, candidate):
        return (PromotionReason.IDENTITY_MISMATCH,)
    return ()


def _missing_git_identity(
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> bool:
    return accepted.candidate_commit is None or candidate.candidate_commit is None


def _authority_identity_mismatch(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> bool:
    return (
        _wrong_experiment(policy, accepted, candidate)
        or _different_lineage(accepted, candidate)
        or _same_candidate_identity(accepted, candidate)
    )


def _wrong_experiment(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> bool:
    return (
        accepted.experiment_id != policy.experiment_id
        or candidate.experiment_id != policy.experiment_id
    )


def _different_lineage(
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> bool:
    return (
        accepted.hypothesis_id != candidate.hypothesis_id
        or accepted.source_commit != candidate.source_commit
    )


def _same_candidate_identity(
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> bool:
    return (
        accepted.candidate_id == candidate.candidate_id
        or accepted.candidate_tree == candidate.candidate_tree
        or accepted.candidate_commit == candidate.candidate_commit
    )


def _candidate_id_matches(
    policy: ExperimentPolicySnapshot,
    run: CandidateExecutionObservation,
) -> bool:
    if run.candidate_id is None or run.candidate_tree is None or run.source_commit is None:
        return False
    expected = CandidateId.build(
        policy_digest=candidate_policy_digest(policy),
        hypothesis_id=run.hypothesis_id,
        source_commit=run.source_commit,
        candidate_tree=run.candidate_tree,
        controls_digest=policy.controls_digest,
    )
    return run.candidate_id == expected.value


def _task_reasons(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> tuple[PromotionReason, ...]:
    if _task_sequence(accepted) != policy.task_ids or _task_sequence(candidate) != policy.task_ids:
        return (PromotionReason.TASK_SET_MISMATCH,)
    if not _receipt_ids_unique(accepted) or not _receipt_ids_unique(candidate):
        return (PromotionReason.TASK_SET_MISMATCH,)
    return ()


def _outcome_reasons(
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
) -> tuple[PromotionReason, ...]:
    return _run_outcome_reasons(accepted) + _run_outcome_reasons(candidate)


def _run_outcome_reasons(run: CandidateExecutionObservation) -> tuple[PromotionReason, ...]:
    reasons: list[PromotionReason] = []
    if run.status is CandidateStatus.ERROR:
        reasons.append(PromotionReason.UNVERIFIED_OUTCOME)
    reasons.extend(_blocker_reasons(run))
    reasons.extend(_receipt_reasons(run))
    return tuple(reasons)


def _blocker_reasons(run: CandidateExecutionObservation) -> tuple[PromotionReason, ...]:
    return tuple(
        PromotionReason.UNSUPPORTED_OUTCOME
        if blocker.code is CandidateBlockerCode.UNSUPPORTED_REWARD
        else PromotionReason.UNVERIFIED_OUTCOME
        for blocker in run.blockers
    )


def _receipt_reasons(run: CandidateExecutionObservation) -> tuple[PromotionReason, ...]:
    return tuple(
        reason
        for receipt in run.outcome_receipts
        if (reason := _receipt_reason(receipt)) is not None
    )


def _receipt_reason(receipt: CandidateOutcomeReceipt) -> PromotionReason | None:
    if receipt.verdict is VerifierVerdict.ERROR:
        return PromotionReason.ERROR_OUTCOME
    if receipt.verdict is VerifierVerdict.ABSTAIN:
        return PromotionReason.ABSTAIN_OUTCOME
    return None


def _metric_reasons(policy: ExperimentPolicySnapshot) -> tuple[PromotionReason, ...]:
    reasons: list[PromotionReason] = []
    if policy.max_cost_per_task_usd is not None:
        reasons.append(PromotionReason.MISSING_COST)
    if policy.max_latency_seconds is not None:
        reasons.append(PromotionReason.MISSING_LATENCY)
    return tuple(reasons)


def _decision(
    policy: ExperimentPolicySnapshot,
    accepted: CandidateExecutionObservation,
    candidate: CandidateExecutionObservation,
    status: PromotionStatus,
    reasons: tuple[PromotionReason, ...],
) -> PromotionDecision:
    accepted_passes = _passes(accepted)
    candidate_passes = _passes(candidate)
    accepted_quality = _quality(accepted_passes, policy.task_ids)
    candidate_quality = _quality(candidate_passes, policy.task_ids)
    accepted_id = accepted.candidate_id or ""
    candidate_id = candidate.candidate_id or ""
    canonical_json = json.dumps(
        (
            ("policy_id", candidate_policy_digest(policy)),
            ("controls_digest", policy.controls_digest),
            ("accepted_identity", _identity(accepted)),
            ("candidate_identity", _identity(candidate)),
            ("accepted_receipts", _receipts(accepted)),
            ("candidate_receipts", _receipts(candidate)),
            ("accepted_blockers", _blockers(accepted)),
            ("candidate_blockers", _blockers(candidate)),
            (
                "metrics",
                (
                    ("task_ids", policy.task_ids),
                    ("accepted_passes", accepted_passes),
                    ("candidate_passes", candidate_passes),
                    ("accepted_quality", accepted_quality),
                    ("candidate_quality", candidate_quality),
                    ("accepted_cost", None),
                    ("candidate_cost", None),
                    ("accepted_latency", None),
                    ("candidate_latency", None),
                ),
            ),
            ("status", status.value),
            ("reasons", tuple(reason.value for reason in reasons)),
        ),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return PromotionDecision(
        decision_id=f"sha256:{digest}",
        policy_id=candidate_policy_digest(policy),
        accepted_run_id=accepted_id,
        candidate_run_id=candidate_id,
        status=status,
        reasons=reasons,
        task_ids=policy.task_ids,
        accepted_passes=accepted_passes,
        candidate_passes=candidate_passes,
        accepted_quality=accepted_quality,
        candidate_quality=candidate_quality,
        accepted_cost=None,
        candidate_cost=None,
        accepted_latency=None,
        candidate_latency=None,
        canonical_json=canonical_json,
    )


def _identity(run: CandidateExecutionObservation) -> tuple[str | None, ...]:
    return (
        run.experiment_id,
        run.hypothesis_id,
        run.source_commit,
        run.candidate_id,
        run.candidate_tree,
        run.candidate_commit,
    )


def _receipts(run: CandidateExecutionObservation) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (receipt.task_id, receipt.trace_id, receipt.score_id, receipt.verdict.value)
        for receipt in run.outcome_receipts
    )


def _blockers(run: CandidateExecutionObservation) -> tuple[tuple[str, str, str], ...]:
    return tuple((blocker.task_id, blocker.code.value, blocker.subject) for blocker in run.blockers)


def _task_sequence(run: CandidateExecutionObservation) -> tuple[str, ...]:
    return tuple(item.task_id for item in run.outcome_receipts) + tuple(
        item.task_id for item in run.blockers
    )


def _receipt_ids_unique(run: CandidateExecutionObservation) -> bool:
    ids = tuple(item.score_id for item in run.outcome_receipts)
    return len(ids) == len(set(ids))


def _passes(run: CandidateExecutionObservation) -> tuple[str, ...]:
    return tuple(
        item.task_id for item in run.outcome_receipts if item.verdict is VerifierVerdict.PASS
    )


def _quality(passes: tuple[str, ...], task_ids: tuple[str, ...]) -> float:
    value = len(passes) / len(task_ids)
    return value if math.isfinite(value) else 0.0


def _ordered_reasons(reasons: tuple[PromotionReason, ...]) -> tuple[PromotionReason, ...]:
    return tuple(reason for reason in PromotionReason if reason in reasons)
