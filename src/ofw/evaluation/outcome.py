"""Immutable authoritative task-outcome contract."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from ofw.observability.langfuse.domain import TraceId

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_IDENTIFIER_LIMIT = 256
_EVIDENCE_LIMIT = 10
_EVIDENCE_VALUE_LIMIT = 1024
_RUN_ID_LIMIT = 256
_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"
_RUN_METRIC_LIMIT = 172800.0
_COST_LIMIT = 1_000_000.0


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


class VerifierVerdict(StrEnum):
    PASS = "pass"  # nosec B105
    FAIL = "fail"
    ABSTAIN = "abstain"
    ERROR = "error"


class RunSide(StrEnum):
    ACCEPTED = "accepted"
    CANDIDATE = "candidate"


class _ReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvaluatedTaskReceipt(_ReceiptModel):
    task_id: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    trace_id: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    score_id: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    verdict: VerifierVerdict
    verifier_id: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    normalized_score: float | None = None
    cost_usd: float | None = None
    latency_seconds: float | None = None

    @model_validator(mode="after")
    def validate_metrics(self) -> EvaluatedTaskReceipt:
        _validate_run_metric(self.normalized_score, 0.0, 1.0, "normalized_score")
        _validate_run_metric(self.cost_usd, 0.0, _COST_LIMIT, "cost_usd")
        _validate_run_metric(
            self.latency_seconds,
            0.0,
            _RUN_METRIC_LIMIT,
            "latency_seconds",
        )
        _validate_verdict_score(self.verdict, self.normalized_score)
        return self


class EvaluatedRunBlocker(_ReceiptModel):
    task_id: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    code: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)
    subject: StrictStr = Field(min_length=1, max_length=_IDENTIFIER_LIMIT)


class EvaluatedRunReceipt(_ReceiptModel):
    receipt_id: StrictStr = Field(pattern=_DIGEST_PATTERN)
    run_id: StrictStr = Field(
        min_length=1,
        max_length=_RUN_ID_LIMIT,
        pattern=_RUN_ID_PATTERN,
    )
    side: RunSide
    policy_digest: StrictStr = Field(pattern=_DIGEST_PATTERN)
    controls_digest: StrictStr = Field(pattern=_DIGEST_PATTERN)
    evaluated_commit: StrictStr = Field(pattern=_COMMIT_PATTERN)
    evaluated_tree: StrictStr = Field(pattern=_COMMIT_PATTERN)
    task_ids: tuple[StrictStr, ...] = Field(min_length=1, max_length=500)
    outcome_receipts: tuple[EvaluatedTaskReceipt, ...] = Field(max_length=500)
    blockers: tuple[EvaluatedRunBlocker, ...] = Field(max_length=500)

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        side: RunSide,
        policy_digest: str,
        controls_digest: str,
        evaluated_commit: str,
        evaluated_tree: str,
        task_ids: tuple[str, ...],
        outcome_receipts: tuple[EvaluatedTaskReceipt, ...],
        blockers: tuple[EvaluatedRunBlocker, ...],
    ) -> EvaluatedRunReceipt:
        draft = cls.model_construct(
            receipt_id="sha256:" + "0" * 64,
            run_id=run_id,
            side=side,
            policy_digest=policy_digest,
            controls_digest=controls_digest,
            evaluated_commit=evaluated_commit,
            evaluated_tree=evaluated_tree,
            task_ids=task_ids,
            outcome_receipts=outcome_receipts,
            blockers=blockers,
        )
        return cls(
            receipt_id=draft.recomputed_id(),
            run_id=run_id,
            side=side,
            policy_digest=policy_digest,
            controls_digest=controls_digest,
            evaluated_commit=evaluated_commit,
            evaluated_tree=evaluated_tree,
            task_ids=task_ids,
            outcome_receipts=outcome_receipts,
            blockers=blockers,
        )

    def recomputed_id(self) -> str:
        canonical = self.model_dump_json(exclude={"receipt_id"})
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @model_validator(mode="after")
    def validate_identity_and_partition(self) -> EvaluatedRunReceipt:
        if self.receipt_id != self.recomputed_id():
            raise ValueError("receipt_id does not match canonical receipt content")
        task_ids = self.task_ids
        _validate_unique_ids(task_ids, "task_ids")
        outcome_ids = tuple(item.task_id for item in self.outcome_receipts)
        blocker_ids = tuple(item.task_id for item in self.blockers)
        all_result_ids = outcome_ids + blocker_ids
        _validate_partition(all_result_ids, task_ids)
        _validate_result_order(outcome_ids, task_ids)
        _validate_result_order(blocker_ids, task_ids)
        return self


def _validate_verdict_score(verdict: VerifierVerdict, score: float | None) -> None:
    expected = (
        1.0
        if verdict is VerifierVerdict.PASS
        else 0.0
        if verdict is VerifierVerdict.FAIL
        else None
    )
    if score != expected:
        raise ValueError("normalized_score does not match verdict")


def _validate_partition(
    result_ids: tuple[str, ...],
    task_ids: tuple[str, ...],
) -> None:
    _validate_unique_ids(result_ids, "outcomes and blockers")
    if set(result_ids) != set(task_ids):
        raise ValueError("outcomes and blockers must partition task_ids")


def _validate_unique_ids(values: tuple[str, ...], field: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must be unique")


def _validate_result_order(result_ids: tuple[str, ...], task_ids: tuple[str, ...]) -> None:
    if not _follows_task_order(result_ids, task_ids):
        raise ValueError("outcomes and blockers must follow task_ids order")


def _validate_run_metric(
    value: float | None,
    minimum: float,
    maximum: float,
    field: str,
) -> None:
    if value is not None and (not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"{field} is outside its finite bounds")


def _follows_task_order(
    result_ids: tuple[str, ...],
    task_ids: tuple[str, ...],
) -> bool:
    positions = tuple(task_ids.index(task_id) for task_id in result_ids)
    return positions == tuple(sorted(positions))


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    value: str


@dataclass(frozen=True, slots=True)
class VerifierResult:
    verdict: VerifierVerdict
    score: float | None
    feedback: str
    evidence: tuple[EvidenceReference, ...] = ()


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
