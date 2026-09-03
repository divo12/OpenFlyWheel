"""Forward-only accepted publication, recovery, and rollback boundaries."""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ofw.evolution.gate import PromotionDecision
from ofw.evolution.ledger import (
    EvolutionEvent,
    EvolutionEventDraft,
    EvolutionEventType,
    EvolutionLedgerFailure,
    EvolutionStarted,
    ExternalOperation,
    ExternalOperationIntent,
    ReleasePublished,
    ReleaseRolledBack,
)

_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_EXPERIMENT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class PublicationErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_WORKSPACE = "invalid_workspace"
    UNSAFE_REF = "unsafe_ref"
    UNRELATED_WORKTREE = "unrelated_worktree"
    MISSING_CURRENT = "missing_current"
    MISSING_TARGET = "missing_target"
    NON_DURABLE_TARGET = "non_durable_target"
    CURRENT_TARGET = "current_target"
    STALE_CAS = "stale_cas"
    INVALID_GATE = "invalid_gate"
    INVALID_COMMIT = "invalid_commit"
    NOT_FORWARD = "not_forward"
    OPERATION_CONFLICT = "operation_conflict"
    RECOVERY_REQUIRED = "recovery_required"
    REF_CONFLICT = "ref_conflict"
    LEDGER_FAILED = "ledger_failed"
    GIT_FAILED = "git_failed"


class PublicationFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: PublicationErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class AcceptedCasToken:
    value: str
    expected_commit: str
    publication_id: str
    policy_digest: str

    def __post_init__(self) -> None:
        _require_commit(self.expected_commit)
        _require_digest(self.policy_digest)
        _require_identifier(self.publication_id)
        if not _DIGEST.fullmatch(self.value):
            raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "cas_token")


@dataclass(frozen=True, slots=True)
class AcceptedPublication:
    publication_id: str
    event_id: str
    content_commit: str
    content_tree: str
    parent_publication_id: str | None
    experiment_id: str
    policy_digest: str
    cas_token: AcceptedCasToken


@dataclass(frozen=True, slots=True)
class PublishedPublication:
    publication_id: str
    event_id: str
    content_commit: str
    content_tree: str
    parent_publication_id: str | None


@dataclass(frozen=True, slots=True)
class RollbackRequest:
    root: Path
    experiment_id: str
    policy_digest: str
    operation_id: str
    publication_id: str
    expected: AcceptedCasToken
    target_publication_id: str


class PublicationLedger(Protocol):
    def events(self, workspace_root: Path, experiment_id: str) -> tuple[EvolutionEvent, ...]: ...

    def append(self, workspace_root: Path, draft: EvolutionEventDraft) -> EvolutionEvent: ...


class PublicationGitGateway(Protocol):
    def current(self, root: Path, experiment_id: str) -> str: ...

    def tree(self, root: Path, commit: str) -> str: ...

    def validate_candidate(
        self, root: Path, expected_commit: str, candidate_commit: str, candidate_tree: str
    ) -> None: ...

    def validate_historical(self, root: Path, target_commit: str, current_commit: str) -> None: ...

    def rollback_commit(self, root: Path, parent: str, tree: str, operation_id: str) -> str: ...

    def cas(self, root: Path, experiment_id: str, expected: str, replacement: str) -> None: ...


class GitPublicationGateway:
    """Use only exact derived refs and old-value guarded Git mutations."""

    def current(self, root: Path, experiment_id: str) -> str:
        ref = _ref(experiment_id)
        self._validate_root(root, experiment_id)
        code, output = _git(root, "show-ref", "--hash", "--verify", ref)
        if code != 0 or not _COMMIT.fullmatch(output):
            raise PublicationFailure(PublicationErrorCode.MISSING_CURRENT, experiment_id)
        return output

    def tree(self, root: Path, commit: str) -> str:
        _require_commit(commit)
        code, output = _git(root, "rev-parse", "--verify", f"{commit}^{{tree}}")
        if code != 0 or not _COMMIT.fullmatch(output):
            raise PublicationFailure(PublicationErrorCode.INVALID_COMMIT, commit)
        return output

    def validate_candidate(
        self, root: Path, expected_commit: str, candidate_commit: str, candidate_tree: str
    ) -> None:
        self._validate_commit_lineage(root, expected_commit, candidate_commit, candidate_tree)
        if candidate_commit == expected_commit:
            raise PublicationFailure(PublicationErrorCode.INVALID_COMMIT, candidate_commit)

    def validate_historical(self, root: Path, target_commit: str, current_commit: str) -> None:
        _require_commit(target_commit)
        _require_commit(current_commit)
        code, _ = _git(root, "merge-base", "--is-ancestor", target_commit, current_commit)
        if code != 0:
            raise PublicationFailure(PublicationErrorCode.NOT_FORWARD, target_commit)

    def rollback_commit(self, root: Path, parent: str, tree: str, operation_id: str) -> str:
        _require_commit(parent)
        _require_commit(tree)
        if not _DIGEST.fullmatch(operation_id):
            raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "operation_id")
        code, output = _git(
            root,
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            f"chore(ofw): rollback {operation_id}",
        )
        if code != 0 or not _COMMIT.fullmatch(output):
            raise PublicationFailure(PublicationErrorCode.GIT_FAILED, "commit-tree")
        self._validate_commit_lineage(root, parent, output, tree)
        return output

    def cas(self, root: Path, experiment_id: str, expected: str, replacement: str) -> None:
        _require_commit(expected)
        _require_commit(replacement)
        self._validate_root(root, experiment_id)
        code, _ = _git(
            root,
            "update-ref",
            "--no-deref",
            _ref(experiment_id),
            replacement,
            expected,
        )
        if code != 0:
            raise PublicationFailure(PublicationErrorCode.STALE_CAS, experiment_id)

    def _validate_root(self, root: Path, experiment_id: str) -> Path:
        if not root.is_absolute() or not root.is_dir():
            raise PublicationFailure(PublicationErrorCode.INVALID_WORKSPACE, experiment_id)
        _require_prepared_root(root, experiment_id)
        _require_accepted_branch(root, experiment_id)
        return root

    def _validate_commit_lineage(
        self, root: Path, expected_commit: str, candidate_commit: str, candidate_tree: str
    ) -> None:
        _require_commit(expected_commit)
        _require_commit(candidate_commit)
        _require_commit(candidate_tree)
        if self.tree(root, candidate_commit) != candidate_tree:
            raise PublicationFailure(PublicationErrorCode.INVALID_COMMIT, candidate_commit)
        code, _ = _git(root, "merge-base", "--is-ancestor", expected_commit, candidate_commit)
        if code != 0:
            raise PublicationFailure(PublicationErrorCode.NOT_FORWARD, candidate_commit)


class PublicationService:
    def __init__(
        self,
        ledger: PublicationLedger,
        git: PublicationGitGateway | None = None,
    ) -> None:
        self._ledger = ledger
        self._git = git or GitPublicationGateway()

    def commit_tree(self, root: Path, commit: str) -> str:
        return self._git.tree(root, commit)

    def current_accepted(
        self, root: Path, experiment_id: str, policy_digest: str
    ) -> AcceptedPublication:
        _validate_identity(experiment_id, policy_digest)
        events = self._events(root, experiment_id)
        current_commit = self._git.current(root, experiment_id)
        release = _last_release(events)
        if release is None:
            return self._initial_publication(
                root, experiment_id, policy_digest, current_commit, events
            )
        return self._release_publication(
            root, experiment_id, policy_digest, current_commit, release
        )

    def _initial_publication(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        current_commit: str,
        events: tuple[EvolutionEvent, ...],
    ) -> AcceptedPublication:
        started = _last_started(events)
        if started is None:
            raise PublicationFailure(PublicationErrorCode.MISSING_CURRENT, experiment_id)
        payload = _started_payload(started)
        if payload.accepted_commit != current_commit:
            raise PublicationFailure(PublicationErrorCode.MISSING_CURRENT, experiment_id)
        if payload.policy_digest != policy_digest:
            raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, experiment_id)
        return _publication(
            payload.accepted_release_id or started.event_id,
            started.event_id,
            current_commit,
            self._git.tree(root, current_commit),
            None,
            experiment_id,
            policy_digest,
        )

    def promote(
        self,
        *,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        operation_id: str,
        publication_id: str,
        expected: AcceptedCasToken,
        candidate_commit: str,
        candidate_tree: str,
        gate: PromotionDecision,
        target_reached: bool = False,
    ) -> PublishedPublication:
        _validate_operation(experiment_id, policy_digest, operation_id, publication_id)
        intent, completion = self._operation(root, experiment_id, operation_id)
        if completion is not None:
            return self._completed(completion, publication_id, candidate_commit, candidate_tree)
        if intent is not None:
            _check_intent(
                intent, expected.expected_commit, candidate_commit, candidate_tree, publication_id
            )
            return self._resume_promotion(root, experiment_id, policy_digest, operation_id, intent)
        _validate_gate(gate, policy_digest, operation_id)
        current = self.current_accepted(root, experiment_id, policy_digest)
        _check_token(current, expected)
        _check_publication_collision(self._events(root, experiment_id), publication_id)
        self._git.validate_candidate(root, current.content_commit, candidate_commit, candidate_tree)
        intent = self._append_intent(
            root,
            experiment_id,
            operation_id,
            publication_id,
            current,
            candidate_commit,
            candidate_tree,
            None,
            target_reached,
        )
        self._git.cas(root, experiment_id, current.content_commit, candidate_commit)
        return self._append_published(
            root,
            experiment_id,
            policy_digest,
            operation_id,
            intent,
            publication_id,
            candidate_commit,
            candidate_tree,
            current.publication_id,
            target_reached,
        )

    def rollback(self, request: RollbackRequest) -> PublishedPublication:
        _validate_operation(
            request.experiment_id,
            request.policy_digest,
            request.operation_id,
            request.publication_id,
        )
        intent, completion = self._operation(
            request.root, request.experiment_id, request.operation_id
        )
        if completion is not None:
            return self._existing_rollback_completion(request, completion)
        if intent is not None:
            return self._existing_rollback(request, intent)
        return self._new_rollback(request)

    def _existing_rollback_completion(
        self, request: RollbackRequest, completion: EvolutionEvent
    ) -> PublishedPublication:
        payload = _release_payload(completion)
        if (
            not isinstance(payload, ReleaseRolledBack)
            or payload.target_release_id != request.target_publication_id
        ):
            raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, request.operation_id)
        return self._completed(completion, request.publication_id, "", "")

    def _existing_rollback(
        self, request: RollbackRequest, intent: EvolutionEvent
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        if payload.target_release_id != request.target_publication_id:
            raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, request.operation_id)
        _check_intent(
            intent,
            request.expected.expected_commit,
            payload.candidate_commit or "",
            payload.content_tree or "",
            request.publication_id,
        )
        return self._resume_rollback(request, intent)

    def _new_rollback(self, request: RollbackRequest) -> PublishedPublication:
        current = self.current_accepted(request.root, request.experiment_id, request.policy_digest)
        _check_token(current, request.expected)
        _check_publication_collision(
            self._events(request.root, request.experiment_id), request.publication_id
        )
        target = self._historical_target(
            request.root,
            request.experiment_id,
            request.target_publication_id,
            current,
        )
        self._git.validate_historical(
            root=request.root,
            target_commit=target.content_commit,
            current_commit=current.content_commit,
        )
        rollback_commit = self._git.rollback_commit(
            request.root, current.content_commit, target.content_tree, request.operation_id
        )
        intent = self._append_intent(
            request.root,
            request.experiment_id,
            request.operation_id,
            request.publication_id,
            current,
            rollback_commit,
            target.content_tree,
            request.target_publication_id,
        )
        self._git.cas(request.root, request.experiment_id, current.content_commit, rollback_commit)
        return self._append_rolled_back(
            request.root,
            request.experiment_id,
            request.policy_digest,
            request.operation_id,
            intent,
            request.publication_id,
            rollback_commit,
            target.content_tree,
            current.publication_id,
            request.target_publication_id,
        )

    def reconcile(
        self, root: Path, experiment_id: str, operation_id: str, policy_digest: str
    ) -> PublishedPublication | None:
        return self._reconcile(root, experiment_id, operation_id, policy_digest)

    def _reconcile(
        self, root: Path, experiment_id: str, operation_id: str, policy_digest: str
    ) -> PublishedPublication | None:
        intent, completion = self._operation(root, experiment_id, operation_id)
        if completion is not None:
            return self._completed(completion, "", "", "")
        if intent is None:
            return None
        return self._reconcile_intent(root, experiment_id, operation_id, policy_digest, intent)

    def _reconcile_intent(
        self,
        root: Path,
        experiment_id: str,
        operation_id: str,
        policy_digest: str,
        intent: EvolutionEvent,
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        current = self._git.current(root, experiment_id)
        if current == payload.candidate_commit:
            return self._complete_intent(root, experiment_id, policy_digest, intent)
        if current == payload.expected_current_commit:
            raise PublicationFailure(PublicationErrorCode.RECOVERY_REQUIRED, operation_id)
        raise PublicationFailure(PublicationErrorCode.REF_CONFLICT, experiment_id)

    def _complete_intent(
        self, root: Path, experiment_id: str, policy_digest: str, intent: EvolutionEvent
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        if payload.target_release_id is None:
            return self._append_published_from_intent(root, experiment_id, policy_digest, intent)
        return self._append_rolled_back_from_intent(root, experiment_id, policy_digest, intent)

    def _events(self, root: Path, experiment_id: str) -> tuple[EvolutionEvent, ...]:
        try:
            return self._ledger.events(root, experiment_id)
        except EvolutionLedgerFailure:
            raise PublicationFailure(PublicationErrorCode.LEDGER_FAILED, experiment_id) from None

    def _operation(
        self, root: Path, experiment_id: str, operation_id: str
    ) -> tuple[EvolutionEvent | None, EvolutionEvent | None]:
        events = self._events(root, experiment_id)
        intent = next(
            (event for event in events if _is_operation_intent(event, operation_id)), None
        )
        completion = next(
            (event for event in reversed(events) if _is_operation_completion(event, operation_id)),
            None,
        )
        return intent, completion

    def _append_intent(
        self,
        root: Path,
        experiment_id: str,
        operation_id: str,
        publication_id: str,
        current: AcceptedPublication,
        candidate_commit: str,
        candidate_tree: str,
        target_release_id: str | None,
        target_reached: bool = False,
    ) -> EvolutionEvent:
        try:
            return self._ledger.append(
                root,
                EvolutionEventDraft(
                    event_type=EvolutionEventType.EXTERNAL_OPERATION_INTENT,
                    experiment_id=experiment_id,
                    payload=ExternalOperationIntent(
                        operation=ExternalOperation.PUBLICATION,
                        idempotency_key=operation_id,
                        target=publication_id,
                        expected_current_commit=current.content_commit,
                        candidate_commit=candidate_commit,
                        content_tree=candidate_tree,
                        policy_digest=current.policy_digest,
                        parent_release_id=current.publication_id,
                        target_release_id=target_release_id,
                        target_reached=target_reached,
                    ),
                    occurred_at=datetime.now(UTC),
                    causation_id=operation_id,
                    correlation_id=operation_id,
                    request_digest=operation_id,
                ),
            )
        except EvolutionLedgerFailure:
            raise PublicationFailure(PublicationErrorCode.LEDGER_FAILED, experiment_id) from None

    def _append_published(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        operation_id: str,
        intent: EvolutionEvent,
        publication_id: str,
        commit: str,
        tree: str,
        parent: str,
        target_reached: bool,
    ) -> PublishedPublication:
        payload = ReleasePublished(
            release_id=publication_id,
            content_commit=commit,
            content_id=_content_id(tree),
            target_reached=target_reached,
            content_tree=tree,
            parent_release_id=parent,
            expected_current_commit=_intent_payload(intent).expected_current_commit,
            policy_digest=policy_digest,
            operation_id=operation_id,
            intent_event_id=intent.event_id,
        )
        event = self._append_completion(root, experiment_id, operation_id, intent, payload)
        return PublishedPublication(publication_id, event.event_id, commit, tree, parent)

    def _append_rolled_back(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        operation_id: str,
        intent: EvolutionEvent,
        publication_id: str,
        commit: str,
        tree: str,
        parent: str,
        target: str,
    ) -> PublishedPublication:
        payload = ReleaseRolledBack(
            release_id=publication_id,
            target_release_id=target,
            content_commit=commit,
            content_id=_content_id(tree),
            content_tree=tree,
            parent_release_id=parent,
            expected_current_commit=_intent_payload(intent).expected_current_commit,
            policy_digest=policy_digest,
            operation_id=operation_id,
            intent_event_id=intent.event_id,
        )
        event = self._append_completion(root, experiment_id, operation_id, intent, payload)
        return PublishedPublication(publication_id, event.event_id, commit, tree, parent)

    def _append_completion(
        self,
        root: Path,
        experiment_id: str,
        operation_id: str,
        intent: EvolutionEvent,
        payload: ReleasePublished | ReleaseRolledBack,
    ) -> EvolutionEvent:
        try:
            return self._ledger.append(
                root,
                EvolutionEventDraft(
                    event_type=(
                        EvolutionEventType.RELEASE_PUBLISHED
                        if isinstance(payload, ReleasePublished)
                        else EvolutionEventType.RELEASE_ROLLED_BACK
                    ),
                    experiment_id=experiment_id,
                    payload=payload,
                    occurred_at=intent.occurred_at,
                    causation_id=operation_id,
                    correlation_id=operation_id,
                    request_digest=operation_id,
                ),
            )
        except EvolutionLedgerFailure:
            raise PublicationFailure(PublicationErrorCode.LEDGER_FAILED, experiment_id) from None

    def _resume_promotion(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        operation_id: str,
        intent: EvolutionEvent,
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        current = self._git.current(root, experiment_id)
        if current == payload.candidate_commit:
            return self._append_published_from_intent(root, experiment_id, policy_digest, intent)
        if current != payload.expected_current_commit:
            raise PublicationFailure(PublicationErrorCode.STALE_CAS, experiment_id)
        self._git.cas(
            root,
            experiment_id,
            payload.expected_current_commit or "",
            payload.candidate_commit or "",
        )
        return self._append_published_from_intent(root, experiment_id, policy_digest, intent)

    def _resume_rollback(
        self, request: RollbackRequest, intent: EvolutionEvent
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        if payload.target_release_id is None:
            raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, request.operation_id)
        current = self._git.current(request.root, request.experiment_id)
        self._resume_rollback_head(request, payload, current)
        return self._append_rolled_back_from_intent(
            request.root, request.experiment_id, request.policy_digest, intent
        )

    def _resume_rollback_head(
        self,
        request: RollbackRequest,
        payload: ExternalOperationIntent,
        current: str,
    ) -> None:
        if current == payload.candidate_commit:
            return
        if current != payload.expected_current_commit:
            raise PublicationFailure(PublicationErrorCode.STALE_CAS, request.experiment_id)
        self._git.cas(
            request.root,
            request.experiment_id,
            payload.expected_current_commit or "",
            payload.candidate_commit or "",
        )

    def _append_published_from_intent(
        self, root: Path, experiment_id: str, policy_digest: str, intent: EvolutionEvent
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        parent = payload.parent_release_id or ""
        return self._append_published(
            root,
            experiment_id,
            policy_digest,
            payload.idempotency_key,
            intent,
            payload.target,
            payload.candidate_commit or "",
            payload.content_tree or "",
            parent,
            payload.target_reached,
        )

    def _append_rolled_back_from_intent(
        self, root: Path, experiment_id: str, policy_digest: str, intent: EvolutionEvent
    ) -> PublishedPublication:
        payload = _intent_payload(intent)
        return self._append_rolled_back(
            root,
            experiment_id,
            policy_digest,
            payload.idempotency_key,
            intent,
            payload.target,
            payload.candidate_commit or "",
            payload.content_tree or "",
            payload.parent_release_id or "",
            payload.target_release_id or "",
        )

    def _release_publication(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        current_commit: str,
        event: EvolutionEvent,
    ) -> AcceptedPublication:
        payload = _release_payload(event)
        if payload.policy_digest is not None and payload.policy_digest != policy_digest:
            raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, experiment_id)
        if payload.content_commit != current_commit:
            raise PublicationFailure(PublicationErrorCode.REF_CONFLICT, experiment_id)
        tree = self._release_tree(root, current_commit, payload.content_tree)
        parent = payload.parent_release_id
        if parent is None:
            parent = _previous_publication(self._events(root, experiment_id), event)
        return _publication(
            payload.release_id,
            event.event_id,
            current_commit,
            tree,
            parent,
            experiment_id,
            policy_digest,
        )

    def _release_tree(self, root: Path, commit: str, declared: str | None) -> str:
        tree = self._git.tree(root, commit)
        if declared is not None and declared != tree:
            raise PublicationFailure(PublicationErrorCode.INVALID_COMMIT, commit)
        return tree

    def _historical_target(
        self,
        root: Path,
        experiment_id: str,
        target_id: str,
        current: AcceptedPublication,
    ) -> AcceptedPublication:
        for event in reversed(self._events(root, experiment_id)):
            if not _matches_target(event, target_id):
                continue
            if (
                event.event_id == current.event_id
                or _publication_id(event) == current.publication_id
            ):
                raise PublicationFailure(PublicationErrorCode.CURRENT_TARGET, target_id)
            return self._historical_event(
                root, experiment_id, current.policy_digest, target_id, event
            )
        raise PublicationFailure(PublicationErrorCode.MISSING_TARGET, target_id)

    def _historical_event(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        target_id: str,
        event: EvolutionEvent,
    ) -> AcceptedPublication:
        payload = event.payload
        if isinstance(payload, (ReleasePublished, ReleaseRolledBack)):
            return self._historical_release(
                root, experiment_id, policy_digest, target_id, event, payload
            )
        return self._historical_started(root, experiment_id, policy_digest, target_id, event)

    def _historical_release(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        target_id: str,
        event: EvolutionEvent,
        payload: ReleasePublished | ReleaseRolledBack,
    ) -> AcceptedPublication:
        if payload.content_commit is None or payload.content_tree is None:
            raise PublicationFailure(PublicationErrorCode.NON_DURABLE_TARGET, target_id)
        if self._git.tree(root, payload.content_commit) != payload.content_tree:
            raise PublicationFailure(PublicationErrorCode.NON_DURABLE_TARGET, target_id)
        return _publication(
            payload.release_id,
            event.event_id,
            payload.content_commit,
            payload.content_tree,
            payload.parent_release_id,
            experiment_id,
            policy_digest,
        )

    def _historical_started(
        self,
        root: Path,
        experiment_id: str,
        policy_digest: str,
        target_id: str,
        event: EvolutionEvent,
    ) -> AcceptedPublication:
        started = _started_payload(event)
        if started.accepted_commit is None:
            raise PublicationFailure(PublicationErrorCode.NON_DURABLE_TARGET, target_id)
        return _publication(
            started.accepted_release_id or event.event_id,
            event.event_id,
            started.accepted_commit,
            self._git.tree(root, started.accepted_commit),
            None,
            experiment_id,
            policy_digest,
        )

    def _completed(
        self, event: EvolutionEvent, publication_id: str, commit: str, tree: str
    ) -> PublishedPublication:
        payload = _release_payload(event)
        _validate_completed(payload, publication_id, commit, tree)
        return PublishedPublication(
            payload.release_id,
            event.event_id,
            payload.content_commit or "",
            payload.content_tree or "",
            payload.parent_release_id,
        )


def _publication(
    publication_id: str,
    event_id: str,
    commit: str,
    tree: str,
    parent: str | None,
    experiment_id: str,
    policy_digest: str,
) -> AcceptedPublication:
    token_value = (
        "sha256:"
        + hashlib.sha256(
            "\0".join((experiment_id, publication_id, commit, tree, policy_digest)).encode()
        ).hexdigest()
    )
    token = AcceptedCasToken(token_value, commit, publication_id, policy_digest)
    return AcceptedPublication(
        publication_id,
        event_id,
        commit,
        tree,
        parent,
        experiment_id,
        policy_digest,
        token,
    )


def _validate_identity(experiment_id: str, policy_digest: str) -> None:
    _require_experiment(experiment_id)
    _require_digest(policy_digest)


def _validate_operation(
    experiment_id: str, policy_digest: str, operation_id: str, publication_id: str
) -> None:
    _validate_identity(experiment_id, policy_digest)
    if not _DIGEST.fullmatch(operation_id):
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "operation_id")
    _require_identifier(publication_id)


def _check_token(current: AcceptedPublication, expected: AcceptedCasToken) -> None:
    if expected != current.cas_token:
        raise PublicationFailure(PublicationErrorCode.STALE_CAS, current.experiment_id)


def _check_publication_collision(events: tuple[EvolutionEvent, ...], publication_id: str) -> None:
    if any(_publication_id(event) == publication_id for event in events):
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, publication_id)


def _validate_completed(
    payload: ReleasePublished | ReleaseRolledBack,
    publication_id: str,
    commit: str,
    tree: str,
) -> None:
    _validate_publication_id(payload.release_id, publication_id)
    _validate_commit(payload.content_commit, commit)
    _validate_tree(payload.content_tree, tree)


def _validate_publication_id(actual: str, expected: str) -> None:
    if expected and actual != expected:
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, expected)


def _validate_commit(actual: str | None, expected: str) -> None:
    if expected and actual != expected:
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, expected)


def _validate_tree(actual: str | None, expected: str) -> None:
    if expected and actual != expected:
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, expected)


def _validate_gate(gate: PromotionDecision, policy_digest: str, operation_id: str) -> None:
    if gate.policy_digest != policy_digest or gate.status.value != "accept":
        raise PublicationFailure(PublicationErrorCode.INVALID_GATE, operation_id)
    if gate.decision_id != gate.recomputed_id():
        raise PublicationFailure(PublicationErrorCode.INVALID_GATE, operation_id)


def _check_intent(
    event: EvolutionEvent,
    expected: str,
    commit: str,
    tree: str,
    publication_id: str,
) -> None:
    payload = _intent_payload(event)
    if (
        payload.target != publication_id
        or payload.expected_current_commit != expected
        or payload.candidate_commit != commit
        or payload.content_tree != tree
    ):
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, payload.idempotency_key)


def _matches_target(event: EvolutionEvent, target_id: str) -> bool:
    return event.event_id == target_id or _publication_id(event) == target_id


def _last_release(events: tuple[EvolutionEvent, ...]) -> EvolutionEvent | None:
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event.payload, (ReleasePublished, ReleaseRolledBack))
        ),
        None,
    )


def _last_started(events: tuple[EvolutionEvent, ...]) -> EvolutionEvent | None:
    return next(
        (event for event in reversed(events) if isinstance(event.payload, EvolutionStarted)),
        None,
    )


def _previous_publication(
    events: tuple[EvolutionEvent, ...], current: EvolutionEvent
) -> str | None:
    releases = _release_events(events)
    index = _release_position(releases, current)
    if index is not None and index:
        return _release_payload(releases[index - 1]).release_id
    started = _last_started(events)
    if started is None:
        return None
    return _started_payload(started).accepted_release_id or started.event_id


def _release_events(events: tuple[EvolutionEvent, ...]) -> tuple[EvolutionEvent, ...]:
    return tuple(
        event
        for event in events
        if isinstance(event.payload, (ReleasePublished, ReleaseRolledBack))
    )


def _release_position(
    releases: tuple[EvolutionEvent, ...], current: EvolutionEvent
) -> int | None:
    try:
        return releases.index(current)
    except ValueError:
        return None


def _is_operation_intent(event: EvolutionEvent, operation_id: str) -> bool:
    payload = event.payload
    return (
        isinstance(payload, ExternalOperationIntent)
        and payload.operation is ExternalOperation.PUBLICATION
        and payload.idempotency_key == operation_id
    )


def _is_operation_completion(event: EvolutionEvent, operation_id: str) -> bool:
    payload = event.payload
    return (
        isinstance(payload, (ReleasePublished, ReleaseRolledBack))
        and payload.operation_id == operation_id
    )


def _release_id(event: EvolutionEvent) -> str | None:
    payload = event.payload
    if isinstance(payload, (ReleasePublished, ReleaseRolledBack)):
        return payload.release_id
    return None


def _release_payload(event: EvolutionEvent) -> ReleasePublished | ReleaseRolledBack:
    payload = event.payload
    if not isinstance(payload, (ReleasePublished, ReleaseRolledBack)):
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, event.event_id)
    return payload


def _started_payload(event: EvolutionEvent) -> EvolutionStarted:
    payload = event.payload
    if not isinstance(payload, EvolutionStarted):
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, event.event_id)
    return payload


def _intent_payload(event: EvolutionEvent) -> ExternalOperationIntent:
    payload = event.payload
    if not isinstance(payload, ExternalOperationIntent):
        raise PublicationFailure(PublicationErrorCode.OPERATION_CONFLICT, event.event_id)
    return payload


def _publication_id(event: EvolutionEvent) -> str | None:
    payload = event.payload
    if isinstance(payload, EvolutionStarted):
        return payload.accepted_release_id
    return _release_id(event)


def _content_id(tree: str) -> str:
    return "sha256:" + hashlib.sha256(f"git-tree\0{tree}".encode()).hexdigest()


def _ref(experiment_id: str) -> str:
    _require_experiment(experiment_id)
    return f"refs/heads/ofw/{experiment_id}"


def _require_prepared_root(root: Path, experiment_id: str) -> None:
    code, top = _git(root, "rev-parse", "--show-toplevel")
    if code != 0 or Path(top).resolve() != root.resolve():
        raise PublicationFailure(PublicationErrorCode.UNRELATED_WORKTREE, experiment_id)


def _require_accepted_branch(root: Path, experiment_id: str) -> None:
    code, branch = _git(root, "branch", "--show-current")
    if code != 0 or branch != f"ofw/{experiment_id}":
        raise PublicationFailure(PublicationErrorCode.UNRELATED_WORKTREE, experiment_id)


def _require_commit(value: str) -> None:
    if not _COMMIT.fullmatch(value):
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "commit")


def _require_digest(value: str) -> None:
    if not _DIGEST.fullmatch(value):
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "digest")


def _require_identifier(value: str) -> None:
    if len(value) > 256 or _IDENTIFIER.fullmatch(value) is None:
        raise PublicationFailure(PublicationErrorCode.INVALID_REQUEST, "identifier")


def _require_experiment(value: str) -> None:
    if _EXPERIMENT.fullmatch(value) is None:
        raise PublicationFailure(PublicationErrorCode.UNSAFE_REF, "experiment_id")


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return 1, ""
    return result.returncode, result.stdout.strip()
