"""Langfuse publisher for authoritative outcome scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from langfuse import Langfuse

from ofw.evaluation.outcome import OutcomeEvaluation
from ofw.observability.langfuse.contracts import EnvironmentName, LangfuseProject
from ofw.observability.langfuse.domain import ScoreId, TraceId

OUTCOME_SCORE_NAME = "ofw.outcome"
_OUTCOME_SCORE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class OutcomeScoreMetadata:
    schema_version: int
    task_id: str
    verifier_id: str
    normalized_score: float | None
    evidence: tuple[str, ...]


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
        metadata: OutcomeScoreMetadata,
        timestamp: datetime,
        environment: str,
    ) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class LangfuseOutcomeStore:
    """Publish outcome evaluations without changing trace-query behavior."""

    def __init__(self, client: _OutcomeScoreClient, *, environment: str) -> None:
        self._client = client
        self._environment = EnvironmentName(environment).value

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
        return cls(client, environment=manifest.environment.value)

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
            metadata=_metadata(outcome),
            timestamp=outcome.evaluated_at,
            environment=self._environment,
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
