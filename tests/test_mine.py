"""Deterministic Mine admission and immutable snapshot behavior."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ofw import (
    Harness,
    Mine,
    MineError,
    MineErrorCode,
    MiningPolicy,
    ScoreName,
    TracePartition,
    TraceQualityThreshold,
    read_snapshot_content,
)
from ofw.contracts import HarnessRevision, Sha256Digest
from ofw.diagnosis import read_snapshot
from ofw.mine import TraceSnapshot
from ofw.observability.langfuse.contracts import (
    LangfuseConnectionId,
    ObservationContentPolicy,
    TraceWindow,
)
from ofw.observability.langfuse.domain import (
    AttributionLevel,
    CollectionCapabilityReason,
    CollectionResult,
    CollectionSyncId,
    JsonDocument,
    ObservationContent,
    ObservationContentReference,
    ObservationId,
    ObservationPage,
    ObservationRecord,
    ObservationType,
    ProjectId,
    ScoreDataType,
    ScoreId,
    ScorePage,
    ScoreRecord,
    ScoreSource,
    ScoreSubject,
    ScoreSubjectKind,
    TraceGap,
    TraceId,
    TraceRecord,
)
from ofw.observability.langfuse.store import CollectionStore


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _harness(tmp_path: Path) -> Harness:
    root = tmp_path / "mine-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "memory.md").write_text("Known facts.\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("mine-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    return harness


def _observation(
    trace: str,
    revision: HarnessRevision,
    *,
    tags: tuple[str, ...] = (),
) -> ObservationRecord:
    return ObservationRecord(
        id=ObservationId(f"observation-{trace}"),
        trace_id=TraceId(trace),
        start_time=datetime(2026, 8, 22, tzinfo=UTC),
        end_time=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        project_id=ProjectId("project-1"),
        parent_observation_id=None,
        type=ObservationType.AGENT,
        is_root=True,
        name="agent-run",
        level=None,
        version="v1",
        environment="production",
        user_id=None,
        session_id=f"session-{trace}",
        created_at=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        metadata=JsonDocument(f'{{"ofw.harness.revision":"{revision.id}","secret":"token"}}'),
        usage=None,
        costs=None,
        total_cost=None,
        tags=tags,
        release=None,
        trace_name="agent-run",
        digest=Sha256Digest(f"sha256:observation-{trace}"),
    )


def _score(trace: str, value: bool, suffix: str = "") -> ScoreRecord:
    return ScoreRecord(
        id=ScoreId(f"score-{trace}{suffix}"),
        project_id=ProjectId("project-1"),
        name="correctness",
        value=value,
        data_type=ScoreDataType.BOOLEAN,
        source=ScoreSource.ANNOTATION,
        timestamp=datetime(2026, 8, 22, 0, 2, tzinfo=UTC),
        environment="production",
        created_at=datetime(2026, 8, 22, 0, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 22, 0, 2, tzinfo=UTC),
        comment="reviewed",
        metadata=None,
        subject=ScoreSubject(ScoreSubjectKind.TRACE, trace, None),
        digest=Sha256Digest(f"sha256:score-{trace}{suffix}"),
    )


def _trace(
    trace: str,
    scores: tuple[ScoreRecord, ...],
    *,
    attribution: AttributionLevel = AttributionLevel.EXACT,
    gaps: tuple[TraceGap, ...] = (),
) -> TraceRecord:
    return TraceRecord(
        id=TraceId(trace),
        observation_ids=(ObservationId(f"observation-{trace}"),),
        root_observation_ids=(ObservationId(f"observation-{trace}"),),
        score_ids=tuple(score.id for score in scores),
        session_id=f"session-{trace}",
        environment="production",
        release=None,
        attribution=attribution,
        gaps=gaps,
        digest=Sha256Digest(f"sha256:trace-{trace}"),
    )


def _collection(
    tmp_path: Path,
    revision: HarnessRevision,
    *,
    conflict: bool = False,
    foreign_good_score: bool = False,
    content: bool = False,
) -> CollectionResult:
    good_score = _score("good", True)
    if foreign_good_score:
        good_score = replace(
            good_score,
            subject=ScoreSubject(ScoreSubjectKind.TRACE, "other-trace", None),
        )
    failed_score = _score("failed", False)
    conflicting_scores: tuple[ScoreRecord, ...] = (
        (_score("ambiguous", True, "-pass"), _score("ambiguous", False, "-fail"))
        if conflict
        else ()
    )
    scores: tuple[ScoreRecord, ...] = (good_score, failed_score, *conflicting_scores)
    plain_observations: tuple[ObservationRecord, ...] = (
        _observation("good", revision),
        _observation("failed", revision),
        _observation("ambiguous", revision),
        _observation("invalid", revision, tags=("ofw-internal",)),
    )
    contents: tuple[ObservationContent, ...] = ()
    observations = plain_observations
    if content:
        captured: tuple[ObservationRecord, ...] = ()
        for observation in plain_observations:
            trace_id = observation.trace_id
            assert trace_id is not None
            input_text = f"request {trace_id.value} [REDACTED_EMAIL]"
            output_text = f"result {trace_id.value}"
            input_reference = ObservationContentReference.for_text(
                input_text,
                truncated=False,
            )
            output_reference = ObservationContentReference.for_text(
                output_text,
                truncated=False,
            )
            captured = (
                *captured,
                replace(
                    observation,
                    input_content=input_reference,
                    output_content=output_reference,
                ),
            )
            contents = (
                *contents,
                ObservationContent(input_reference, input_text),
                ObservationContent(output_reference, output_text),
            )
        observations = captured
    traces: tuple[TraceRecord, ...] = (
        _trace("good", (good_score,)),
        _trace("failed", (failed_score,)),
        _trace("ambiguous", conflicting_scores),
        _trace("invalid", (), attribution=AttributionLevel.MISSING),
    )
    observation_sync = CollectionSyncId("observations-mine")
    score_sync = CollectionSyncId("scores-mine")
    store_path = tmp_path / "collection.sqlite"
    store = CollectionStore(store_path)
    try:
        store.commit_observation_page(
            "connection-1",
            observation_sync,
            ObservationPage(observations, None, contents),
        )
        store.commit_score_page("connection-1", score_sync, ScorePage(scores, None))
    finally:
        store.close()
    start = datetime(2026, 8, 22, tzinfo=UTC)
    return CollectionResult(
        revision_id=revision.id,
        connection_id=LangfuseConnectionId("connection-1"),
        window=TraceWindow(start, start + timedelta(hours=1)),
        observation_sync_id=observation_sync,
        score_sync_id=score_sync,
        traces=traces,
        observation_count=len(observations),
        score_count=len(scores),
        gap_count=0,
        snapshot_digest=Sha256Digest("sha256:collection"),
        capability=CollectionCapabilityReason.READY,
        store_path=store_path,
        content_policy=(
            ObservationContentPolicy.redacted(
                maximum_bytes_per_field=4096,
                secret_environment_variables=(),
            )
            if content
            else ObservationContentPolicy.metadata_only()
        ),
    )


def _policy() -> MiningPolicy:
    return MiningPolicy(
        critical_scores=(ScoreName("correctness"),),
        trusted_sources=(ScoreSource.ANNOTATION,),
        quality=TraceQualityThreshold.COMPLETE,
    )


def test_mine_partitions_only_from_trusted_independent_evidence(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    result = Mine(revision, _collection(tmp_path, revision), _policy()).run()

    partitions = tuple(admission.partition for admission in result.admissions)
    assert partitions == (
        TracePartition.AMBIGUOUS,
        TracePartition.VERIFIED_FAILURE,
        TracePartition.VERIFIED_GOOD,
        TracePartition.INVALID,
    )
    assert result.verified_good_count == 1
    assert result.verified_failure_count == 1
    assert result.ambiguous_count == 1
    assert result.invalid_count == 1


def test_conflicting_trusted_scores_remain_ambiguous(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    result = Mine(
        revision,
        _collection(tmp_path, revision, conflict=True),
        _policy(),
    ).run()
    ambiguous = next(
        admission for admission in result.admissions if admission.trace_id == TraceId("ambiguous")
    )

    assert ambiguous.partition is TracePartition.AMBIGUOUS


def test_known_critical_failure_wins_over_other_missing_evidence(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    policy = MiningPolicy(
        critical_scores=(ScoreName("correctness"), ScoreName("safety")),
        trusted_sources=(ScoreSource.ANNOTATION,),
        quality=TraceQualityThreshold.COMPLETE,
    )
    result = Mine(revision, _collection(tmp_path, revision), policy).run()
    failed = next(
        admission for admission in result.admissions if admission.trace_id == TraceId("failed")
    )

    assert failed.partition is TracePartition.VERIFIED_FAILURE


def test_mine_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    collection = _collection(tmp_path, revision)

    first = Mine(revision, collection, _policy()).run()
    second = Mine(revision, collection, _policy()).run()

    assert first == second
    assert first.manifest_path.read_text(encoding="utf-8") == f"{first.to_json()}\n"
    assert all(admission.snapshot_path is not None for admission in first.admissions[:-1])
    assert first.admissions[-1].snapshot_path is None
    good = next(
        admission for admission in first.admissions if admission.trace_id == TraceId("good")
    )
    assert good.snapshot_path is not None
    snapshot = good.snapshot_path.read_text(encoding="utf-8")
    assert "token" not in snapshot
    assert "reviewed" not in snapshot
    assert read_snapshot(good, first).trace.id == TraceId("good")


def test_foreign_score_subject_cannot_label_trace(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    result = Mine(
        revision,
        _collection(tmp_path, revision, foreign_good_score=True),
        _policy(),
    ).run()
    good = next(
        admission for admission in result.admissions if admission.trace_id == TraceId("good")
    )

    assert good.partition is TracePartition.AMBIGUOUS


def test_mine_freezes_redacted_content_as_verified_artifact_references(
    tmp_path: Path,
) -> None:
    revision = _harness(tmp_path).process()
    collection = _collection(tmp_path, revision, content=True)

    result = Mine(revision, collection, _policy()).run()

    failed = next(
        admission for admission in result.admissions if admission.trace_id == TraceId("failed")
    )
    assert failed.snapshot_path is not None
    snapshot = TypeAdapter(TraceSnapshot).validate_json(failed.snapshot_path.read_bytes())
    observation = snapshot.observations[0]
    assert observation.input_content is not None
    assert observation.output_content is not None
    assert "request failed" not in failed.snapshot_path.read_text(encoding="utf-8")
    input_content = read_snapshot_content(result, observation.input_content)
    assert input_content.text == "request failed [REDACTED_EMAIL]"
    assert input_content.reference == observation.input_content.content
    collection.store_path.unlink()
    assert read_snapshot_content(result, observation.input_content) == input_content

    observation.input_content.path.write_text("tampered", encoding="utf-8")
    with pytest.raises(MineError) as raised:
        read_snapshot_content(result, observation.input_content)
    assert raised.value.code is MineErrorCode.CONTENT_INVALID


def test_source_window_is_part_of_mine_identity(tmp_path: Path) -> None:
    revision = _harness(tmp_path).process()
    collection = _collection(tmp_path, revision)
    shifted = replace(
        collection,
        window=TraceWindow(
            collection.window.start + timedelta(hours=1),
            collection.window.end + timedelta(hours=1),
        ),
    )

    first = Mine(revision, collection, _policy()).run()
    second = Mine(revision, shifted, _policy()).run()

    assert first.id != second.id


def test_processed_harness_is_accepted_and_later_connection_makes_it_stale(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    revision = harness.process()
    collection = _collection(tmp_path, revision)
    assert Mine(harness, collection, _policy()).run().revision_id == revision.id

    harness.connect_skills(Path("memory.md"))

    with pytest.raises(MineError) as raised:
        Mine(harness, collection, _policy()).run()
    assert raised.value.code is MineErrorCode.STALE_HARNESS


def test_processed_harness_is_stale_after_external_file_change(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.process()
    collection = _collection(tmp_path, revision)
    (harness.root / "prompt.md").write_text("Changed externally.\n", encoding="utf-8")

    with pytest.raises(MineError) as raised:
        Mine(harness, collection, _policy()).run()

    assert raised.value.code is MineErrorCode.STALE_HARNESS


def test_collection_from_another_revision_is_rejected(tmp_path: Path) -> None:
    first = _harness(tmp_path)
    first_revision = first.process()
    collection = _collection(tmp_path, first_revision)
    (first.root / "prompt.md").write_text("Changed.\n", encoding="utf-8")
    second_revision = first.process()

    with pytest.raises(MineError) as raised:
        Mine(second_revision, collection, _policy()).run()

    assert raised.value.code is MineErrorCode.REVISION_MISMATCH
