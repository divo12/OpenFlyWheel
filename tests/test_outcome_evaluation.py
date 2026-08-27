"""Authoritative task-outcome contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ofw.evaluation.outcome import (
    OutcomeErrorCode,
    OutcomeEvaluation,
    OutcomeEvaluationError,
    TaskId,
    VerifierId,
)
from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import EvidenceReference, VerifierResult, VerifierVerdict

_EVALUATED_AT = datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC)


def test_builds_authoritative_outcome_from_existing_verifier_result() -> None:
    evidence = EvidenceReference("harbor://trial-1/verifier/ctrf")
    result = VerifierResult(
        verdict=VerifierVerdict.PASS,
        score=1.0,
        feedback="25 verifier checks passed",
        evidence=(evidence,),
    )

    outcome = OutcomeEvaluation.from_verifier_result(
        trace_id=TraceId("trace-1"),
        task_id=TaskId("task-1"),
        verifier_id=VerifierId("verifier@v1"),
        evaluated_at=_EVALUATED_AT,
        result=result,
    )

    assert outcome.trace_id == TraceId("trace-1")
    assert outcome.task_id == TaskId("task-1")
    assert outcome.verifier_id == VerifierId("verifier@v1")
    assert outcome.verdict is VerifierVerdict.PASS
    assert outcome.score == 1.0
    assert outcome.evidence == (evidence,)


@pytest.mark.parametrize("verdict", [VerifierVerdict.PASS, VerifierVerdict.FAIL])
def test_decisive_outcome_requires_a_score(verdict: VerifierVerdict) -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=verdict,
            score=None,
            evidence=(EvidenceReference("evidence-1"),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_SCORE


@pytest.mark.parametrize("verdict", [VerifierVerdict.ABSTAIN, VerifierVerdict.ERROR])
def test_inconclusive_outcome_rejects_a_score(verdict: VerifierVerdict) -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=verdict,
            score=0.0,
            evidence=(EvidenceReference("evidence-1"),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_SCORE


def test_outcome_requires_bounded_evidence() -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=VerifierVerdict.ABSTAIN,
            score=None,
            evidence=(),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_EVIDENCE


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf")])
def test_score_is_normalized_and_finite(score: float) -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=VerifierVerdict.PASS,
            score=score,
            evidence=(EvidenceReference("evidence-1"),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_SCORE


def test_evidence_count_has_a_hard_bound() -> None:
    evidence = tuple(EvidenceReference(f"evidence-{index}") for index in range(11))

    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=evidence,
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_EVIDENCE


def test_evidence_reference_has_a_hard_size_bound() -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=(EvidenceReference("e" * 1025),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_EVIDENCE


def test_trace_identifier_is_strict() -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("invalid trace"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=_EVALUATED_AT,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=(EvidenceReference("evidence-1"),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_TRACE_ID


@pytest.mark.parametrize(
    "evaluated_at",
    [
        datetime(2026, 8, 27, 10, 3, 46),
        datetime(2026, 8, 27, 15, 33, 46, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_evaluation_timestamp_must_be_utc(evaluated_at: datetime) -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        OutcomeEvaluation(
            trace_id=TraceId("trace-1"),
            task_id=TaskId("task-1"),
            verifier_id=VerifierId("verifier@v1"),
            evaluated_at=evaluated_at,
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=(EvidenceReference("evidence-1"),),
        )

    assert raised.value.code is OutcomeErrorCode.INVALID_EVALUATED_AT


@pytest.mark.parametrize(
    ("constructor", "value", "code"),
    [
        (TaskId, "Task 1", OutcomeErrorCode.INVALID_TASK_ID),
        (VerifierId, "Verifier V1", OutcomeErrorCode.INVALID_VERIFIER_ID),
    ],
)
def test_identifiers_are_strict(
    constructor: type[TaskId] | type[VerifierId],
    value: str,
    code: OutcomeErrorCode,
) -> None:
    with pytest.raises(OutcomeEvaluationError) as raised:
        constructor(value)

    assert raised.value.code is code
