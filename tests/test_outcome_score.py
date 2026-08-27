"""Langfuse outcome-score publisher tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ofw.evaluation.langfuse import (
    OUTCOME_SCORE_NAME,
    LangfuseOutcomeStore,
)
from ofw.evaluation.outcome import OutcomeEvaluation, TaskId, VerifierId
from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import EvidenceReference, VerifierVerdict

_EVALUATED_AT = datetime(2026, 8, 27, 10, 3, 46, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _ScoreCall:
    name: str
    value: str
    trace_id: str
    score_id: str
    data_type: Literal["CATEGORICAL"]
    comment: str
    metadata: dict[str, object]
    timestamp: datetime


class _FakeScoreClient:
    def __init__(self) -> None:
        self.calls: list[_ScoreCall] = []
        self.flush_count = 0
        self.shutdown_count = 0

    def create_score(
        self,
        *,
        name: str,
        value: str,
        trace_id: str,
        score_id: str,
        data_type: Literal["CATEGORICAL"],
        comment: str,
        metadata: dict[str, object],
        timestamp: datetime,
    ) -> None:
        self.calls.append(
            _ScoreCall(
                name,
                value,
                trace_id,
                score_id,
                data_type,
                comment,
                metadata,
                timestamp,
            )
        )

    def flush(self) -> None:
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _outcome(
    verdict: VerifierVerdict = VerifierVerdict.PASS,
    score: float | None = 1.0,
) -> OutcomeEvaluation:
    return OutcomeEvaluation(
        trace_id=TraceId("trace-1"),
        task_id=TaskId("task-1"),
        verifier_id=VerifierId("verifier@v1"),
        evaluated_at=_EVALUATED_AT,
        verdict=verdict,
        score=score,
        evidence=(
            EvidenceReference("artifact://result"),
            EvidenceReference("artifact://verifier"),
        ),
    )


def test_stores_categorical_outcome_on_the_trace_with_typed_metadata() -> None:
    client = _FakeScoreClient()
    store = LangfuseOutcomeStore(client)

    submission = store.store(_outcome())

    assert submission.trace_id == TraceId("trace-1")
    assert submission.score_id.value == client.calls[0].score_id
    assert client.calls == [
        _ScoreCall(
            name=OUTCOME_SCORE_NAME,
            value="pass",
            trace_id="trace-1",
            score_id=submission.score_id.value,
            data_type="CATEGORICAL",
            comment="Authoritative outcome from verifier@v1: pass.",
            metadata={
                "schema_version": 1,
                "task_id": "task-1",
                "verifier_id": "verifier@v1",
                "normalized_score": 1.0,
                "evidence": ["artifact://result", "artifact://verifier"],
            },
            timestamp=_EVALUATED_AT,
        )
    ]
    assert client.flush_count == 1


def test_score_id_is_stable_and_inconclusive_outcomes_remain_explicit() -> None:
    client = _FakeScoreClient()
    store = LangfuseOutcomeStore(client)
    outcome = _outcome(VerifierVerdict.ABSTAIN, None)

    first = store.store(outcome)
    second = store.store(outcome)

    assert (
        first.score_id,
        client.calls[0].value,
        client.calls[0].metadata["normalized_score"],
        client.calls[0].timestamp,
        client.flush_count,
    ) == (
        second.score_id,
        "abstain",
        None,
        client.calls[1].timestamp,
        2,
    )


def test_close_shuts_down_the_owned_client() -> None:
    client = _FakeScoreClient()
    store = LangfuseOutcomeStore(client)

    store.close()

    assert client.shutdown_count == 1
