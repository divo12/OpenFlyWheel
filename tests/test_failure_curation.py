"""Evidence-bound cross-failure curation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.contracts import ComponentKind, Sha256Digest
from ofw.evaluation.failure import FailureEvidenceStatus, FailureType
from ofw.evaluation.failure_curation import (
    DeferredFailureInput,
    FailureCuration,
    FailureCurationErrorCode,
    FailureCurationFailure,
    FailureCurationObservation,
    FailureCurationReceipt,
    FailureCurationService,
    FailureCurationStatus,
    FailureGroupInput,
    FailureSource,
    RecordFailureCurationInput,
)
from ofw.evaluation.outcome import TaskId
from ofw.observability.langfuse.domain import ObservationId, ScoreId, TraceId

_FIRST_ARTIFACT_ID = "00000000-0000-0000-0000-000000000001"
_SECOND_ARTIFACT_ID = "00000000-0000-0000-0000-000000000002"
_THIRD_ARTIFACT_ID = "00000000-0000-0000-0000-000000000003"


class _FakeCurationGateway:
    def __init__(self, sources: tuple[FailureSource, ...]) -> None:
        self.sources = sources
        self.loaded: list[tuple[Path, tuple[str, ...]]] = []
        self.stored: list[tuple[Path, FailureCuration]] = []

    def load(self, root: Path, artifact_ids: tuple[str, ...]) -> tuple[FailureSource, ...]:
        self.loaded.append((root, artifact_ids))
        return self.sources

    def store(self, root: Path, curation: FailureCuration) -> FailureCurationReceipt:
        self.stored.append((root, curation))
        return FailureCurationReceipt(
            curation_id=curation.id,
            relative_path=Path(f".workspace/failure-curations/{curation.id}.json"),
        )


def _source(
    artifact_id: str,
    task_id: str,
    *,
    trace_id: str | None = None,
    issue_type: FailureType | None = FailureType.CONTROL_FLOW_FAILURE,
    evidence_status: FailureEvidenceStatus = FailureEvidenceStatus.SUPPORTED,
    digest_character: str | None = None,
) -> FailureSource:
    suffix = artifact_id[-1]
    return FailureSource(
        artifact_id=artifact_id,
        artifact_digest=Sha256Digest(f"sha256:{(digest_character or suffix) * 64}"),
        trace_id=TraceId(trace_id or f"trace-{suffix}"),
        task_id=TaskId(task_id),
        outcome_score_id=ScoreId(f"score-{suffix}"),
        evidence_status=evidence_status,
        issue_type=issue_type,
        critical_observation_id=(
            ObservationId(f"observation-{suffix}")
            if evidence_status is FailureEvidenceStatus.SUPPORTED
            else None
        ),
    )


def _group(*artifact_ids: str) -> FailureGroupInput:
    return FailureGroupInput(
        pattern_key="finalizes-before-verification",
        title="Finalizes before verifying state",
        mechanism="The control loop treats a successful mutation as task completion.",
        prevention="Require a state read after mutation and before finalization.",
        target_component=ComponentKind.PROMPT,
        failure_artifact_ids=artifact_ids,
    )


def _request(root: Path) -> RecordFailureCurationInput:
    return RecordFailureCurationInput(
        workspace_root=root,
        source_artifact_ids=(
            _THIRD_ARTIFACT_ID,
            _SECOND_ARTIFACT_ID,
            _FIRST_ARTIFACT_ID,
        ),
        groups=(_group(_SECOND_ARTIFACT_ID, _FIRST_ARTIFACT_ID),),
        deferred=(
            DeferredFailureInput(
                failure_artifact_id=_THIRD_ARTIFACT_ID,
                reason="No second task supports this mechanism yet.",
            ),
        ),
    )


def test_curates_repeated_failures_into_one_deterministic_actionable_group(
    tmp_path: Path,
) -> None:
    sources = (
        _source(_THIRD_ARTIFACT_ID, "task-3"),
        _source(_SECOND_ARTIFACT_ID, "task-2"),
        _source(_FIRST_ARTIFACT_ID, "task-1"),
    )
    gateway = _FakeCurationGateway(sources)
    service = FailureCurationService(gateway)
    request = _request(tmp_path)

    observation = service.record(request)

    curation = gateway.stored[0][1]
    group = curation.groups[0]
    expected = FailureCurationObservation(
        status=FailureCurationStatus.SUCCESS,
        summary="Stored one evidence-bound failure group and one deferred failure.",
        next_actions=("Use one recorded group to form the next harness hypothesis.",),
        artifacts=(str(observation.relative_path), observation.curation_id),
        curation_id=observation.curation_id,
        relative_path=observation.relative_path,
        source_failure_count=3,
        group_count=1,
        deferred_count=1,
    )
    assert (
        gateway.loaded,
        observation,
        tuple(member.task_id.value for member in group.members),
        group.issue_type,
        group.target_component,
        curation.deferred[0].source.artifact_id,
    ) == (
        [(tmp_path, request.source_artifact_ids)],
        expected,
        ("task-1", "task-2"),
        FailureType.CONTROL_FLOW_FAILURE,
        ComponentKind.PROMPT,
        _THIRD_ARTIFACT_ID,
    )


def test_curation_identity_and_order_do_not_depend_on_request_order(tmp_path: Path) -> None:
    sources = (
        _source(_FIRST_ARTIFACT_ID, "task-1"),
        _source(_SECOND_ARTIFACT_ID, "task-2"),
        _source(_THIRD_ARTIFACT_ID, "task-3"),
    )
    service = FailureCurationService(_FakeCurationGateway(sources))
    first = service.build(_request(tmp_path))
    reordered = RecordFailureCurationInput(
        workspace_root=tmp_path,
        source_artifact_ids=(
            _FIRST_ARTIFACT_ID,
            _SECOND_ARTIFACT_ID,
            _THIRD_ARTIFACT_ID,
        ),
        groups=(_group(_FIRST_ARTIFACT_ID, _SECOND_ARTIFACT_ID),),
        deferred=(
            DeferredFailureInput(
                failure_artifact_id=_THIRD_ARTIFACT_ID,
                reason="No second task supports this mechanism yet.",
            ),
        ),
    )

    assert service.build(reordered) == first


def test_curation_identity_is_bound_to_source_artifact_content(tmp_path: Path) -> None:
    original_sources = (
        _source(_FIRST_ARTIFACT_ID, "task-1"),
        _source(_SECOND_ARTIFACT_ID, "task-2"),
        _source(_THIRD_ARTIFACT_ID, "task-3"),
    )
    changed_sources = (
        _source(_FIRST_ARTIFACT_ID, "task-1", digest_character="a"),
        original_sources[1],
        original_sources[2],
    )
    request = _request(tmp_path)

    original = FailureCurationService(_FakeCurationGateway(original_sources)).build(request)
    changed = FailureCurationService(_FakeCurationGateway(changed_sources)).build(request)

    assert (changed.id, changed.groups[0].id) != (original.id, original.groups[0].id)


def test_all_deferred_curation_blocks_a_harness_hypothesis(tmp_path: Path) -> None:
    source = _source(
        _FIRST_ARTIFACT_ID,
        "task-1",
        issue_type=None,
        evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
    )
    service = FailureCurationService(_FakeCurationGateway((source,)))
    request = RecordFailureCurationInput(
        workspace_root=tmp_path,
        source_artifact_ids=(_FIRST_ARTIFACT_ID,),
        groups=(),
        deferred=(
            DeferredFailureInput(
                failure_artifact_id=_FIRST_ARTIFACT_ID,
                reason="The trace lacks the terminal state observation.",
            ),
        ),
    )

    observation = service.record(request)

    assert observation.next_actions == (
        "Do not form a harness hypothesis until a repeated supported pattern exists.",
    )


@pytest.mark.parametrize(
    ("sources", "code"),
    [
        (
            (
                _source(_FIRST_ARTIFACT_ID, "task-1"),
                _source(
                    _SECOND_ARTIFACT_ID,
                    "task-2",
                    issue_type=FailureType.TOOL_INTERACTION_FAILURE,
                ),
                _source(_THIRD_ARTIFACT_ID, "task-3"),
            ),
            FailureCurationErrorCode.MIXED_ISSUE_TYPES,
        ),
        (
            (
                _source(_FIRST_ARTIFACT_ID, "task-1"),
                _source(
                    _SECOND_ARTIFACT_ID,
                    "task-2",
                    issue_type=None,
                    evidence_status=FailureEvidenceStatus.INCONCLUSIVE,
                ),
                _source(_THIRD_ARTIFACT_ID, "task-3"),
            ),
            FailureCurationErrorCode.UNSUPPORTED_SOURCE,
        ),
        (
            (
                _source(_FIRST_ARTIFACT_ID, "task-1"),
                _source(_SECOND_ARTIFACT_ID, "task-1", trace_id="trace-other"),
                _source(_THIRD_ARTIFACT_ID, "task-3"),
            ),
            FailureCurationErrorCode.INSUFFICIENT_RECURRENCE,
        ),
    ],
)
def test_group_members_must_be_supported_same_type_failures_from_distinct_tasks(
    tmp_path: Path,
    sources: tuple[FailureSource, ...],
    code: FailureCurationErrorCode,
) -> None:
    service = FailureCurationService(_FakeCurationGateway(sources))

    with pytest.raises(FailureCurationFailure) as raised:
        service.build(_request(tmp_path))

    assert raised.value.code is code


def test_gateway_must_return_every_requested_source(tmp_path: Path) -> None:
    gateway = _FakeCurationGateway(
        (
            _source(_FIRST_ARTIFACT_ID, "task-1"),
            _source(_SECOND_ARTIFACT_ID, "task-2"),
        )
    )

    with pytest.raises(FailureCurationFailure) as raised:
        FailureCurationService(gateway).build(_request(tmp_path))

    assert raised.value.code is FailureCurationErrorCode.SOURCE_NOT_FOUND
    assert raised.value.subject == _THIRD_ARTIFACT_ID


def test_curation_input_rejects_relative_workspace() -> None:
    with pytest.raises(ValidationError):
        RecordFailureCurationInput(
            workspace_root=Path("relative"),
            source_artifact_ids=(_FIRST_ARTIFACT_ID,),
            groups=(),
            deferred=(
                DeferredFailureInput(
                    failure_artifact_id=_FIRST_ARTIFACT_ID,
                    reason="No repeated mechanism.",
                ),
            ),
        )


def test_curation_input_rejects_an_empty_source_set(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RecordFailureCurationInput(
            workspace_root=tmp_path,
            source_artifact_ids=(),
            groups=(),
            deferred=(),
        )


@pytest.mark.parametrize(
    ("sources", "groups", "deferred"),
    [
        (
            (_FIRST_ARTIFACT_ID, _FIRST_ARTIFACT_ID),
            (),
            (),
        ),
        (
            (_FIRST_ARTIFACT_ID, _SECOND_ARTIFACT_ID),
            (_group(_FIRST_ARTIFACT_ID, _SECOND_ARTIFACT_ID),),
            (DeferredFailureInput(failure_artifact_id=_FIRST_ARTIFACT_ID, reason="duplicate"),),
        ),
        (
            (_FIRST_ARTIFACT_ID, _SECOND_ARTIFACT_ID, _THIRD_ARTIFACT_ID),
            (_group(_FIRST_ARTIFACT_ID, _SECOND_ARTIFACT_ID),),
            (),
        ),
    ],
)
def test_curation_input_requires_a_unique_complete_partition(
    tmp_path: Path,
    sources: tuple[str, ...],
    groups: tuple[FailureGroupInput, ...],
    deferred: tuple[DeferredFailureInput, ...],
) -> None:
    with pytest.raises(ValidationError):
        RecordFailureCurationInput(
            workspace_root=tmp_path,
            source_artifact_ids=sources,
            groups=groups,
            deferred=deferred,
        )


def test_curation_input_forbids_unknown_fields(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(ValidationError):
        RecordFailureCurationInput.model_validate_json(
            request.model_dump_json().removesuffix("}") + ',"unexpected":"field"}'
        )
