"""Immutable authoritative task-outcome contract."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import EvidenceReference, VerifierResult, VerifierVerdict

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_IDENTIFIER_LIMIT = 256
_EVIDENCE_LIMIT = 10
_EVIDENCE_VALUE_LIMIT = 1024


class OutcomeErrorCode(StrEnum):
    INVALID_TASK_ID = "invalid_task_id"
    INVALID_VERIFIER_ID = "invalid_verifier_id"
    INVALID_TRACE_ID = "invalid_trace_id"
    INVALID_EVALUATED_AT = "invalid_evaluated_at"
    INVALID_SCORE = "invalid_score"
    INVALID_EVIDENCE = "invalid_evidence"


class OutcomeEvaluationError(Exception):
    """Typed failure while constructing an authoritative outcome."""

    __slots__ = ("code", "subject")

    def __init__(self, code: OutcomeErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class TaskId:
    value: str

    def __post_init__(self) -> None:
        _validate_identifier(self.value, OutcomeErrorCode.INVALID_TASK_ID)


@dataclass(frozen=True, slots=True)
class VerifierId:
    value: str

    def __post_init__(self) -> None:
        _validate_identifier(self.value, OutcomeErrorCode.INVALID_VERIFIER_ID)


@dataclass(frozen=True, slots=True)
class OutcomeEvaluation:
    trace_id: TraceId
    task_id: TaskId
    verifier_id: VerifierId
    evaluated_at: datetime
    verdict: VerifierVerdict
    score: float | None
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        _validate_trace_id(self.trace_id)
        _validate_evaluated_at(self.evaluated_at)
        _validate_score(self.verdict, self.score)
        _validate_evidence(self.evidence)

    @classmethod
    def from_verifier_result(
        cls,
        *,
        trace_id: TraceId,
        task_id: TaskId,
        verifier_id: VerifierId,
        evaluated_at: datetime,
        result: VerifierResult,
    ) -> OutcomeEvaluation:
        return cls(
            trace_id=trace_id,
            task_id=task_id,
            verifier_id=verifier_id,
            evaluated_at=evaluated_at,
            verdict=result.verdict,
            score=result.score,
            evidence=result.evidence,
        )


def _validate_identifier(value: str, code: OutcomeErrorCode) -> None:
    if len(value) > _IDENTIFIER_LIMIT or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise OutcomeEvaluationError(code, value)


def _validate_trace_id(trace_id: TraceId) -> None:
    _validate_identifier(trace_id.value, OutcomeErrorCode.INVALID_TRACE_ID)


def _validate_evaluated_at(evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() != timedelta(0):
        raise OutcomeEvaluationError(
            OutcomeErrorCode.INVALID_EVALUATED_AT,
            evaluated_at.isoformat(),
        )


def _validate_score(verdict: VerifierVerdict, score: float | None) -> None:
    decisive = verdict in (VerifierVerdict.PASS, VerifierVerdict.FAIL)
    if decisive != (score is not None):
        raise OutcomeEvaluationError(OutcomeErrorCode.INVALID_SCORE, verdict.value)
    if score is not None and (not math.isfinite(score) or not 0.0 <= score <= 1.0):
        raise OutcomeEvaluationError(OutcomeErrorCode.INVALID_SCORE, str(score))


def _validate_evidence(evidence: tuple[EvidenceReference, ...]) -> None:
    if not 1 <= len(evidence) <= _EVIDENCE_LIMIT:
        raise OutcomeEvaluationError(OutcomeErrorCode.INVALID_EVIDENCE, str(len(evidence)))
    if any(not 1 <= len(item.value) <= _EVIDENCE_VALUE_LIMIT for item in evidence):
        raise OutcomeEvaluationError(OutcomeErrorCode.INVALID_EVIDENCE, "reference")
