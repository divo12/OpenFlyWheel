"""Immutable trace-grounded failure-diagnosis contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ofw.evaluation.outcome import OutcomeEvaluation
from ofw.observability.langfuse.domain import ObservationId, ScoreId
from ofw.runtime import VerifierVerdict

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_IDENTIFIER_LIMIT = 256
_TEXT_LIMIT = 4000
_EVIDENCE_LIMIT = 10


class FailureType(StrEnum):
    """Stable top-level causes used to route failure diagnoses."""

    INTENT_PLAN_FAILURE = "intent_plan_failure"
    TOOL_INTERACTION_FAILURE = "tool_interaction_failure"
    EVIDENCE_GROUNDING_FAILURE = "evidence_grounding_failure"
    CONTROL_FLOW_FAILURE = "control_flow_failure"
    POLICY_FAILURE = "policy_failure"


class FailureEvidenceStatus(StrEnum):
    """Whether trace evidence supports a causal attribution."""

    SUPPORTED = "supported"
    INCONCLUSIVE = "inconclusive"


class FailureErrorCode(StrEnum):
    INVALID_OUTCOME = "invalid_outcome"
    INVALID_OUTCOME_SCORE_ID = "invalid_outcome_score_id"
    INVALID_OBSERVATION_ID = "invalid_observation_id"
    INVALID_TEXT = "invalid_text"
    INVALID_EVIDENCE = "invalid_evidence"
    INVALID_STATUS_FIELDS = "invalid_status_fields"


class FailureDiagnosisError(Exception):
    """Typed failure while constructing a trace-grounded diagnosis."""

    __slots__ = ("code", "subject")

    def __init__(self, code: FailureErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    outcome: OutcomeEvaluation
    outcome_score_id: ScoreId
    evidence_status: FailureEvidenceStatus
    issue_type: FailureType | None
    expected_outcome: str
    actual_outcome: str
    critical_observation_id: ObservationId | None
    evidence_observation_ids: tuple[ObservationId, ...]
    root_cause: str | None
    counterfactual_action: str | None
    inconclusive_reason: str | None

    def __post_init__(self) -> None:
        _validate_outcome(self)
        _validate_identifier(
            self.outcome_score_id.value,
            FailureErrorCode.INVALID_OUTCOME_SCORE_ID,
        )
        _validate_observation_identifiers(self)
        _validate_text(self.expected_outcome, "expected_outcome")
        _validate_text(self.actual_outcome, "actual_outcome")
        _validate_optional_text(self.root_cause, "root_cause")
        _validate_optional_text(self.counterfactual_action, "counterfactual_action")
        _validate_optional_text(self.inconclusive_reason, "inconclusive_reason")
        _validate_evidence(self)
        _validate_status_fields(self)


def _validate_outcome(diagnosis: FailureDiagnosis) -> None:
    if diagnosis.outcome.verdict is not VerifierVerdict.FAIL:
        raise FailureDiagnosisError(FailureErrorCode.INVALID_OUTCOME, "verdict")


def _validate_identifier(value: str, code: FailureErrorCode) -> None:
    if len(value) > _IDENTIFIER_LIMIT or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise FailureDiagnosisError(code, value)


def _validate_observation_identifiers(diagnosis: FailureDiagnosis) -> None:
    if diagnosis.critical_observation_id is not None:
        _validate_identifier(
            diagnosis.critical_observation_id.value,
            FailureErrorCode.INVALID_OBSERVATION_ID,
        )
    for observation_id in diagnosis.evidence_observation_ids:
        _validate_identifier(
            observation_id.value,
            FailureErrorCode.INVALID_OBSERVATION_ID,
        )


def _validate_text(value: str, subject: str) -> None:
    if not value.strip() or len(value) > _TEXT_LIMIT:
        raise FailureDiagnosisError(FailureErrorCode.INVALID_TEXT, subject)


def _validate_optional_text(value: str | None, subject: str) -> None:
    if value is not None:
        _validate_text(value, subject)


def _validate_evidence(diagnosis: FailureDiagnosis) -> None:
    evidence = diagnosis.evidence_observation_ids
    if len(evidence) > _EVIDENCE_LIMIT or len(set(evidence)) != len(evidence):
        raise FailureDiagnosisError(FailureErrorCode.INVALID_EVIDENCE, "observations")


def _validate_status_fields(diagnosis: FailureDiagnosis) -> None:
    if diagnosis.evidence_status is FailureEvidenceStatus.SUPPORTED:
        _validate_supported(diagnosis)
        return
    _validate_inconclusive(diagnosis)


def _validate_supported(diagnosis: FailureDiagnosis) -> None:
    required = (
        diagnosis.issue_type,
        diagnosis.critical_observation_id,
        diagnosis.root_cause,
        diagnosis.counterfactual_action,
    )
    if any(value is None for value in required) or diagnosis.inconclusive_reason is not None:
        raise FailureDiagnosisError(FailureErrorCode.INVALID_STATUS_FIELDS, "supported")
    if diagnosis.critical_observation_id not in diagnosis.evidence_observation_ids:
        raise FailureDiagnosisError(FailureErrorCode.INVALID_EVIDENCE, "critical_observation_id")


def _validate_inconclusive(diagnosis: FailureDiagnosis) -> None:
    unsupported = (
        diagnosis.issue_type,
        diagnosis.critical_observation_id,
        diagnosis.root_cause,
        diagnosis.counterfactual_action,
    )
    if any(value is not None for value in unsupported) or diagnosis.inconclusive_reason is None:
        raise FailureDiagnosisError(FailureErrorCode.INVALID_STATUS_FIELDS, "inconclusive")
