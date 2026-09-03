"""Strict durable evaluated-run receipt contracts."""

from __future__ import annotations

import math

import pytest
from pydantic import TypeAdapter, ValidationError

from ofw.evaluation.outcome import (
    EvaluatedRunBlocker,
    EvaluatedRunReceipt,
    EvaluatedTaskReceipt,
    RunSide,
    VerifierVerdict,
)

_DIGEST = "sha256:" + "a" * 64
_COMMIT = "b" * 40
_TREE = "c" * 40
_JSON_OBJECT = TypeAdapter(dict[str, object])


def _task(
    task_id: str,
    *,
    verdict: VerifierVerdict = VerifierVerdict.PASS,
    score: float | None = 1.0,
) -> EvaluatedTaskReceipt:
    return EvaluatedTaskReceipt(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        score_id=f"score-{task_id}",
        verdict=verdict,
        verifier_id="itsm-bench@checksum",
        normalized_score=score,
        cost_usd=0.25,
        latency_seconds=1.5,
    )


def _blocker(task_id: str) -> EvaluatedRunBlocker:
    return EvaluatedRunBlocker(
        task_id=task_id,
        code="trace_ambiguous",
        subject="trace_mapping",
    )


def _receipt(
    task_ids: tuple[str, ...] = ("task-1", "task-2"),
    outcomes: tuple[EvaluatedTaskReceipt, ...] = (),
    blockers: tuple[EvaluatedRunBlocker, ...] = (),
) -> EvaluatedRunReceipt:
    return EvaluatedRunReceipt.build(
        run_id="run-1",
        side=RunSide.CANDIDATE,
        policy_digest=_DIGEST,
        controls_digest=_DIGEST,
        evaluated_commit=_COMMIT,
        evaluated_tree=_TREE,
        task_ids=task_ids,
        outcome_receipts=outcomes,
        blockers=blockers,
    )


def test_evaluated_run_receipt_is_immutable_and_deterministic() -> None:
    first = _receipt(outcomes=(_task("task-1"),), blockers=(_blocker("task-2"),))
    second = _receipt(outcomes=(_task("task-1"),), blockers=(_blocker("task-2"),))

    assert first == second
    assert first.receipt_id == first.recomputed_id()
    assert first.receipt_id.startswith("sha256:")
    with pytest.raises(ValidationError):
        first.run_id = "other"


def test_evaluated_run_receipt_rejects_tampered_hash_and_extra_fields() -> None:
    receipt = _receipt(outcomes=(_task("task-1"),), blockers=(_blocker("task-2"),))
    payload = _JSON_OBJECT.validate_json(receipt.model_dump_json())
    payload["run_id"] = "tampered"
    with pytest.raises(ValidationError):
        EvaluatedRunReceipt.model_validate(payload)

    payload = _JSON_OBJECT.validate_json(receipt.model_dump_json())
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        EvaluatedRunReceipt.model_validate(payload)


@pytest.mark.parametrize(
    ("task_ids", "outcomes", "blockers"),
    (
        (("task-1", "task-2"), (_task("task-1"),), ()),
        (("task-1",), (_task("task-1"),), (_blocker("task-1"),)),
        (("task-1",), (_task("task-1"), _task("task-1")), ()),
        (("task-1",), (), (_blocker("task-1"), _blocker("task-1"))),
        (("task-1",), (), (_blocker("task-2"),)),
    ),
)
def test_evaluated_run_receipt_requires_an_exact_unique_partition(
    task_ids: tuple[str, ...],
    outcomes: tuple[EvaluatedTaskReceipt, ...],
    blockers: tuple[EvaluatedRunBlocker, ...],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _receipt(task_ids, outcomes, blockers)


def test_evaluated_run_receipt_requires_task_order_within_each_partition() -> None:
    with pytest.raises((ValidationError, ValueError)):
        _receipt(
            task_ids=("task-1", "task-2"),
            outcomes=(_task("task-2"),),
            blockers=(_blocker("task-1"),),
        )


@pytest.mark.parametrize(
    ("verdict", "score"),
    (
        (VerifierVerdict.PASS, None),
        (VerifierVerdict.PASS, 0.5),
        (VerifierVerdict.FAIL, 1.0),
        (VerifierVerdict.ABSTAIN, 0.0),
    ),
)
def test_evaluated_task_receipt_validates_decisive_scores(
    verdict: VerifierVerdict,
    score: float | None,
) -> None:
    with pytest.raises(ValidationError):
        _task("task-1", verdict=verdict, score=score)


@pytest.mark.parametrize(
    "field",
    ("normalized_score", "cost_usd", "latency_seconds"),
)
def test_evaluated_task_receipt_rejects_non_finite_or_out_of_bound_metrics(
    field: str,
) -> None:
    values = {
        "normalized_score": 1.0,
        "cost_usd": 0.25,
        "latency_seconds": 1.5,
    }
    values[field] = math.inf
    with pytest.raises(ValidationError):
        EvaluatedTaskReceipt(
            task_id="task-1",
            trace_id="trace-task-1",
            score_id="score-task-1",
            verdict=VerifierVerdict.PASS,
            verifier_id="itsm-bench@checksum",
            **values,
        )


def test_evaluated_task_receipt_preserves_missing_optional_metrics() -> None:
    receipt = EvaluatedTaskReceipt(
        task_id="task-1",
        trace_id="trace-task-1",
        score_id="score-task-1",
        verdict=VerifierVerdict.ABSTAIN,
        verifier_id="itsm-bench@checksum",
        normalized_score=None,
        cost_usd=None,
        latency_seconds=None,
    )

    assert receipt.cost_usd is None
    assert receipt.latency_seconds is None


def test_evaluated_run_receipt_rejects_non_strict_scalar_input() -> None:
    payload = _receipt(outcomes=(_task("task-1"),), blockers=(_blocker("task-2"),))
    raw = _JSON_OBJECT.validate_json(payload.model_dump_json())
    raw["run_id"] = 1
    with pytest.raises(ValidationError):
        EvaluatedRunReceipt.model_validate(raw)


def test_evaluated_run_receipt_identity_changes_with_receipt_content() -> None:
    first = _receipt(outcomes=(_task("task-1"),), blockers=(_blocker("task-2"),))
    changed = EvaluatedRunReceipt.build(
        run_id=first.run_id,
        side=first.side,
        policy_digest=first.policy_digest,
        controls_digest=first.controls_digest,
        evaluated_commit=first.evaluated_commit,
        evaluated_tree=first.evaluated_tree,
        task_ids=first.task_ids,
        outcome_receipts=(
            EvaluatedTaskReceipt(
                task_id="task-1",
                trace_id="trace-task-1",
                score_id="score-task-1",
                verdict=VerifierVerdict.PASS,
                verifier_id="itsm-bench@checksum",
                normalized_score=1.0,
                cost_usd=0.5,
                latency_seconds=1.5,
            ),
        ),
        blockers=first.blockers,
    )

    assert first.receipt_id != changed.receipt_id
