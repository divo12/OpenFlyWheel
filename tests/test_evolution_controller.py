from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.evolution.candidate import candidate_policy_digest
from ofw.evolution.controller import (
    AdvanceEvolutionInput,
    EvolutionAdvanceAction,
    EvolutionController,
    EvolutionControllerErrorCode,
    EvolutionControllerFailure,
    EvolutionPhase,
    EvolutionStatus,
    EvolutionStopReason,
)
from ofw.evolution.gate import PromotionDecision, PromotionReason, PromotionStatus
from ofw.evolution.ledger import (
    CandidatePrepared,
    EvolutionEvent,
    EvolutionEventDraft,
    EvolutionEventType,
    EvolutionLedgerErrorCode,
    EvolutionLedgerFailure,
    FileEvolutionLedger,
    ReleasePublished,
    ReleaseRolledBack,
)
from ofw.preparation.policy import (
    ExperimentPolicyErrorCode,
    ExperimentPolicyFailure,
    ExperimentPolicySnapshot,
)
from tests.support_policy import PolicyRepository, policy


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    subprocess.run(("git", "-C", str(root), "init", "-q"), check=True)
    return root


def _controller(
    root: Path,
    *,
    max_iterations: int = 2,
    no_improvement_limit: int = 1,
) -> EvolutionController:
    from tests.support_policy import HypothesisRepository, PolicyRepository, policy

    return EvolutionController(
        workspace_root=root,
        ledger=FileEvolutionLedger(),
        policy_repository=PolicyRepository(
            policy(
                max_iterations=max_iterations, no_improvement_limit=no_improvement_limit
            )
        ),
        hypothesis_repository=HypothesisRepository(),
    )


def _request(
    root: Path,
    request_id: str,
    *,
    action: EvolutionAdvanceAction = EvolutionAdvanceAction.AUTO,
    hypothesis_id: str | None = None,
    source_commit: str | None = None,
    accepted_commit: str | None = None,
    accepted_content_id: str | None = None,
    accepted_release_id: str | None = None,
    candidate_workspace_id: str | None = None,
    candidate_id: str | None = None,
    candidate_commit: str | None = None,
    run_id: str | None = None,
    candidate_receipt_id: str | None = None,
    promotion_decision: PromotionDecision | None = None,
    release_id: str | None = None,
    stop_reason: EvolutionStopReason | None = None,
    blocker_reason: str | None = None,
    baseline_deadline_exceeded: bool = False,
    evidence_available: bool = True,
) -> AdvanceEvolutionInput:
    return AdvanceEvolutionInput(
        workspace_root=root,
        experiment_id="experiment-one",
        request_id=request_id,
        action=action,
        hypothesis_id=hypothesis_id,
        source_commit=source_commit,
        accepted_commit=accepted_commit,
        accepted_content_id=accepted_content_id,
        accepted_release_id=accepted_release_id,
        candidate_workspace_id=candidate_workspace_id,
        candidate_id=candidate_id,
        candidate_commit=candidate_commit,
        run_id=run_id,
        candidate_receipt_id=candidate_receipt_id,
        promotion_decision=promotion_decision,
        release_id=release_id,
        stop_reason=stop_reason,
        blocker_reason=blocker_reason,
        baseline_deadline_exceeded=baseline_deadline_exceeded,
        evidence_available=evidence_available,
    )


def test_controller_advances_one_deterministic_step_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    initial = controller.status("experiment-one")
    assert initial.phase is EvolutionPhase.AWAITING_HYPOTHESIS
    started = controller.advance(_request(root, "r1"))
    assert started.phase is EvolutionPhase.AWAITING_HYPOTHESIS
    assert started.status is EvolutionStatus.SUCCESS
    assert controller.advance(_request(root, "r1")) == started

    linked = controller.advance(
        _request(root, "r2", hypothesis_id="sha256:" + "a" * 64)
    )
    assert linked.phase is EvolutionPhase.AWAITING_CANDIDATE


def test_accepted_candidate_is_stuck_until_publication_package(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    controller.advance(_request(root, "r1"))
    controller.advance(_request(root, "r2", hypothesis_id="sha256:" + "a" * 64))
    controller.advance(_request(root, "r3", candidate_workspace_id="workspace-1"))
    controller.advance(
        _request(
            root, "r4", candidate_id="sha256:" + "b" * 64, candidate_commit="a" * 40
        )
    )
    running = controller.advance(
        _request(root, "r5", run_id="run-1", candidate_receipt_id="sha256:" + "c" * 64)
    )
    assert running.phase is EvolutionPhase.GATE_READY
    decision = PromotionDecision(
        decision_id="sha256:" + "d" * 64,
        policy_digest=candidate_policy_digest(policy()),
        accepted_run_id="baseline",
        candidate_run_id="run-1",
        status=PromotionStatus.ACCEPT,
        reasons=(PromotionReason.IMPROVEMENT,),
        task_ids=("task-1",),
        accepted_passes=(),
        candidate_passes=("task-1",),
        accepted_quality=0.0,
        candidate_quality=1.0,
        accepted_cost_usd=None,
        candidate_cost_usd=None,
        accepted_latency_seconds=None,
        candidate_latency_seconds=None,
        canonical_json="{}",
    )
    accepted = controller.advance(_request(root, "r6", promotion_decision=decision))
    assert accepted.phase is EvolutionPhase.AWAITING_PUBLICATION
    assert (accepted.accepted_commit, accepted.accepted_content_id) == (
        "a" * 40,
        "sha256:" + "b" * 64,
    )
    from ofw.evolution.controller import (
        EvolutionControllerErrorCode,
        EvolutionControllerFailure,
    )

    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(_request(root, "r7", release_id="release-1"))
    assert raised.value.code is EvolutionControllerErrorCode.PUBLICATION_REQUIRED
    assert (
        controller.status("experiment-one").phase is EvolutionPhase.AWAITING_PUBLICATION
    )


def test_explicit_stop_is_typed_and_terminal(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    stopped = controller.advance(
        _request(
            root,
            "stop",
            action=EvolutionAdvanceAction.STOP,
            stop_reason=EvolutionStopReason.USER_STOP,
        )
    )
    assert stopped.phase is EvolutionPhase.STOPPED
    assert stopped.stop_reason is EvolutionStopReason.USER_STOP
    assert controller.status("experiment-one") == stopped


def test_input_and_workspace_boundaries_are_strict(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AdvanceEvolutionInput(
            workspace_root=Path("relative"),
            experiment_id="experiment-one",
            request_id="request-1",
        )
    root = _repo(tmp_path)
    controller = _controller(root)
    request = _request(root / "other", "request-1")
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(request)
    assert raised.value.code is EvolutionControllerErrorCode.REQUEST_CONFLICT
    with pytest.raises(ValidationError):
        AdvanceEvolutionInput(
            workspace_root=root,
            experiment_id="experiment-one",
            request_id="stop-without-reason",
            action=EvolutionAdvanceAction.STOP,
        )


def test_baseline_and_evidence_stops_are_durable(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    baseline = controller.advance(
        _request(root, "baseline", baseline_deadline_exceeded=True)
    )
    assert baseline.phase is EvolutionPhase.STOPPED
    assert baseline.stop_reason is EvolutionStopReason.BASELINE_DEADLINE

    root = _repo(tmp_path / "second")
    controller = _controller(root)
    evidence = controller.advance(_request(root, "evidence", evidence_available=False))
    assert evidence.stop_reason is EvolutionStopReason.EVIDENCE_UNAVAILABLE


def test_missing_and_stale_hypothesis_receipts_fail_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    from tests.support_policy import PolicyRepository

    ledger = FileEvolutionLedger()
    controller = EvolutionController(
        workspace_root=root,
        ledger=ledger,
        policy_repository=PolicyRepository(policy()),
    )
    controller.advance(_request(root, "start"))
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(
            _request(root, "missing", hypothesis_id="sha256:" + "a" * 64)
        )
    assert raised.value.code is EvolutionControllerErrorCode.STALE_RECEIPT


def test_missing_candidate_input_fails_without_ledger_mutation(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    controller.advance(_request(root, "start"))
    controller.advance(_request(root, "hypothesis", hypothesis_id="sha256:" + "a" * 64))
    before = controller.status("experiment-one").sequence
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(_request(root, "candidate"))
    assert raised.value.code is EvolutionControllerErrorCode.MISSING_INPUT
    assert controller.status("experiment-one").sequence == before


def test_block_retry_and_conflicting_external_targets(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    controller.advance(_request(root, "r1"))
    controller.advance(_request(root, "r2", hypothesis_id="sha256:" + "a" * 64))
    controller.advance(_request(root, "r3", candidate_workspace_id="workspace-1"))
    controller.advance(
        _request(
            root, "r4", candidate_id="sha256:" + "b" * 64, candidate_commit="a" * 40
        )
    )
    blocked = controller.advance(
        _request(
            root,
            "r5",
            action=EvolutionAdvanceAction.BLOCK,
            blocker_reason="provider_timeout",
        )
    )
    assert blocked.phase is EvolutionPhase.BLOCKED
    retried = controller.advance(
        _request(root, "r6", action=EvolutionAdvanceAction.RETRY)
    )
    assert retried.phase is EvolutionPhase.CANDIDATE_RUNNING

    root = _repo(tmp_path / "conflict")
    controller = _controller(root)
    controller.advance(_request(root, "r1"))
    controller.advance(_request(root, "r2", hypothesis_id="sha256:" + "a" * 64))
    controller.advance(_request(root, "r3", candidate_workspace_id="workspace-1"))
    controller.advance(
        _request(
            root, "r4", candidate_id="sha256:" + "b" * 64, candidate_commit="a" * 40
        )
    )
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(
            _request(
                root, "r5", candidate_id="sha256:" + "c" * 64, candidate_commit="a" * 40
            )
        )
    assert raised.value.code is EvolutionControllerErrorCode.STALE_RECEIPT


def test_max_iterations_and_no_improvement_stops(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root, max_iterations=1, no_improvement_limit=1)
    controller.advance(_request(root, "r1"))
    controller.advance(_request(root, "r2", hypothesis_id="sha256:" + "a" * 64))
    controller.advance(_request(root, "r3", candidate_workspace_id="workspace-1"))
    controller.advance(
        _request(
            root, "r4", candidate_id="sha256:" + "b" * 64, candidate_commit="a" * 40
        )
    )
    controller.advance(
        _request(root, "r5", run_id="run-1", candidate_receipt_id="sha256:" + "c" * 64)
    )
    decision = PromotionDecision(
        decision_id="sha256:" + "d" * 64,
        policy_digest=candidate_policy_digest(
            policy(max_iterations=1, no_improvement_limit=1)
        ),
        accepted_run_id="baseline",
        candidate_run_id="run-1",
        status=PromotionStatus.REJECT,
        reasons=(PromotionReason.NO_IMPROVEMENT,),
        task_ids=("task-1",),
        accepted_passes=("task-1",),
        candidate_passes=("task-1",),
        accepted_quality=1.0,
        candidate_quality=1.0,
        accepted_cost_usd=None,
        candidate_cost_usd=None,
        accepted_latency_seconds=None,
        candidate_latency_seconds=None,
        canonical_json="{}",
    )
    stopped = controller.advance(_request(root, "r6", promotion_decision=decision))
    assert stopped.stop_reason is EvolutionStopReason.MAX_ITERATIONS


def test_policy_and_ledger_failures_are_sanitized(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    class FailingPolicy:
        def load(
            self, workspace_root: Path, experiment_id: str
        ) -> ExperimentPolicySnapshot:
            del workspace_root, experiment_id
            raise ExperimentPolicyFailure(
                ExperimentPolicyErrorCode.POLICY_INVALID, "secret-path"
            )

    with pytest.raises(EvolutionControllerFailure) as raised:
        EvolutionController(
            workspace_root=root,
            policy_repository=FailingPolicy(),
        ).status("experiment-one")
    assert raised.value.code is EvolutionControllerErrorCode.POLICY_INVALID
    assert "secret-path" not in str(raised.value)

    class FailingLedger:
        def events(
            self, workspace_root: Path, experiment_id: str
        ) -> tuple[EvolutionEvent, ...]:
            del workspace_root, experiment_id
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.CORRUPT_LEDGER, "secret-content", 4
            )

        def append(
            self, workspace_root: Path, draft: EvolutionEventDraft
        ) -> EvolutionEvent:
            del workspace_root, draft
            raise AssertionError("append must not be reached")

    with pytest.raises(EvolutionControllerFailure) as raised:
        EvolutionController(
            workspace_root=root,
            ledger=FailingLedger(),
            policy_repository=PolicyRepository(policy()),
        ).status("experiment-one")
    assert raised.value.code is EvolutionControllerErrorCode.LEDGER_INVALID
    assert "secret-content" not in str(raised.value)

    with pytest.raises(EvolutionControllerFailure) as raised:
        EvolutionController(
            workspace_root=root,
            ledger=FailingLedger(),
            policy_repository=PolicyRepository(policy()),
        ).advance(_request(root, "advance"))
    assert raised.value.code is EvolutionControllerErrorCode.LEDGER_INVALID


def test_conflicting_controller_retry_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    controller.advance(_request(root, "same"))
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(
            _request(
                root,
                "same",
                action=EvolutionAdvanceAction.STOP,
                stop_reason=EvolutionStopReason.USER_STOP,
            )
        )
    assert raised.value.code is EvolutionControllerErrorCode.REQUEST_CONFLICT


def test_illegal_action_and_stopped_state_are_typed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    controller = _controller(root)
    controller.advance(_request(root, "r1"))
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(
            _request(
                root,
                "bad",
                action=EvolutionAdvanceAction.PREPARE_CANDIDATE,
                hypothesis_id="sha256:" + "a" * 64,
            )
        )
    assert raised.value.code is EvolutionControllerErrorCode.INVALID_TRANSITION
    stopped = controller.advance(
        _request(
            root,
            "stop",
            action=EvolutionAdvanceAction.STOP,
            stop_reason=EvolutionStopReason.USER_STOP,
        )
    )
    with pytest.raises(EvolutionControllerFailure) as raised:
        controller.advance(_request(root, "after"))
    assert raised.value.code is EvolutionControllerErrorCode.STOPPED
    assert stopped.phase is EvolutionPhase.STOPPED


def test_current_accepted_identity_is_replayed_and_exposed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    accepted_commit = "b" * 40
    accepted_content_id = "sha256:" + "c" * 64
    from tests.support_policy import HypothesisRepository, PolicyRepository

    ledger = FileEvolutionLedger()
    controller = EvolutionController(
        workspace_root=root,
        ledger=ledger,
        policy_repository=PolicyRepository(policy()),
        hypothesis_repository=HypothesisRepository(accepted_commit),
    )
    started = controller.advance(
        _request(
            root,
            "start",
            accepted_commit=accepted_commit,
            accepted_content_id=accepted_content_id,
            accepted_release_id="release-a",
        )
    )
    assert (started.accepted_commit, started.accepted_content_id) == (
        accepted_commit,
        accepted_content_id,
    )
    controller.advance(
        _request(
            root,
            "hypothesis",
            hypothesis_id="sha256:" + "a" * 64,
            source_commit=accepted_commit,
        )
    )
    prepared = controller.advance(
        _request(root, "candidate", candidate_workspace_id="workspace-1")
    )
    event = ledger.events(root, "experiment-one")[-1]
    assert isinstance(event.payload, CandidatePrepared)
    assert (event.payload.source_commit, event.payload.source_content_id) == (
        accepted_commit,
        accepted_content_id,
    )
    assert (prepared.accepted_commit, prepared.accepted_content_id) == (
        accepted_commit,
        accepted_content_id,
    )

    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.RELEASE_PUBLISHED,
            experiment_id="experiment-one",
            payload=ReleasePublished(
                release_id="release-b",
                content_commit="d" * 40,
                content_id="sha256:" + "e" * 64,
                target_reached=True,
            ),
            occurred_at=datetime(2026, 9, 3, 0, 0, 1, tzinfo=UTC),
            causation_id="publish-b",
            correlation_id="publish-b",
        ),
    )
    published = controller.status("experiment-one")
    assert (published.phase, published.stop_reason, published.accepted_release_id) == (
        EvolutionPhase.STOPPED,
        EvolutionStopReason.QUALITY_TARGET,
        "release-b",
    )
    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.RELEASE_ROLLED_BACK,
            experiment_id="experiment-one",
            payload=ReleaseRolledBack(
                release_id="release-c",
                target_release_id="release-a",
                content_commit=accepted_commit,
                content_id=accepted_content_id,
            ),
            occurred_at=datetime(2026, 9, 3, 0, 0, 2, tzinfo=UTC),
            causation_id="rollback-c",
            correlation_id="rollback-c",
        ),
    )
    rolled_back = controller.status("experiment-one")
    assert (rolled_back.phase, rolled_back.accepted_commit) == (
        EvolutionPhase.AWAITING_HYPOTHESIS,
        accepted_commit,
    )
