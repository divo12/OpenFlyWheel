"""Resumable one-step evolution controller over the typed event ledger."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, model_validator

from ofw.evaluation.outcome import EvaluatedRunReceipt, RunSide
from ofw.evolution.candidate import candidate_policy_digest
from ofw.evolution.gate import PromotionDecision, PromotionStatus, decide_promotion
from ofw.evolution.hypothesis import HarnessHypothesis, HypothesisFailure
from ofw.evolution.hypothesis_repository import FileHypothesisRepository
from ofw.evolution.ledger import (
    CandidateAccepted,
    CandidatePrepared,
    CandidateRejected,
    CandidateSubmitted,
    EvolutionEvent,
    EvolutionEventDraft,
    EvolutionEventPayload,
    EvolutionEventType,
    EvolutionLedgerFailure,
    EvolutionStarted,
    EvolutionStopped,
    EvolutionStopReason,
    ExternalOperation,
    ExternalOperationBlocked,
    ExternalOperationIntent,
    FileEvolutionLedger,
    GateDecided,
    HypothesisLinked,
    ReleasePublished,
    ReleaseRolledBack,
    RunCompleted,
    RunStarted,
)
from ofw.evolution.publication import PublicationFailure, PublicationService
from ofw.preparation.contracts import StrictModel
from ofw.preparation.policy import (
    ExperimentPolicyFailure,
    ExperimentPolicySnapshot,
    FileExperimentPolicyRepository,
)

_DIGEST = r"sha256:[0-9a-f]{64}"
_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:@/-]*"

__all__ = [
    "AdvanceEvolutionInput",
    "EvolutionAdvanceAction",
    "EvolutionController",
    "EvolutionControllerErrorCode",
    "EvolutionControllerFailure",
    "EvolutionObservation",
    "EvolutionPhase",
    "EvolutionStatus",
    "EvolutionStopReason",
]


class EvolutionPhase(StrEnum):
    AWAITING_HYPOTHESIS = "awaiting_hypothesis"
    AWAITING_CANDIDATE = "awaiting_candidate"
    CANDIDATE_RUNNING = "candidate_running"
    GATE_READY = "gate_ready"
    AWAITING_PUBLICATION = "awaiting_publication"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class EvolutionAdvanceAction(StrEnum):
    AUTO = "auto"
    LINK_HYPOTHESIS = "link_hypothesis"
    PREPARE_CANDIDATE = "prepare_candidate"
    SUBMIT_CANDIDATE = "submit_candidate"
    COMPLETE_RUN = "complete_run"
    DECIDE_GATE = "decide_gate"
    RETRY = "retry"
    BLOCK = "block"
    STOP = "stop"
    PUBLISH = "publish"


class EvolutionStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class EvolutionControllerErrorCode(StrEnum):
    POLICY_INVALID = "policy_invalid"
    LEDGER_INVALID = "ledger_invalid"
    REQUEST_CONFLICT = "request_conflict"
    INVALID_TRANSITION = "invalid_transition"
    MISSING_INPUT = "missing_input"
    STALE_RECEIPT = "stale_receipt"
    PUBLICATION_REQUIRED = "publication_required"
    STOPPED = "stopped"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    MAX_ITERATIONS = "max_iterations"
    NO_IMPROVEMENT = "no_improvement"
    PUBLICATION_FAILED = "publication_failed"


class EvolutionControllerFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: EvolutionControllerErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class AdvanceEvolutionInput(StrictModel):
    workspace_root: Path
    experiment_id: str = Field(
        min_length=1, max_length=80, pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*"
    )
    request_id: str = Field(min_length=1, max_length=256, pattern=_IDENTIFIER)
    action: EvolutionAdvanceAction = EvolutionAdvanceAction.AUTO
    hypothesis_id: str | None = Field(default=None, pattern=_DIGEST)
    source_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    accepted_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    accepted_content_id: str | None = Field(default=None, pattern=_DIGEST)
    accepted_release_id: str | None = Field(
        default=None, max_length=256, pattern=_IDENTIFIER
    )
    candidate_workspace_id: str | None = Field(
        default=None, max_length=256, pattern=_IDENTIFIER
    )
    candidate_id: str | None = Field(default=None, pattern=_DIGEST)
    candidate_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    run_id: str | None = Field(default=None, max_length=256, pattern=_IDENTIFIER)
    candidate_receipt_id: str | None = Field(default=None, pattern=_DIGEST)
    evaluated_run_receipt: EvaluatedRunReceipt | None = None
    accepted_run_receipt: EvaluatedRunReceipt | None = None
    promotion_decision: PromotionDecision | None = None
    release_id: str | None = Field(default=None, max_length=256, pattern=_IDENTIFIER)
    stop_reason: EvolutionStopReason | None = None
    blocker_reason: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=_IDENTIFIER
    )
    baseline_deadline_exceeded: bool = False
    evidence_available: bool = True
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_root(self) -> AdvanceEvolutionInput:
        if not self.workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if self.action is EvolutionAdvanceAction.STOP and self.stop_reason is None:
            raise ValueError("stop_reason is required")
        return self

    def digest(self) -> str:
        content = self.model_dump_json(exclude={"requested_at"})
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class EvolutionObservation(StrictModel):
    experiment_id: str = Field(min_length=1, max_length=80)
    status: EvolutionStatus
    phase: EvolutionPhase
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=3)
    sequence: int = Field(strict=True, ge=0)
    iteration: int = Field(strict=True, ge=0, le=100)
    hypothesis_id: str | None = Field(default=None, pattern=_DIGEST)
    candidate_workspace_id: str | None = Field(default=None, max_length=256)
    candidate_id: str | None = Field(default=None, pattern=_DIGEST)
    run_id: str | None = Field(default=None, max_length=256)
    decision_id: str | None = Field(default=None, pattern=_DIGEST)
    accepted_release_id: str | None = Field(default=None, max_length=256)
    accepted_commit: str | None = Field(default=None, pattern=r"[0-9a-f]{40}")
    accepted_content_id: str | None = Field(default=None, pattern=_DIGEST)
    stop_reason: EvolutionStopReason | None = None
    error_code: EvolutionControllerErrorCode | None = None


class EvolutionPolicyRepository(Protocol):
    def load(
        self, workspace_root: Path, experiment_id: str
    ) -> ExperimentPolicySnapshot: ...


class EvolutionHypothesisRepository(Protocol):
    def load(self, workspace_root: Path, hypothesis_id: str) -> HarnessHypothesis: ...


class EvolutionLedger(Protocol):
    def events(
        self, workspace_root: Path, experiment_id: str
    ) -> tuple[EvolutionEvent, ...]: ...

    def append(
        self, workspace_root: Path, draft: EvolutionEventDraft
    ) -> EvolutionEvent: ...


@dataclass(frozen=True, slots=True)
class _EvolutionState:
    phase: EvolutionPhase = EvolutionPhase.AWAITING_HYPOTHESIS
    iteration: int = 0
    hypothesis_id: str | None = None
    candidate_workspace_id: str | None = None
    candidate_id: str | None = None
    run_id: str | None = None
    candidate_receipt_id: str | None = None
    decision_id: str | None = None
    gate_status: PromotionStatus | None = None
    accepted_release_id: str | None = None
    stop_reason: EvolutionStopReason | None = None
    accepted_commit: str | None = None
    accepted_content_id: str | None = None
    candidate_commit: str | None = None


class EvolutionController:
    def __init__(
        self,
        *,
        workspace_root: Path,
        ledger: EvolutionLedger | None = None,
        policy_repository: EvolutionPolicyRepository | None = None,
        hypothesis_repository: EvolutionHypothesisRepository | None = None,
        publication: PublicationService | None = None,
    ) -> None:
        if not workspace_root.is_absolute():
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.POLICY_INVALID,
                "workspace_root",
            )
        self._workspace_root = workspace_root
        self._ledger = ledger or FileEvolutionLedger()
        self._policies = policy_repository or FileExperimentPolicyRepository()
        self._hypotheses = hypothesis_repository or FileHypothesisRepository()
        self._publication = publication or PublicationService(self._ledger)

    def status(self, experiment_id: str) -> EvolutionObservation:
        policy = self._policy(experiment_id)
        try:
            events = self._ledger.events(self._workspace_root, experiment_id)
        except EvolutionLedgerFailure as error:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.LEDGER_INVALID,
                experiment_id,
            ) from error
        state = _accepted_identity(_reduce(events), policy)
        return _observation(experiment_id, state, events[-1].sequence if events else 0)

    def advance(self, request: AdvanceEvolutionInput) -> EvolutionObservation:
        if request.workspace_root != self._workspace_root:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.REQUEST_CONFLICT,
                request.experiment_id,
            )
        policy = self._policy(request.experiment_id)
        events = self._events(request.experiment_id)
        replayed = self._replay_request(request, policy, events)
        if replayed is not None:
            return replayed
        state = _reduce(events)
        return self._advance_after_replay(request, policy, state, events)

    def _advance_after_replay(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
        events: tuple[EvolutionEvent, ...],
    ) -> EvolutionObservation:
        if state.phase is EvolutionPhase.STOPPED:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STOPPED, request.experiment_id
            )
        special = self._special_advance(request, state)
        if special is not None:
            return special
        if not events:
            return self._start(request, policy)
        return self._advance_phase(request, policy, state)

    def _events(self, experiment_id: str) -> tuple[EvolutionEvent, ...]:
        try:
            return self._ledger.events(self._workspace_root, experiment_id)
        except EvolutionLedgerFailure as error:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.LEDGER_INVALID, experiment_id
            ) from error

    def _replay_request(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        events: tuple[EvolutionEvent, ...],
    ) -> EvolutionObservation | None:
        prior = _request_event(events, request.request_id)
        if prior is None:
            return None
        if prior.request_digest != request.digest():
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.REQUEST_CONFLICT, request.request_id
            )
        resumed = self._resume_after_crash(request, policy, _reduce(events), prior)
        if resumed is not None:
            return resumed
        return _observation(request.experiment_id, _reduce(events), prior.sequence)

    def _special_advance(
        self,
        request: AdvanceEvolutionInput,
        state: _EvolutionState,
    ) -> EvolutionObservation | None:
        if (
            state.phase is EvolutionPhase.AWAITING_PUBLICATION
            and request.action is EvolutionAdvanceAction.BLOCK
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.PUBLICATION_REQUIRED,
                request.experiment_id,
            )
        if request.action is EvolutionAdvanceAction.STOP:
            return self._stop(request, state)
        if request.baseline_deadline_exceeded:
            return self._stop_with_reason(
                request, state, EvolutionStopReason.BASELINE_DEADLINE
            )
        if not request.evidence_available:
            return self._stop_with_reason(
                request, state, EvolutionStopReason.EVIDENCE_UNAVAILABLE
            )
        if request.action is EvolutionAdvanceAction.BLOCK:
            return self._block(request, state)
        return None

    def _advance_phase(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        if state.phase is EvolutionPhase.AWAITING_HYPOTHESIS:
            return self._link_hypothesis(request, policy, state)
        if state.phase is EvolutionPhase.AWAITING_CANDIDATE:
            return self._candidate(request, policy, state)
        if state.phase is EvolutionPhase.CANDIDATE_RUNNING:
            return self._complete_run(request, policy, state)
        return self._advance_gate_or_wait(request, policy, state)

    def _advance_gate_or_wait(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        if state.phase is EvolutionPhase.GATE_READY:
            return self._decide(request, policy, state)
        if state.phase in (EvolutionPhase.AWAITING_PUBLICATION, EvolutionPhase.BLOCKED):
            return self._advance_waiting(request, state)
        return self._invalid_phase(state)

    def _invalid_phase(self, state: _EvolutionState) -> EvolutionObservation:
        raise EvolutionControllerFailure(
            EvolutionControllerErrorCode.INVALID_TRANSITION,
            state.phase.value,
        )

    def _advance_waiting(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> EvolutionObservation:
        if state.phase is EvolutionPhase.AWAITING_PUBLICATION:
            if request.action is EvolutionAdvanceAction.PUBLISH:
                return self._publish(request, state)
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.PUBLICATION_REQUIRED,
                request.experiment_id,
            )
        return self._retry(request, state)

    def _publish(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> EvolutionObservation:
        if (
            state.candidate_commit is None
            or request.release_id is None
            or request.promotion_decision is None
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT,
                "publication",
            )
        try:
            policy = self._policy(request.experiment_id)
            current = self._publication.current_accepted(
                self._workspace_root,
                request.experiment_id,
                candidate_policy_digest(policy),
            )
            self._publication.promote(
                root=self._workspace_root,
                experiment_id=request.experiment_id,
                policy_digest=current.policy_digest,
                operation_id=request.digest(),
                publication_id=request.release_id,
                expected=current.cas_token,
                candidate_commit=state.candidate_commit,
                candidate_tree=self._publication.commit_tree(
                    self._workspace_root, state.candidate_commit
                ),
                gate=request.promotion_decision,
            )
        except PublicationFailure:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.PUBLICATION_FAILED,
                request.experiment_id,
            ) from None
        return self.status(request.experiment_id)

    def _resume_after_crash(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
        prior: EvolutionEvent,
    ) -> EvolutionObservation | None:
        if prior.event_type is EvolutionEventType.GATE_DECIDED:
            return self._decide(request, policy, state)
        if prior.event_type is EvolutionEventType.EXTERNAL_OPERATION_INTENT:
            return self._resume_intent(request, policy, state)
        return None

    def _resume_intent(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation | None:
        if state.phase is EvolutionPhase.AWAITING_CANDIDATE:
            return self._candidate(request, policy, state)
        if state.phase is EvolutionPhase.CANDIDATE_RUNNING and (
            request.run_id is not None or request.evaluated_run_receipt is not None
        ):
            return self._complete_run(request, policy, state)
        return None

    def _policy(self, experiment_id: str) -> ExperimentPolicySnapshot:
        try:
            return self._policies.load(self._workspace_root, experiment_id)
        except (ExperimentPolicyFailure, ValueError):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.POLICY_INVALID,
                experiment_id,
            ) from None

    def _start(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
    ) -> EvolutionObservation:
        accepted_commit = request.accepted_commit or policy.initialization_commit
        accepted_content_id = request.accepted_content_id or _content_identity(
            accepted_commit
        )
        self._append(
            request,
            EvolutionEventType.EVOLUTION_STARTED,
            EvolutionStarted(
                policy_digest=candidate_policy_digest(policy),
                accepted_commit=accepted_commit,
                accepted_content_id=accepted_content_id,
                accepted_release_id=request.accepted_release_id,
            ),
        )
        return self.status(request.experiment_id)

    def _link_hypothesis(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        hypothesis_id, source_commit = self._validate_hypothesis(request, policy, state)
        self._append(
            request,
            EvolutionEventType.HYPOTHESIS_LINKED,
            HypothesisLinked(
                hypothesis_id=hypothesis_id,
                source_commit=source_commit,
            ),
        )
        return self.status(request.experiment_id)

    def _validate_hypothesis(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> tuple[str, str]:
        if request.hypothesis_id is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT, "hypothesis"
            )
        hypothesis_id = request.hypothesis_id
        if request.action not in (
            EvolutionAdvanceAction.AUTO,
            EvolutionAdvanceAction.LINK_HYPOTHESIS,
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.INVALID_TRANSITION, state.phase.value
            )
        hypothesis = self._load_hypothesis(hypothesis_id)
        expected_commit, _ = self._accepted_source(request, policy, state)
        self._validate_hypothesis_identity(
            request, expected_commit, hypothesis, hypothesis_id
        )
        return hypothesis_id, request.source_commit or hypothesis.source_commit

    def _load_hypothesis(self, hypothesis_id: str) -> HarnessHypothesis:
        try:
            return self._hypotheses.load(self._workspace_root, hypothesis_id)
        except HypothesisFailure:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, hypothesis_id
            ) from None

    def _validate_hypothesis_identity(
        self,
        request: AdvanceEvolutionInput,
        expected_commit: str,
        hypothesis: HarnessHypothesis,
        hypothesis_id: str,
    ) -> None:
        source_commit = request.source_commit or hypothesis.source_commit
        if (
            hypothesis.experiment_id != request.experiment_id
            or source_commit != hypothesis.source_commit
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, hypothesis_id
            )
        if source_commit != expected_commit:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, hypothesis_id
            )

    def _candidate(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        if request.candidate_workspace_id is not None:
            if request.action not in (
                EvolutionAdvanceAction.AUTO,
                EvolutionAdvanceAction.PREPARE_CANDIDATE,
            ):
                raise EvolutionControllerFailure(
                    EvolutionControllerErrorCode.INVALID_TRANSITION,
                    state.phase.value,
                )
            return self._prepare_candidate(request, policy, state)
        if request.action not in (
            EvolutionAdvanceAction.AUTO,
            EvolutionAdvanceAction.SUBMIT_CANDIDATE,
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.INVALID_TRANSITION,
                state.phase.value,
            )
        return self._submit_candidate(request, state)

    def _prepare_candidate(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        workspace_id = request.candidate_workspace_id
        if workspace_id is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT, "candidate_workspace"
            )
        if state.candidate_workspace_id is not None:
            if state.candidate_workspace_id != workspace_id:
                raise EvolutionControllerFailure(
                    EvolutionControllerErrorCode.REQUEST_CONFLICT, workspace_id
                )
            return self.status(request.experiment_id)
        if state.iteration >= policy.max_iterations:
            return self._stop_with_reason(
                request, state, EvolutionStopReason.MAX_ITERATIONS
            )
        source_commit, source_content_id = self._accepted_source(request, policy, state)
        key = _operation_key(
            request.experiment_id, "candidate-prepare", state.iteration
        )
        self._ensure_candidate_intent(request, key, workspace_id)
        self._append(
            request,
            EvolutionEventType.CANDIDATE_PREPARED,
            CandidatePrepared(
                iteration=state.iteration + 1,
                candidate_workspace_id=workspace_id,
                source_commit=source_commit,
                source_content_id=source_content_id,
                source_release_id=state.accepted_release_id,
            ),
        )
        return self.status(request.experiment_id)

    def _accepted_source(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> tuple[str, str]:
        events = self._events(request.experiment_id)
        if not _has_publication(events):
            commit = state.accepted_commit or policy.initialization_commit
            return commit, state.accepted_content_id or _content_identity(commit)
        try:
            current = self._publication.current_accepted(
                self._workspace_root,
                request.experiment_id,
                candidate_policy_digest(policy),
            )
        except PublicationFailure:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.PUBLICATION_FAILED,
                request.experiment_id,
            ) from None
        return current.content_commit, _tree_content_identity(current.content_tree)

    def _ensure_candidate_intent(
        self, request: AdvanceEvolutionInput, key: str, target: str
    ) -> None:
        events = self._events(request.experiment_id)
        intent = _find_intent(events, key, ExternalOperation.CANDIDATE)
        if intent is not None and intent.target != target:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.REQUEST_CONFLICT, target
            )
        if intent is None:
            self._append(
                request,
                EvolutionEventType.EXTERNAL_OPERATION_INTENT,
                ExternalOperationIntent(
                    operation=ExternalOperation.CANDIDATE,
                    idempotency_key=key,
                    target=target,
                ),
            )

    def _submit_candidate(
        self,
        request: AdvanceEvolutionInput,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        if request.candidate_id is None or request.candidate_commit is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT, "candidate"
            )
        if state.candidate_workspace_id is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.INVALID_TRANSITION, "candidate"
            )
        key = _operation_key(request.experiment_id, "candidate", state.iteration)
        self._ensure_candidate_intent(request, key, request.candidate_id)
        self._append(
            request,
            EvolutionEventType.CANDIDATE_SUBMITTED,
            CandidateSubmitted(
                candidate_id=request.candidate_id,
                candidate_commit=request.candidate_commit,
            ),
        )
        return self.status(request.experiment_id)

    def _complete_run(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        run_id, receipt_id = self._run_details(request, policy, state)
        return self._record_run(request, state, run_id, receipt_id)

    def _run_details(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> tuple[str, str]:
        self._validate_run_action(request, state)
        run_id, receipt_id = _run_values(request, policy, state)
        if state.run_id is not None and state.run_id != run_id:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, run_id
            )
        return run_id, receipt_id

    def _validate_run_action(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> None:
        if (
            request.candidate_id is not None
            and request.candidate_id != state.candidate_id
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, request.candidate_id
            )
        if state.candidate_id is None or request.action not in (
            EvolutionAdvanceAction.AUTO,
            EvolutionAdvanceAction.COMPLETE_RUN,
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.INVALID_TRANSITION,
                state.phase.value,
            )

    def _record_run(
        self,
        request: AdvanceEvolutionInput,
        state: _EvolutionState,
        run_id: str,
        receipt_id: str,
    ) -> EvolutionObservation:
        key = _operation_key(request.experiment_id, "harbor", state.iteration)
        events = self._events(request.experiment_id)
        intent = _find_intent(events, key, ExternalOperation.HARBOR)
        if intent is not None and intent.target != run_id:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.REQUEST_CONFLICT, run_id
            )
        if intent is None:
            self._append(
                request,
                EvolutionEventType.EXTERNAL_OPERATION_INTENT,
                ExternalOperationIntent(
                    operation=ExternalOperation.HARBOR,
                    idempotency_key=key,
                    target=run_id,
                ),
            )
        self._append(
            request,
            EvolutionEventType.RUN_COMPLETED,
            RunCompleted(run_id=run_id, receipt_id=receipt_id),
        )
        return self.status(request.experiment_id)

    def _decide(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> EvolutionObservation:
        decision, reasons = self._validate_decision(request, policy, state)
        if state.decision_id != decision.decision_id:
            self._append(
                request,
                EvolutionEventType.GATE_DECIDED,
                GateDecided(
                    decision_id=decision.decision_id,
                    candidate_run_id=decision.candidate_run_id,
                    status=decision.status,
                    reasons=reasons,
                ),
            )
        return self._finish_decision(request, policy, state, decision, reasons)

    def _validate_decision(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> tuple[PromotionDecision, tuple[str, ...]]:
        decision = request.promotion_decision
        if decision is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT, "promotion_decision"
            )
        self._validate_decision_identity(decision, policy, state)
        candidate = request.evaluated_run_receipt
        accepted = request.accepted_run_receipt
        if candidate is None or accepted is None:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.MISSING_INPUT, "gate_receipts"
            )
        if candidate.receipt_id != state.candidate_receipt_id:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, candidate.receipt_id
            )
        if decide_promotion(policy, accepted, candidate) != decision:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, decision.decision_id
            )
        return decision, tuple(reason.value for reason in decision.reasons)

    def _validate_decision_identity(
        self,
        decision: PromotionDecision,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
    ) -> None:
        if state.run_id is None or decision.candidate_run_id != state.run_id:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, decision.candidate_run_id
            )
        if decision.policy_digest != candidate_policy_digest(policy):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, decision.decision_id
            )
        if decision.decision_id != decision.recomputed_id():
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, decision.decision_id
            )

    def _finish_decision(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
        decision: PromotionDecision,
        reasons: tuple[str, ...],
    ) -> EvolutionObservation:
        if decision.status is PromotionStatus.INCONCLUSIVE:
            return self._block(request, state)
        if decision.status is PromotionStatus.ACCEPT:
            self._append(
                request,
                EvolutionEventType.CANDIDATE_ACCEPTED,
                CandidateAccepted(
                    candidate_id=state.candidate_id or "sha256:" + "0" * 64,
                    decision_id=decision.decision_id,
                    candidate_commit=state.candidate_commit,
                    accepted_content_id=state.candidate_id,
                ),
            )
            return self.status(request.experiment_id)
        return self._reject_candidate(request, policy, state, decision, reasons)

    def _reject_candidate(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
        decision: PromotionDecision,
        reasons: tuple[str, ...],
    ) -> EvolutionObservation:
        self._append(
            request,
            EvolutionEventType.CANDIDATE_REJECTED,
            CandidateRejected(
                candidate_id=state.candidate_id or "sha256:" + "0" * 64,
                decision_id=decision.decision_id,
                reasons=reasons or ("no_improvement",),
            ),
        )
        rejects = sum(
            1
            for event in self._events(request.experiment_id)
            if event.event_type is EvolutionEventType.CANDIDATE_REJECTED
        )
        return self._finish_rejection(request, policy, state, reasons, rejects)

    def _finish_rejection(
        self,
        request: AdvanceEvolutionInput,
        policy: ExperimentPolicySnapshot,
        state: _EvolutionState,
        reasons: tuple[str, ...],
        rejects: int,
    ) -> EvolutionObservation:
        stop_reason = _rejection_stop_reason(policy, state, reasons, rejects)
        if stop_reason is not None:
            return self._stop_with_reason(request, state, stop_reason)
        return self.status(request.experiment_id)

    def _retry(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> EvolutionObservation:
        if request.action is not EvolutionAdvanceAction.RETRY:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.INVALID_TRANSITION, state.phase.value
            )
        run_id = state.run_id or f"run-{state.iteration}"
        self._append(
            request,
            EvolutionEventType.RUN_STARTED,
            RunStarted(
                run_id=run_id,
                idempotency_key=_operation_key(
                    request.experiment_id, "harbor", state.iteration
                ),
            ),
        )
        return self.status(request.experiment_id)

    def _block(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> EvolutionObservation:
        reason = request.blocker_reason or EvolutionStopReason.BLOCKED.value
        self._append(
            request,
            EvolutionEventType.EXTERNAL_OPERATION_BLOCKED,
            ExternalOperationBlocked(
                operation=ExternalOperation.HARBOR,
                idempotency_key=_operation_key(
                    request.experiment_id, "harbor", state.iteration
                ),
                reason=reason,
            ),
        )
        return self.status(request.experiment_id)

    def _stop(
        self, request: AdvanceEvolutionInput, state: _EvolutionState
    ) -> EvolutionObservation:
        return self._stop_with_reason(
            request, state, request.stop_reason or EvolutionStopReason.USER_STOP
        )

    def _stop_with_reason(
        self,
        request: AdvanceEvolutionInput,
        state: _EvolutionState,
        reason: EvolutionStopReason,
    ) -> EvolutionObservation:
        del state
        self._append(
            request,
            EvolutionEventType.EVOLUTION_STOPPED,
            EvolutionStopped(reason=reason),
        )
        return self.status(request.experiment_id)

    def _append(
        self,
        request: AdvanceEvolutionInput,
        event_type: EvolutionEventType,
        payload: EvolutionEventPayload,
    ) -> EvolutionEvent:
        try:
            return self._ledger.append(
                self._workspace_root,
                EvolutionEventDraft(
                    event_type=event_type,
                    experiment_id=request.experiment_id,
                    payload=payload,
                    occurred_at=request.requested_at,
                    causation_id=request.request_id,
                    correlation_id=request.request_id,
                    request_digest=request.digest(),
                ),
            )
        except EvolutionLedgerFailure as error:
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.LEDGER_INVALID, request.experiment_id
            ) from error


def _run_values(
    request: AdvanceEvolutionInput,
    policy: ExperimentPolicySnapshot,
    state: _EvolutionState,
) -> tuple[str, str]:
    receipt = request.evaluated_run_receipt
    if receipt is not None:
        if (
            receipt.side is not RunSide.CANDIDATE
            or receipt.policy_digest != candidate_policy_digest(policy)
            or receipt.controls_digest != policy.controls_digest
            or (
                state.candidate_commit is not None
                and receipt.evaluated_commit != state.candidate_commit
            )
        ):
            raise EvolutionControllerFailure(
                EvolutionControllerErrorCode.STALE_RECEIPT, receipt.receipt_id
            )
        return receipt.run_id, receipt.receipt_id
    if request.run_id is None or request.candidate_receipt_id is None:
        raise EvolutionControllerFailure(
            EvolutionControllerErrorCode.MISSING_INPUT, "run"
        )
    return request.run_id, request.candidate_receipt_id


def _rejection_stop_reason(
    policy: ExperimentPolicySnapshot,
    state: _EvolutionState,
    reasons: tuple[str, ...],
    rejects: int,
) -> EvolutionStopReason | None:
    if state.iteration >= policy.max_iterations:
        return EvolutionStopReason.MAX_ITERATIONS
    if "cost_limit_exceeded" in reasons:
        return EvolutionStopReason.COST_LIMIT
    if "latency_limit_exceeded" in reasons:
        return EvolutionStopReason.LATENCY_LIMIT
    if rejects >= policy.no_improvement_limit:
        return EvolutionStopReason.NO_IMPROVEMENT
    return None


def _reduce(events: tuple[EvolutionEvent, ...]) -> _EvolutionState:
    state = _EvolutionState()
    for event in events:
        state = _apply_event(event, state)
    return state


def _apply_event(event: EvolutionEvent, state: _EvolutionState) -> _EvolutionState:
    payload = event.payload
    if isinstance(payload, EvolutionStarted):
        return replace(
            state,
            accepted_commit=payload.accepted_commit,
            accepted_content_id=payload.accepted_content_id,
            accepted_release_id=payload.accepted_release_id,
        )
    if isinstance(payload, (ReleasePublished, ReleaseRolledBack)):
        return _apply_release(payload, state)
    if isinstance(payload, (HypothesisLinked, CandidatePrepared)):
        return _apply_candidate_preparation(payload, state)
    if isinstance(payload, (CandidateSubmitted, RunStarted)):
        return _apply_run_start(payload, state)
    if isinstance(payload, (RunCompleted, GateDecided, CandidateAccepted)):
        return _apply_progress(payload, state)
    if isinstance(
        payload, (CandidateRejected, EvolutionStopped, ExternalOperationBlocked)
    ):
        return _apply_terminal(payload, state)
    return state


def _apply_release(
    payload: ReleasePublished | ReleaseRolledBack, state: _EvolutionState
) -> _EvolutionState:
    if isinstance(payload, ReleasePublished):
        target_reached = payload.target_reached
        return replace(
            state,
            phase=(
                EvolutionPhase.STOPPED
                if target_reached
                else EvolutionPhase.AWAITING_HYPOTHESIS
            ),
            accepted_release_id=payload.release_id,
            accepted_commit=payload.content_commit or state.accepted_commit,
            accepted_content_id=payload.content_id or state.accepted_content_id,
            stop_reason=(
                EvolutionStopReason.QUALITY_TARGET if target_reached else None
            ),
            hypothesis_id=None,
            candidate_workspace_id=None,
            candidate_id=None,
            candidate_commit=None,
            run_id=None,
            candidate_receipt_id=None,
            decision_id=None,
            gate_status=None,
        )
    return replace(
        state,
        phase=EvolutionPhase.AWAITING_HYPOTHESIS,
        accepted_release_id=payload.release_id,
        accepted_commit=payload.content_commit or state.accepted_commit,
        accepted_content_id=payload.content_id or state.accepted_content_id,
        stop_reason=None,
        hypothesis_id=None,
        candidate_workspace_id=None,
        candidate_id=None,
        candidate_commit=None,
        run_id=None,
        candidate_receipt_id=None,
        decision_id=None,
        gate_status=None,
    )


def _apply_candidate_preparation(
    payload: HypothesisLinked | CandidatePrepared, state: _EvolutionState
) -> _EvolutionState:
    if isinstance(payload, HypothesisLinked):
        return replace(
            state,
            phase=EvolutionPhase.AWAITING_CANDIDATE,
            hypothesis_id=payload.hypothesis_id,
            candidate_workspace_id=None,
            candidate_id=None,
            candidate_commit=None,
            run_id=None,
            candidate_receipt_id=None,
            decision_id=None,
            gate_status=None,
        )
    return replace(
        state,
        phase=EvolutionPhase.AWAITING_CANDIDATE,
        iteration=payload.iteration,
        candidate_workspace_id=payload.candidate_workspace_id,
    )


def _apply_run_start(
    payload: CandidateSubmitted | RunStarted, state: _EvolutionState
) -> _EvolutionState:
    if isinstance(payload, CandidateSubmitted):
        return replace(
            state,
            phase=EvolutionPhase.CANDIDATE_RUNNING,
            candidate_id=payload.candidate_id,
            candidate_commit=payload.candidate_commit,
        )
    return replace(
        state,
        phase=EvolutionPhase.CANDIDATE_RUNNING,
        run_id=payload.run_id,
    )


def _apply_progress(
    payload: RunCompleted | GateDecided | CandidateAccepted, state: _EvolutionState
) -> _EvolutionState:
    if isinstance(payload, RunCompleted):
        return replace(
            state,
            phase=EvolutionPhase.GATE_READY,
            run_id=payload.run_id,
            candidate_receipt_id=payload.receipt_id,
        )
    if isinstance(payload, GateDecided):
        return replace(
            state,
            phase=EvolutionPhase.GATE_READY,
            decision_id=payload.decision_id,
            gate_status=payload.status,
        )
    return replace(
        state,
        phase=EvolutionPhase.AWAITING_PUBLICATION,
        candidate_id=payload.candidate_id,
        decision_id=payload.decision_id,
        candidate_commit=payload.candidate_commit or state.candidate_commit,
        accepted_commit=payload.candidate_commit or state.candidate_commit,
        accepted_content_id=payload.accepted_content_id or payload.candidate_id,
    )


def _apply_terminal(
    payload: CandidateRejected | EvolutionStopped | ExternalOperationBlocked,
    state: _EvolutionState,
) -> _EvolutionState:
    if isinstance(payload, CandidateRejected):
        return replace(
            state,
            phase=EvolutionPhase.AWAITING_HYPOTHESIS,
            hypothesis_id=None,
            candidate_workspace_id=None,
            candidate_id=None,
            candidate_commit=None,
            run_id=None,
            candidate_receipt_id=None,
            decision_id=None,
            gate_status=None,
        )
    if isinstance(payload, EvolutionStopped):
        return replace(
            state,
            phase=EvolutionPhase.STOPPED,
            stop_reason=payload.reason,
        )
    return replace(
        state,
        phase=EvolutionPhase.BLOCKED,
        stop_reason=EvolutionStopReason.BLOCKED,
    )


def _request_event(
    events: tuple[EvolutionEvent, ...], request_id: str
) -> EvolutionEvent | None:
    matches = tuple(event for event in events if event.causation_id == request_id)
    return matches[-1] if matches else None


def _find_intent(
    events: tuple[EvolutionEvent, ...],
    key: str,
    operation: ExternalOperation,
) -> ExternalOperationIntent | None:
    for event in events:
        if _matches_intent(event, key, operation):
            payload = event.payload
            if isinstance(payload, ExternalOperationIntent):
                return payload
    return None


def _matches_intent(
    event: EvolutionEvent, key: str, operation: ExternalOperation
) -> bool:
    if event.event_type is not EvolutionEventType.EXTERNAL_OPERATION_INTENT:
        return False
    if not isinstance(event.payload, ExternalOperationIntent):
        return False
    return event.payload.idempotency_key == key and event.payload.operation is operation


def _observation(
    experiment_id: str,
    state: _EvolutionState,
    sequence: int,
) -> EvolutionObservation:
    next_action = {
        EvolutionPhase.AWAITING_HYPOTHESIS: "link_hypothesis",
        EvolutionPhase.AWAITING_CANDIDATE: "prepare_or_submit_candidate",
        EvolutionPhase.CANDIDATE_RUNNING: "complete_candidate_run",
        EvolutionPhase.GATE_READY: "record_promotion_decision",
        EvolutionPhase.AWAITING_PUBLICATION: "publish_accepted_candidate",
        EvolutionPhase.BLOCKED: "retry_after_external_state_change",
        EvolutionPhase.STOPPED: "stop",
    }[state.phase]
    status = (
        EvolutionStatus.ERROR
        if state.phase is EvolutionPhase.STOPPED
        else EvolutionStatus.SUCCESS
    )
    return EvolutionObservation(
        experiment_id=experiment_id,
        status=status,
        phase=state.phase,
        summary=f"Evolution is {state.phase.value}.",
        next_actions=(next_action,),
        sequence=sequence,
        iteration=state.iteration,
        hypothesis_id=state.hypothesis_id,
        candidate_workspace_id=state.candidate_workspace_id,
        candidate_id=state.candidate_id,
        run_id=state.run_id,
        decision_id=state.decision_id,
        accepted_release_id=state.accepted_release_id,
        accepted_commit=state.accepted_commit,
        accepted_content_id=state.accepted_content_id,
        stop_reason=state.stop_reason,
    )


def _operation_key(experiment_id: str, operation: str, iteration: int) -> str:
    value = f"{experiment_id}\0{operation}\0{iteration}"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_identity(commit: str) -> str:
    return "sha256:" + hashlib.sha256(f"git-commit\0{commit}".encode()).hexdigest()


def _tree_content_identity(tree: str) -> str:
    return "sha256:" + hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest()


def _has_publication(events: tuple[EvolutionEvent, ...]) -> bool:
    return any(
        event.event_type
        in (EvolutionEventType.RELEASE_PUBLISHED, EvolutionEventType.RELEASE_ROLLED_BACK)
        for event in events
    )


def _accepted_identity(
    state: _EvolutionState, policy: ExperimentPolicySnapshot
) -> _EvolutionState:
    if state.accepted_commit is not None and state.accepted_content_id is not None:
        return state
    commit = state.accepted_commit or policy.initialization_commit
    return replace(
        state,
        accepted_commit=commit,
        accepted_content_id=state.accepted_content_id or _content_identity(commit),
    )
