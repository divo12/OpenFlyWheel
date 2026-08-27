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
from ofw.evaluation.outcome import OutcomeEvaluation, TaskId, VerifierId
from ofw.observability.langfuse.domain import ScoreId, ScoreSubjectKind, TraceId
from ofw.observability.langfuse.wire import ScoreResponseWire
from ofw.runtime import EvidenceReference, VerifierVerdict


@pytest.mark.live_langfuse
def test_live_outcome_score_is_linked_to_the_trace_and_readable_by_id() -> None:
    trace_id = os.environ.get("LANGFUSE_OUTCOME_TRACE_ID")
    evaluated_at_text = os.environ.get("LANGFUSE_OUTCOME_EVALUATED_AT")
    if trace_id is None or evaluated_at_text is None:
        pytest.skip("LANGFUSE_OUTCOME_TRACE_ID and LANGFUSE_OUTCOME_EVALUATED_AT are required")
    environment = os.environ.get("LANGFUSE_OUTCOME_ENVIRONMENT", "ofw-local")
    evidence_values = os.environ.get(
        "LANGFUSE_OUTCOME_EVIDENCE",
        "integration://outcome-evidence",
    ).split(",")
    project = LangfuseProject.from_env(environment=environment)
    outcome = OutcomeEvaluation(
        trace_id=TraceId(trace_id),
        task_id=TaskId(os.environ.get("LANGFUSE_OUTCOME_TASK_ID", "integration-task")),
        verifier_id=VerifierId(
            os.environ.get("LANGFUSE_OUTCOME_VERIFIER_ID", "integration-verifier@v1")
        ),
        evaluated_at=datetime.fromisoformat(evaluated_at_text.replace("Z", "+00:00")),
        verdict=VerifierVerdict.PASS,
        score=1.0,
        evidence=tuple(EvidenceReference(value) for value in evidence_values),
    )
    store = LangfuseOutcomeStore.from_project(project)
    try:
        submission = store.store(outcome)
    finally:
        store.close()

    manifest = project.manifest()
    credentials = project.credentials()
    with httpx.Client(
        base_url=manifest.base_url.value,
        auth=(credentials.public_key, credentials.secret_key),
    ) as client:
        response = client.get(
            "/api/public/v3/scores",
            params=(
                ("id", submission.score_id.value),
                ("fields", "details,subject"),
                ("limit", "1"),
            ),
        )
        response.raise_for_status()
    page = ScoreResponseWire.model_validate_json(response.content).normalize()

    stored = next(
        record for record in page.records if record.id == ScoreId(submission.score_id.value)
    )
    assert stored.id == submission.score_id
    assert stored.name == OUTCOME_SCORE_NAME
    assert stored.value == outcome.verdict.value
    expected_timestamp = outcome.evaluated_at.replace(
        microsecond=(outcome.evaluated_at.microsecond // 1000) * 1000
    )
    assert stored.timestamp == expected_timestamp
    assert stored.subject is not None
    assert stored.subject.kind is ScoreSubjectKind.TRACE
    assert stored.subject.id == trace_id
    assert stored.metadata is not None
    metadata = TypeAdapter(OutcomeScoreMetadata).validate_json(stored.metadata.canonical)
    assert metadata.task_id == outcome.task_id.value
    assert metadata.verifier_id == outcome.verifier_id.value
    assert metadata.normalized_score == outcome.score
    assert metadata.evidence == tuple(evidence_values)
