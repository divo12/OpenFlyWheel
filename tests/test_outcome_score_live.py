"""Opt-in write/read contract check against a real Langfuse trace."""

from __future__ import annotations

import os
from datetime import datetime

import httpx
import pytest
from pydantic import TypeAdapter

from ofw import LangfuseProject
from ofw.evaluation.langfuse import (
    OUTCOME_SCORE_NAME,
    LangfuseOutcomeStore,
    OutcomeScoreMetadata,
)
from ofw.evaluation.outcome import (
    EvidenceReference,
    OutcomeEvaluation,
    TaskId,
    VerifierId,
    VerifierVerdict,
)
from ofw.observability.langfuse.domain import (
    ScoreId,
    ScoreRecord,
    ScoreSubjectKind,
    TraceId,
)
from ofw.observability.langfuse.wire import ScoreResponseWire


def _live_outcome() -> tuple[LangfuseProject, OutcomeEvaluation, tuple[str, ...]]:
    trace_id = os.environ.get("LANGFUSE_OUTCOME_TRACE_ID")
    evaluated_at_text = os.environ.get("LANGFUSE_OUTCOME_EVALUATED_AT")
    if trace_id is None or evaluated_at_text is None:
        pytest.skip("LANGFUSE_OUTCOME_TRACE_ID and LANGFUSE_OUTCOME_EVALUATED_AT are required")
    environment = os.environ.get("LANGFUSE_OUTCOME_ENVIRONMENT", "ofw-local")
    evidence_values = os.environ.get(
        "LANGFUSE_OUTCOME_EVIDENCE",
        "integration://outcome-evidence",
    ).split(",")
    return (
        LangfuseProject.from_env(environment=environment),
        OutcomeEvaluation(
            trace_id=TraceId(trace_id),
            task_id=TaskId(os.environ.get("LANGFUSE_OUTCOME_TASK_ID", "integration-task")),
            verifier_id=VerifierId(
                os.environ.get("LANGFUSE_OUTCOME_VERIFIER_ID", "integration-verifier@v1")
            ),
            evaluated_at=datetime.fromisoformat(evaluated_at_text.replace("Z", "+00:00")),
            verdict=VerifierVerdict.PASS,
            score=1.0,
            evidence=tuple(map(EvidenceReference, evidence_values)),
        ),
        tuple(evidence_values),
    )


def _read_score(project: LangfuseProject, score_id: ScoreId) -> ScoreRecord:
    manifest = project.manifest()
    credentials = project.credentials()
    with httpx.Client(
        base_url=manifest.base_url.value,
        auth=(credentials.public_key, credentials.secret_key),
    ) as client:
        response = client.get(
            "/api/public/v3/scores",
            params=(("id", score_id.value), ("fields", "details,subject"), ("limit", "1")),
        )
        response.raise_for_status()
    page = ScoreResponseWire.model_validate_json(response.content).normalize()
    stored = next((record for record in page.records if record.id == score_id), None)
    assert stored is not None, f"Langfuse score {score_id.value} was not returned"
    return stored


@pytest.mark.live_langfuse
def test_live_outcome_score_is_linked_to_the_trace_and_readable_by_id() -> None:
    project, outcome, evidence_values = _live_outcome()
    store = LangfuseOutcomeStore.from_project(project)
    try:
        submission = store.store(outcome)
    finally:
        store.close()

    stored = _read_score(project, submission.score_id)
    expected_timestamp = outcome.evaluated_at.replace(
        microsecond=(outcome.evaluated_at.microsecond // 1000) * 1000
    )
    assert stored.subject is not None
    assert (
        stored.id,
        stored.name,
        stored.value,
        stored.timestamp,
        stored.subject.kind,
        stored.subject.id,
    ) == (
        submission.score_id,
        OUTCOME_SCORE_NAME,
        outcome.verdict.value,
        expected_timestamp,
        ScoreSubjectKind.TRACE,
        outcome.trace_id.value,
    )
    assert stored.metadata is not None
    metadata = TypeAdapter(OutcomeScoreMetadata).validate_json(stored.metadata.canonical)
    assert metadata == OutcomeScoreMetadata(
        schema_version=1,
        task_id=outcome.task_id.value,
        verifier_id=outcome.verifier_id.value,
        normalized_score=outcome.score,
        evidence=evidence_values,
    )
