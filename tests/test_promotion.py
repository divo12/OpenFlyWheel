"""Governed Git promotion, approval, rollback, and idempotency."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ofw import BenchmarkPolicy, FitCampaign, Harness
from ofw import ofw as ofw_namespace
from ofw.candidate import CandidateBuild
from ofw.promotion import (
    ApprovalDecision,
    ApprovalRecord,
    ApproverId,
    DeploymentAdapter,
    DeploymentReference,
    DeploymentRequest,
    GitRemote,
    PromotionError,
    PromotionErrorCode,
    PromotionMarker,
    PromotionMode,
    PromotionPolicy,
    PromotionRequest,
    PromotionService,
    PullRequestDraft,
    PullRequestId,
    PullRequestPublisher,
    PullRequestReference,
)
from tests.test_fit import _bundle, _candidate, _fit_policy, _harness

_NOW = datetime(2026, 8, 22, 18, tzinfo=UTC)


def _run_git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _winning_campaign(tmp_path: Path) -> tuple[Harness, FitCampaign, CandidateBuild]:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    candidate = _candidate(
        revision,
        "def run(value: str) -> str:\n"
        "    return value + (' FIXED' if 'regression' not in value else '')\n",
        "Fix frontier and holdouts without regressing the critical case.",
    )
    campaign = FitCampaign(
        harness,
        _bundle(revision),
        BenchmarkPolicy(1, 10, 0, 0.25),
        _fit_policy(),
        (candidate,),
    )
    return harness, campaign, candidate


@dataclass(slots=True)
class _PullRequests:
    opened: list[PullRequestReference] = field(default_factory=list)

    def find(self, marker: PromotionMarker) -> PullRequestReference | None:
        return next((reference for reference in self.opened if reference.marker == marker), None)

    def open(self, draft: PullRequestDraft) -> PullRequestReference:
        reference = PullRequestReference(
            PullRequestId(len(self.opened) + 1),
            f"https://example.test/pulls/{len(self.opened) + 1}",
            draft.marker,
            draft.head_branch,
        )
        self.opened.append(reference)
        return reference


@dataclass(slots=True)
class _Deployments:
    deployed: list[DeploymentReference] = field(default_factory=list)

    def find(self, marker: PromotionMarker) -> DeploymentReference | None:
        return next((reference for reference in self.deployed if reference.marker == marker), None)

    def deploy(self, request: DeploymentRequest) -> DeploymentReference:
        reference = DeploymentReference(
            f"deployment-{len(self.deployed) + 1}",
            request.marker,
            f"rollback {request.commit.value}",
        )
        self.deployed.append(reference)
        return reference


def test_pull_request_promotion_is_idempotent_and_has_reverse_artifact(
    tmp_path: Path,
) -> None:
    harness, campaign, candidate = _winning_campaign(tmp_path)
    fit_result = campaign.run()
    remote = tmp_path / "remote.git"
    subprocess.run(("git", "init", "--bare", "-q", str(remote)), check=True)
    _run_git(harness.root, "remote", "add", "review", str(remote))
    publisher = _PullRequests()
    publisher_contract: PullRequestPublisher = publisher
    request = PromotionRequest(
        campaign,
        fit_result,
        candidate,
        PromotionPolicy(
            PromotionMode.PULL_REQUEST,
            GitRemote("review", "main", "ofw"),
            require_approval=False,
        ),
        None,
    )
    interrupted_worktree = candidate.workspace.parent / f"promotion-{request.id[-16:]}"
    _run_git(
        harness.root,
        "worktree",
        "add",
        "-b",
        f"ofw/promotion-{request.id[-16:]}",
        str(interrupted_worktree),
        candidate.workspace.branch.value,
    )

    result = ofw_namespace.promote(
        request,
        now=_NOW,
        pull_requests=publisher_contract,
    )
    result.manifest_path.unlink()
    result.digest_path.unlink()
    recovered = PromotionService(publisher_contract, None).run(request, _NOW)
    restarted = PromotionService(publisher_contract, None).run(request, _NOW)

    assert restarted == result
    assert recovered == result
    assert result.pull_request == publisher.opened[0]
    assert not interrupted_worktree.exists()
    assert len(publisher.opened) == 1
    assert result.deployment is None
    assert result.commit is not None
    assert result.rollback.reverse_patch.read_bytes()
    assert result.report_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    remote_commit = _run_git(
        remote,
        "rev-parse",
        f"refs/heads/{result.pull_request.head_branch}",
    )
    assert remote_commit == result.commit.value
    subprocess.run(
        (
            "git",
            "-C",
            str(candidate.workspace.root),
            "apply",
            "--check",
            str(result.rollback.reverse_patch),
        ),
        check=True,
    )
    assert campaign.run() == fit_result
    result.rollback.reverse_patch.write_bytes(result.rollback.reverse_patch.read_bytes() + b"\n")
    with pytest.raises(PromotionError) as tampered:
        PromotionService(publisher_contract, None).run(request, _NOW)
    assert tampered.value.code is PromotionErrorCode.RESULT_INVALID
    candidate.workspace.close()


def test_direct_deploy_requires_matching_human_approval(tmp_path: Path) -> None:
    _harness_instance, campaign, candidate = _winning_campaign(tmp_path)
    fit_result = campaign.run()
    policy = PromotionPolicy(PromotionMode.DEPLOY, None, require_approval=True)
    request = PromotionRequest(campaign, fit_result, candidate, policy, None)
    deployments = _Deployments()
    deployment_contract: DeploymentAdapter = deployments
    service = PromotionService(None, deployment_contract)

    with pytest.raises(PromotionError) as missing:
        service.run(request, _NOW)
    assert missing.value.code is PromotionErrorCode.APPROVAL_REQUIRED

    rejected = ApprovalRecord(
        fit_result.id,
        candidate.candidate.id,
        policy.digest,
        ApproverId("reviewer"),
        ApprovalDecision.REJECTED,
        _NOW,
    )
    with pytest.raises(PromotionError) as denied:
        service.run(
            PromotionRequest(campaign, fit_result, candidate, policy, rejected),
            _NOW,
        )
    assert denied.value.code is PromotionErrorCode.APPROVAL_REJECTED

    approved = ApprovalRecord(
        fit_result.id,
        candidate.candidate.id,
        policy.digest,
        ApproverId("reviewer"),
        ApprovalDecision.APPROVED,
        _NOW,
    )
    result = service.run(
        PromotionRequest(campaign, fit_result, candidate, policy, approved),
        _NOW,
    )
    result.manifest_path.unlink()
    result.digest_path.unlink()
    recovered = service.run(
        PromotionRequest(campaign, fit_result, candidate, policy, approved),
        _NOW,
    )

    assert result.deployment == deployments.deployed[0]
    assert recovered == result
    assert len(deployments.deployed) == 1
    assert result.approval_digest == approved.digest
    candidate.workspace.close()


def test_durable_cancellation_prevents_git_side_effects(tmp_path: Path) -> None:
    harness, campaign, candidate = _winning_campaign(tmp_path)
    fit_result = campaign.run()
    policy = PromotionPolicy(PromotionMode.COMMIT, None, require_approval=False)
    request = PromotionRequest(campaign, fit_result, candidate, policy, None)
    service = PromotionService(None, None)
    before = _run_git(harness.root, "for-each-ref", "--format=%(refname)", "refs/heads")

    service.cancel(request, ApproverId("operator"), _NOW)
    with pytest.raises(PromotionError) as cancelled:
        service.run(request, _NOW)

    assert cancelled.value.code is PromotionErrorCode.CANCELLED
    after = _run_git(harness.root, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert after == before
    candidate.workspace.close()
