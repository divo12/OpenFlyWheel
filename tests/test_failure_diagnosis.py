"""Failure-mining diagnosis contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from ofw.evaluation.failure import (
    FailureDiagnosis,
    FailureDiagnosisError,
    FailureErrorCode,
    FailureEvidenceStatus,
    FailureType,
)
from ofw.evaluation.outcome import (
    EvidenceReference,
    OutcomeEvaluation,
    TaskId,
    VerifierId,
    VerifierVerdict,
)
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId

_EVALUATED_AT = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)


def _outcome(verdict: VerifierVerdict = VerifierVerdict.FAIL) -> OutcomeEvaluation:
    return OutcomeEvaluation(
        trace_id=TraceId("trace-1"),
        task_id=TaskId("task-1"),
        verifier_id=VerifierId("itsm-bench@v1"),
        evaluated_at=_EVALUATED_AT,
        verdict=verdict,
        score=0.0 if verdict is VerifierVerdict.FAIL else 1.0,
        evidence=(EvidenceReference("harbor://trial-1/verifier/result"),),
    )


def _supported_diagnosis() -> FailureDiagnosis:
    critical = ObservationId("observation-7")
    return FailureDiagnosis(
        outcome=_outcome(),
        outcome_score_id=ScoreId("outcome-score-1"),
        evidence_status=FailureEvidenceStatus.SUPPORTED,
        issue_type=FailureType.CONTROL_FLOW_FAILURE,
        expected_outcome="Incident INC-123 is closed.",
        actual_outcome="Incident INC-123 remains open.",
        critical_observation_id=critical,
        evidence_observation_ids=(critical, ObservationId("observation-9")),
        root_cause="The agent completed before checking the incident state.",
        counterfactual_action="Read the incident after the update and continue until it is closed.",
        inconclusive_reason=None,
    )


def test_failure_type_is_the_five_type_mining_taxonomy() -> None:
    assert tuple(FailureType) == (
        FailureType.INTENT_PLAN_FAILURE,
        FailureType.TOOL_INTERACTION_FAILURE,
        FailureType.EVIDENCE_GROUNDING_FAILURE,
        FailureType.CONTROL_FLOW_FAILURE,
        FailureType.POLICY_FAILURE,
    )


def test_builds_immutable_supported_diagnosis() -> None:
    diagnosis = _supported_diagnosis()

    assert diagnosis.outcome.trace_id == TraceId("trace-1")
    assert diagnosis.critical_observation_id == ObservationId("observation-7")
    assert diagnosis.issue_type is FailureType.CONTROL_FLOW_FAILURE
    with pytest.raises(FrozenInstanceError):
        diagnosis.root_cause = "changed"  # type: ignore[misc]


def test_diagnosis_requires_an_authoritative_failed_outcome() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, outcome=_outcome(VerifierVerdict.PASS))

    assert raised.value.code is FailureErrorCode.INVALID_OUTCOME


def test_supported_diagnosis_requires_complete_attribution() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, issue_type=None)

    assert raised.value.code is FailureErrorCode.INVALID_STATUS_FIELDS


def test_supported_diagnosis_rejects_an_inconclusive_reason() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, inconclusive_reason="Evidence is incomplete.")

    assert raised.value.code is FailureErrorCode.INVALID_STATUS_FIELDS


def test_supported_diagnosis_requires_cited_critical_observation() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, critical_observation_id=ObservationId("observation-2"))

    assert raised.value.code is FailureErrorCode.INVALID_EVIDENCE


def test_inconclusive_diagnosis_does_not_invent_attribution() -> None:
    diagnosis = FailureDiagnosis(
        outcome=_outcome(),
        outcome_score_id=ScoreId("outcome-score-1"),
        evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
        issue_type=None,
        expected_outcome="Ticket is updated.",
        actual_outcome="Ticket is unchanged.",
        critical_observation_id=None,
        evidence_observation_ids=(ObservationId("observation-3"),),
        root_cause=None,
        counterfactual_action=None,
        inconclusive_reason="The mutating tool result is missing from the trace.",
    )

    assert diagnosis.issue_type is None
    assert diagnosis.critical_observation_id is None


def test_inconclusive_diagnosis_rejects_a_claimed_issue_type() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(
            diagnosis,
            evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
            critical_observation_id=None,
            evidence_observation_ids=(),
            root_cause=None,
            counterfactual_action=None,
            inconclusive_reason="The trace is incomplete.",
        )

    assert raised.value.code is FailureErrorCode.INVALID_STATUS_FIELDS


def test_unknown_evidence_status_is_rejected() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(
            diagnosis,
            evidence_status="unknown",  # type: ignore[arg-type]
            issue_type=None,
            critical_observation_id=None,
            evidence_observation_ids=(),
            root_cause=None,
            counterfactual_action=None,
            inconclusive_reason="The trace is incomplete.",
        )

    assert raised.value.code is FailureErrorCode.INVALID_STATUS_FIELDS


def test_unknown_issue_type_is_rejected() -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, issue_type="unknown")  # type: ignore[arg-type]

    assert raised.value.code is FailureErrorCode.INVALID_STATUS_FIELDS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_outcome", "   "),
        ("actual_outcome", "   "),
        ("root_cause", "r" * 4001),
    ],
)
def test_diagnosis_text_is_required_and_bounded(field: str, value: str) -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        if field == "expected_outcome":
            replace(diagnosis, expected_outcome=value)
        elif field == "actual_outcome":
            replace(diagnosis, actual_outcome=value)
        else:
            replace(diagnosis, root_cause=value)

    assert raised.value.code is FailureErrorCode.INVALID_TEXT


def test_evidence_is_unique() -> None:
    diagnosis = _supported_diagnosis()
    critical = diagnosis.critical_observation_id
    assert critical is not None

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(diagnosis, evidence_observation_ids=(critical, critical))

    assert raised.value.code is FailureErrorCode.INVALID_EVIDENCE


def test_evidence_count_has_a_hard_bound() -> None:
    diagnosis = _supported_diagnosis()
    evidence = tuple(ObservationId(f"observation-{index}") for index in range(11))

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(
            diagnosis,
            critical_observation_id=evidence[0],
            evidence_observation_ids=evidence,
        )

    assert raised.value.code is FailureErrorCode.INVALID_EVIDENCE


@pytest.mark.parametrize(
    ("score_id", "observation_id", "code"),
    [
        (
            ScoreId("invalid score"),
            ObservationId("observation-1"),
            FailureErrorCode.INVALID_OUTCOME_SCORE_ID,
        ),
        (
            ScoreId("score-1"),
            ObservationId("invalid observation"),
            FailureErrorCode.INVALID_OBSERVATION_ID,
        ),
        (
            ScoreId("s" * 257),
            ObservationId("observation-1"),
            FailureErrorCode.INVALID_OUTCOME_SCORE_ID,
        ),
        (
            ScoreId("score-1"),
            ObservationId("o" * 257),
            FailureErrorCode.INVALID_OBSERVATION_ID,
        ),
    ],
)
def test_diagnosis_identifiers_are_strict(
    score_id: ScoreId,
    observation_id: ObservationId,
    code: FailureErrorCode,
) -> None:
    diagnosis = _supported_diagnosis()

    with pytest.raises(FailureDiagnosisError) as raised:
        replace(
            diagnosis,
            outcome_score_id=score_id,
            critical_observation_id=observation_id,
            evidence_observation_ids=(observation_id,),
        )

    assert raised.value.code is code
