"""Langfuse publisher for authoritative outcome scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from langfuse import Langfuse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ofw.evaluation.outcome import OutcomeEvaluation
from ofw.observability.langfuse.contracts import LangfuseProject
from ofw.observability.langfuse.domain import ScoreId, TraceId

OUTCOME_SCORE_NAME = "ofw.outcome"
_OUTCOME_SCORE_SCHEMA_VERSION = 1


class OutcomeStoreStatus(StrEnum):
    SUCCESS = "success"


class OutcomeStoreObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OutcomeStoreStatus
    summary: StrictStr = Field(max_length=256)
    next_actions: tuple[StrictStr, ...] = Field(max_length=2)
    artifacts: tuple[StrictStr, ...] = Field(max_length=2)
    trace_id: StrictStr = Field(min_length=1, max_length=256)
    score_id: StrictStr = Field(min_length=1, max_length=256)


@dataclass(frozen=True, slots=True)
class OutcomeScoreMetadata:
    schema_version: int
    task_id: str
    verifier_id: str
    normalized_score: float | None
    evidence: tuple[str, ...]

    def provider_payload(self) -> dict[str, object]:
        """Convert to the plain JSON mapping required by the Langfuse SDK."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "verifier_id": self.verifier_id,
            "normalized_score": self.normalized_score,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class OutcomeScoreSubmission:
    score_id: ScoreId
    trace_id: TraceId


class _OutcomeScoreClient(Protocol):
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
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class LangfuseOutcomeStore:
    """Publish outcome evaluations without changing trace-query behavior."""

    def __init__(self, client: _OutcomeScoreClient) -> None:
        self._client = client

    @classmethod
    def from_project(cls, project: LangfuseProject) -> LangfuseOutcomeStore:
        manifest = project.manifest()
        credentials = project.credentials()
        client = Langfuse(
            public_key=credentials.public_key,
            secret_key=credentials.secret_key,
            base_url=manifest.base_url.value,
            environment=manifest.environment.value,
        )
        return cls(client)

    def store(self, outcome: OutcomeEvaluation) -> OutcomeScoreSubmission:
        score_id = _score_id(outcome)
        self._client.create_score(
            name=OUTCOME_SCORE_NAME,
            value=outcome.verdict.value,
            trace_id=outcome.trace_id.value,
            score_id=score_id.value,
            data_type="CATEGORICAL",
            comment=(
                f"Authoritative outcome from {outcome.verifier_id.value}: {outcome.verdict.value}."
            ),
            metadata=_metadata(outcome).provider_payload(),
            timestamp=outcome.evaluated_at,
        )
        self._client.flush()
        return OutcomeScoreSubmission(score_id, outcome.trace_id)

    def close(self) -> None:
        self._client.shutdown()


def _score_id(outcome: OutcomeEvaluation) -> ScoreId:
    identity = "\0".join(
        (
            OUTCOME_SCORE_NAME,
            outcome.trace_id.value,
            outcome.task_id.value,
            outcome.verifier_id.value,
        )
    )
    return ScoreId(str(uuid5(NAMESPACE_URL, identity)))


def _metadata(outcome: OutcomeEvaluation) -> OutcomeScoreMetadata:
    return OutcomeScoreMetadata(
        schema_version=_OUTCOME_SCORE_SCHEMA_VERSION,
        task_id=outcome.task_id.value,
        verifier_id=outcome.verifier_id.value,
        normalized_score=outcome.score,
        evidence=tuple(reference.value for reference in outcome.evidence),
    )
