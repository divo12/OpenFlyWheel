from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ofw.evolution.gate import PromotionDecision, PromotionReason, PromotionStatus
from ofw.evolution.ledger import (
    EvolutionEvent,
    EvolutionEventDraft,
    EvolutionEventType,
    EvolutionLedgerErrorCode,
    EvolutionLedgerFailure,
    EvolutionStarted,
    FileEvolutionLedger,
    ReleasePublished,
)
from ofw.evolution.publication import (
    AcceptedCasToken,
    AcceptedPublication,
    GitPublicationGateway,
    PublicationErrorCode,
    PublicationFailure,
    PublicationService,
    PublishedPublication,
    RollbackRequest,
)

_POLICY = "sha256:" + "a" * 64
_EXPERIMENT = "experiment-one"
_WHEN = datetime(2026, 9, 3, tzinfo=UTC)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "prompt.md").write_text("initial\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "prompt.md")
    _git(root, "commit", "-qm", "initial")
    initial = _git(root, "rev-parse", "HEAD")
    _git(root, "branch", "-m", "ofw/experiment-one")
    ledger = FileEvolutionLedger()
    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            experiment_id=_EXPERIMENT,
            payload=EvolutionStarted(
                policy_digest=_POLICY,
                accepted_commit=initial,
                accepted_release_id="initial",
            ),
            occurred_at=_WHEN,
            causation_id="start",
            correlation_id="start",
        ),
    )
    _git(root, "switch", "-c", "candidate")
    (root / "prompt.md").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", "prompt.md")
    _git(root, "commit", "-qm", "candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    _git(root, "switch", "ofw/experiment-one")
    return root, initial, candidate, tree


def _decision() -> PromotionDecision:
    canonical = "{}"
    return PromotionDecision(
        decision_id="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        policy_digest=_POLICY,
        accepted_run_id="accepted",
        candidate_run_id="candidate",
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
        canonical_json=canonical,
    )


def _promote(
    service: PublicationService,
    root: Path,
    expected: AcceptedPublication,
    candidate: str,
    tree: str,
    operation_id: str,
    publication_id: str,
) -> PublishedPublication:
    return service.promote(
        root=root,
        experiment_id=_EXPERIMENT,
        policy_digest=_POLICY,
        operation_id=operation_id,
        publication_id=publication_id,
        expected=expected.cas_token,
        candidate_commit=candidate,
        candidate_tree=tree,
        gate=_decision(),
    )


def test_current_lookup_and_linear_promotion_are_typed_and_forward_only(
    tmp_path: Path,
) -> None:
    root, initial, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    assert isinstance(current, AcceptedPublication)
    assert (current.content_commit, current.content_tree) == (
        initial,
        _git(root, "rev-parse", f"{initial}^{{tree}}"),
    )

    published = service.promote(
        root=root,
        experiment_id=_EXPERIMENT,
        policy_digest=_POLICY,
        operation_id="sha256:" + "b" * 64,
        publication_id="publication-1",
        expected=current.cas_token,
        candidate_commit=candidate,
        candidate_tree=tree,
        gate=_decision(),
    )

    assert published.publication_id == "publication-1"
    assert published.content_commit == candidate
    assert _git(root, "rev-parse", "refs/heads/ofw/experiment-one") == candidate
    assert _git(root, "rev-parse", f"{candidate}^{{tree}}") == tree


def test_rollback_creates_a_child_with_historical_tree_and_new_identity(
    tmp_path: Path,
) -> None:
    root, initial, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    first = service.current_accepted(root, _EXPERIMENT, _POLICY)
    service.promote(
        root=root,
        experiment_id=_EXPERIMENT,
        policy_digest=_POLICY,
        operation_id="sha256:" + "b" * 64,
        publication_id="publication-1",
        expected=first.cas_token,
        candidate_commit=candidate,
        candidate_tree=tree,
        gate=_decision(),
    )
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    rolled = service.rollback(
        RollbackRequest(
            root=root,
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="sha256:" + "c" * 64,
            publication_id="publication-2",
            expected=current.cas_token,
            target_publication_id=first.publication_id,
        )
    )

    assert rolled.publication_id == "publication-2"
    assert rolled.content_tree == first.content_tree
    assert rolled.content_commit != initial
    assert _git(root, "rev-parse", f"{rolled.content_commit}^") == current.content_commit
    assert _git(root, "rev-parse", f"{rolled.content_commit}^{{tree}}") == first.content_tree


def test_stale_and_current_rollback_targets_are_rejected(tmp_path: Path) -> None:
    root, _, _, _ = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    with pytest.raises(PublicationFailure) as raised:
        service.rollback(
            RollbackRequest(
                root=root,
                experiment_id=_EXPERIMENT,
                policy_digest=_POLICY,
                operation_id="sha256:" + "c" * 64,
                publication_id="publication-2",
                expected=current.cas_token,
                target_publication_id=current.publication_id,
            )
        )
    assert raised.value.code is PublicationErrorCode.CURRENT_TARGET


def test_promotion_retry_is_idempotent_and_conflicting_reuse_fails(tmp_path: Path) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)
    first = _promote(service, root, current, candidate, tree, "sha256:" + "b" * 64, "publication-1")
    events = FileEvolutionLedger().events(root, _EXPERIMENT)
    assert (
        _promote(service, root, current, candidate, tree, "sha256:" + "b" * 64, "publication-1")
        == first
    )
    assert FileEvolutionLedger().events(root, _EXPERIMENT) == events

    with pytest.raises(PublicationFailure) as raised:
        _promote(
            service,
            root,
            current,
            current.content_commit,
            tree,
            "sha256:" + "b" * 64,
            "publication-1",
        )
    assert raised.value.code is PublicationErrorCode.OPERATION_CONFLICT


def test_stale_cas_is_rejected_after_another_publication(tmp_path: Path) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)
    _promote(service, root, current, candidate, tree, "sha256:" + "b" * 64, "publication-1")

    with pytest.raises(PublicationFailure) as raised:
        service.promote(
            root=root,
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="sha256:" + "c" * 64,
            publication_id="publication-2",
            expected=current.cas_token,
            candidate_commit=candidate,
            candidate_tree=tree,
            gate=_decision(),
        )
    assert raised.value.code is PublicationErrorCode.STALE_CAS


class _CrashLedger:
    def __init__(self, inner: FileEvolutionLedger, *, fail_type: EvolutionEventType) -> None:
        self.inner = inner
        self.fail_type = fail_type
        self.failed = False

    def events(self, root: Path, experiment_id: str) -> tuple[EvolutionEvent, ...]:
        return self.inner.events(root, experiment_id)

    def append(self, root: Path, draft: EvolutionEventDraft) -> EvolutionEvent:
        if draft.event_type is self.fail_type and not self.failed:
            self.failed = True
            raise EvolutionLedgerFailure(EvolutionLedgerErrorCode.WRITE_FAILED, "simulated")
        return self.inner.append(root, draft)


class _CrashGit(GitPublicationGateway):
    def __init__(self) -> None:
        self.fail_once = True

    def cas(self, root: Path, experiment_id: str, expected: str, replacement: str) -> None:
        if self.fail_once:
            self.fail_once = False
            raise PublicationFailure(PublicationErrorCode.GIT_FAILED, "simulated")
        super().cas(root, experiment_id, expected, replacement)


def test_recovery_never_updates_a_ref_left_at_the_expected_head(tmp_path: Path) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    git = _CrashGit()
    service = PublicationService(FileEvolutionLedger(), git)
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)
    operation_id = "sha256:" + "b" * 64

    with pytest.raises(PublicationFailure) as raised:
        _promote(service, root, current, candidate, tree, operation_id, "publication-1")
    assert raised.value.code is PublicationErrorCode.GIT_FAILED
    with pytest.raises(PublicationFailure) as raised:
        service.reconcile(root, _EXPERIMENT, operation_id, _POLICY)
    assert raised.value.code is PublicationErrorCode.RECOVERY_REQUIRED
    assert _git(root, "rev-parse", "refs/heads/ofw/experiment-one") == current.content_commit

    assert (
        _promote(
            service, root, current, candidate, tree, operation_id, "publication-1"
        ).content_commit
        == candidate
    )


def test_missing_and_unrelated_targets_are_rejected(tmp_path: Path) -> None:
    root, _, _, _ = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    with pytest.raises(PublicationFailure) as raised:
        service.rollback(
            RollbackRequest(
                root=root,
                experiment_id=_EXPERIMENT,
                policy_digest=_POLICY,
                operation_id="sha256:" + "c" * 64,
                publication_id="publication-2",
                expected=current.cas_token,
                target_publication_id="missing",
            )
        )
    assert raised.value.code is PublicationErrorCode.MISSING_TARGET
    _git(root, "switch", "-c", "unrelated")
    with pytest.raises(PublicationFailure) as raised:
        service.current_accepted(root, _EXPERIMENT, _POLICY)
    assert raised.value.code is PublicationErrorCode.UNRELATED_WORKTREE


def test_publication_rejects_invalid_gate_tree_and_ancestry(tmp_path: Path) -> None:
    root, initial, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    with pytest.raises(PublicationFailure) as raised:
        service.promote(
            root=root,
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="sha256:" + "b" * 64,
            publication_id="publication-1",
            expected=current.cas_token,
            candidate_commit=candidate,
            candidate_tree=tree,
            gate=replace(_decision(), status=PromotionStatus.REJECT),
        )
    assert raised.value.code is PublicationErrorCode.INVALID_GATE

    with pytest.raises(PublicationFailure) as raised:
        service.promote(
            root=root,
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="sha256:" + "c" * 64,
            publication_id="publication-2",
            expected=current.cas_token,
            candidate_commit=candidate,
            candidate_tree="0" * 40,
            gate=_decision(),
        )
    assert raised.value.code is PublicationErrorCode.INVALID_COMMIT

    gateway = GitPublicationGateway()
    with pytest.raises(PublicationFailure) as raised:
        gateway.validate_candidate(
            root, initial, initial, _git(root, "rev-parse", f"{initial}^{{tree}}")
        )
    assert raised.value.code is PublicationErrorCode.INVALID_COMMIT
    with pytest.raises(PublicationFailure) as raised:
        gateway.validate_historical(root, candidate, initial)
    assert raised.value.code is PublicationErrorCode.NOT_FORWARD


def test_git_boundary_rejects_missing_ref_objects_and_stale_cas(tmp_path: Path) -> None:
    root, initial, candidate, tree = _repo(tmp_path)
    gateway = GitPublicationGateway()

    _git(root, "update-ref", "-d", "refs/heads/ofw/experiment-one")
    with pytest.raises(PublicationFailure) as raised:
        gateway.current(root, _EXPERIMENT)
    assert raised.value.code is PublicationErrorCode.MISSING_CURRENT

    with pytest.raises(PublicationFailure) as raised:
        gateway.tree(root, "f" * 40)
    assert raised.value.code is PublicationErrorCode.INVALID_COMMIT
    with pytest.raises(PublicationFailure) as raised:
        gateway.rollback_commit(root, initial, tree, "not-an-operation")
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST

    _git(root, "update-ref", "refs/heads/ofw/experiment-one", initial)
    with pytest.raises(PublicationFailure) as raised:
        gateway.cas(root, _EXPERIMENT, candidate, candidate)
    assert raised.value.code is PublicationErrorCode.STALE_CAS


def test_current_lookup_rejects_missing_start_and_policy_mismatch(tmp_path: Path) -> None:
    root, _, _, _ = _repo(tmp_path)
    ledger_path = root / ".git/ofw/preparations/experiment-one/evolution.jsonl"
    ledger_path.unlink()
    with pytest.raises(PublicationFailure) as raised:
        PublicationService(FileEvolutionLedger()).current_accepted(root, _EXPERIMENT, _POLICY)
    assert raised.value.code is PublicationErrorCode.MISSING_CURRENT

    policy_root = tmp_path / "policy"
    policy_root.mkdir()
    root, _, _, _ = _repo(policy_root)
    with pytest.raises(PublicationFailure) as raised:
        PublicationService(FileEvolutionLedger()).current_accepted(
            root, _EXPERIMENT, "sha256:" + "d" * 64
        )
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST


def test_publication_rejects_colliding_and_invalid_requests(tmp_path: Path) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)
    _promote(service, root, current, candidate, tree, "sha256:" + "b" * 64, "publication-1")
    latest = service.current_accepted(root, _EXPERIMENT, _POLICY)

    with pytest.raises(PublicationFailure) as raised:
        _promote(
            service,
            root,
            latest,
            candidate,
            tree,
            "sha256:" + "c" * 64,
            "publication-1",
        )
    assert raised.value.code is PublicationErrorCode.OPERATION_CONFLICT

    with pytest.raises(PublicationFailure) as raised:
        service.current_accepted(root, "bad/ref", _POLICY)
    assert raised.value.code is PublicationErrorCode.UNSAFE_REF
    with pytest.raises(PublicationFailure) as raised:
        service.current_accepted(root, _EXPERIMENT, "not-a-digest")
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST


def test_reconcile_handles_unknown_and_conflicting_ref_state(tmp_path: Path) -> None:
    root, initial, candidate, tree = _repo(tmp_path)
    service = PublicationService(FileEvolutionLedger())
    assert service.reconcile(root, _EXPERIMENT, "sha256:" + "f" * 64, _POLICY) is None
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)
    git = _CrashGit()
    crashing = PublicationService(FileEvolutionLedger(), git)
    operation_id = "sha256:" + "b" * 64
    with pytest.raises(PublicationFailure):
        _promote(crashing, root, current, candidate, tree, operation_id, "publication-1")
    other = _git(root, "commit-tree", tree, "-p", initial, "-m", "other")
    _git(root, "update-ref", "refs/heads/ofw/experiment-one", other, initial)
    with pytest.raises(PublicationFailure) as raised:
        crashing.reconcile(root, _EXPERIMENT, operation_id, _POLICY)
    assert raised.value.code is PublicationErrorCode.REF_CONFLICT


def test_rollback_retry_reuses_intent_and_rejects_conflicts(tmp_path: Path) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    inner = FileEvolutionLedger()
    normal = PublicationService(inner)
    first = normal.current_accepted(root, _EXPERIMENT, _POLICY)
    _promote(normal, root, first, candidate, tree, "sha256:" + "b" * 64, "publication-1")
    crashing = PublicationService(inner, _CrashGit())
    current = crashing.current_accepted(root, _EXPERIMENT, _POLICY)
    request = RollbackRequest(
        root=root,
        experiment_id=_EXPERIMENT,
        policy_digest=_POLICY,
        operation_id="sha256:" + "c" * 64,
        publication_id="publication-2",
        expected=current.cas_token,
        target_publication_id=first.publication_id,
    )
    with pytest.raises(PublicationFailure):
        crashing.rollback(request)
    retried = crashing.rollback(request)
    assert retried.content_tree == first.content_tree

    with pytest.raises(PublicationFailure) as raised:
        crashing.rollback(
            RollbackRequest(
                root=root,
                experiment_id=_EXPERIMENT,
                policy_digest=_POLICY,
                operation_id=request.operation_id,
                publication_id=request.publication_id,
                expected=request.expected,
                target_publication_id="publication-1",
            )
        )
    assert raised.value.code is PublicationErrorCode.OPERATION_CONFLICT


def test_release_history_requires_durable_commit_and_tree(tmp_path: Path) -> None:
    root, initial, _, _ = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.RELEASE_PUBLISHED,
            experiment_id=_EXPERIMENT,
            payload=ReleasePublished(
                release_id="legacy",
                content_commit=initial,
                content_tree="0" * 40,
            ),
            occurred_at=_WHEN,
            causation_id="legacy",
            correlation_id="legacy",
        ),
    )
    with pytest.raises(PublicationFailure) as raised:
        PublicationService(ledger).current_accepted(root, _EXPERIMENT, _POLICY)
    assert raised.value.code is PublicationErrorCode.INVALID_COMMIT


def test_release_without_explicit_parent_links_to_initial_publication(tmp_path: Path) -> None:
    root, initial, _, _ = _repo(tmp_path)
    tree = _git(root, "rev-parse", f"{initial}^{{tree}}")
    ledger = FileEvolutionLedger()
    ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.RELEASE_PUBLISHED,
            experiment_id=_EXPERIMENT,
            payload=ReleasePublished(
                release_id="legacy",
                content_commit=initial,
                content_tree=tree,
            ),
            occurred_at=_WHEN,
            causation_id="legacy",
            correlation_id="legacy",
        ),
    )

    current = PublicationService(ledger).current_accepted(root, _EXPERIMENT, _POLICY)

    assert current.parent_publication_id == "initial"


def test_ledger_and_git_failures_are_sanitized(tmp_path: Path) -> None:
    root, _, _, _ = _repo(tmp_path)

    class FailingLedger:
        def events(self, workspace_root: Path, experiment_id: str) -> tuple[EvolutionEvent, ...]:
            del workspace_root, experiment_id
            raise EvolutionLedgerFailure(EvolutionLedgerErrorCode.WRITE_FAILED, "secret")

        def append(self, workspace_root: Path, draft: EvolutionEventDraft) -> EvolutionEvent:
            del workspace_root, draft
            raise AssertionError("append is not reached")

    with pytest.raises(PublicationFailure) as raised:
        PublicationService(FailingLedger()).current_accepted(root, _EXPERIMENT, _POLICY)
    assert raised.value.code is PublicationErrorCode.LEDGER_FAILED
    assert "secret" not in str(raised.value)

    with pytest.raises(PublicationFailure) as raised:
        PublicationService(FileEvolutionLedger()).commit_tree(root, "bad")
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST


def test_cas_token_identity_is_strict() -> None:
    with pytest.raises(PublicationFailure) as raised:
        AcceptedCasToken("not-a-digest", "a" * 40, "publication", _POLICY)
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST

    with pytest.raises(PublicationFailure) as raised:
        PublicationService(FileEvolutionLedger()).promote(
            root=Path("/tmp/repo"),
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="bad-operation",
            publication_id="publication",
            expected=AcceptedCasToken("sha256:" + "a" * 64, "a" * 40, "publication", _POLICY),
            candidate_commit="b" * 40,
            candidate_tree="c" * 40,
            gate=_decision(),
        )
    assert raised.value.code is PublicationErrorCode.INVALID_REQUEST


def test_recovery_completes_after_ref_cas_without_duplicate_publication(
    tmp_path: Path,
) -> None:
    root, _, candidate, tree = _repo(tmp_path)
    inner = FileEvolutionLedger()
    ledger = _CrashLedger(inner, fail_type=EvolutionEventType.RELEASE_PUBLISHED)
    service = PublicationService(ledger)
    current = service.current_accepted(root, _EXPERIMENT, _POLICY)

    with pytest.raises(PublicationFailure) as raised:
        service.promote(
            root=root,
            experiment_id=_EXPERIMENT,
            policy_digest=_POLICY,
            operation_id="sha256:" + "b" * 64,
            publication_id="publication-1",
            expected=current.cas_token,
            candidate_commit=candidate,
            candidate_tree=tree,
            gate=_decision(),
        )
    assert raised.value.code is PublicationErrorCode.LEDGER_FAILED
    assert _git(root, "rev-parse", "refs/heads/ofw/experiment-one") == candidate

    recovered = service.reconcile(root, _EXPERIMENT, "sha256:" + "b" * 64, _POLICY)
    assert recovered is not None
    assert recovered.content_commit == candidate
    assert service.reconcile(root, _EXPERIMENT, "sha256:" + "b" * 64, _POLICY) == recovered
    assert (
        sum(
            event.event_type is EvolutionEventType.RELEASE_PUBLISHED
            for event in inner.events(root, _EXPERIMENT)
        )
        == 1
    )
