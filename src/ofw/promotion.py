"""Reviewable Git promotion with approval, audit, and rollback artifacts."""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from ofw.candidate import (
    CandidateBuild,
    CandidateError,
    CandidateId,
    validate_candidate_artifacts,
    validate_candidate_revision,
)
from ofw.contracts import GitCommit, Sha256Digest
from ofw.fit import FitCampaign, FitResult
from ofw.mine import digest_bytes, write_artifact
from ofw.scheduler import (
    FailureDisposition,
    JobContext,
    JobExecution,
    JobExecutionError,
    JobKind,
    JobResult,
    JobSpec,
    Money,
    ResultId,
    SchedulerErrorCode,
)


class PromotionMode(StrEnum):
    NONE = "none"
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    DEPLOY = "deploy"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PromotionEventKind(StrEnum):
    REPORT_WRITTEN = "report_written"
    COMMIT_CREATED = "commit_created"
    BRANCH_PUSHED = "branch_pushed"
    PULL_REQUEST_OPENED = "pull_request_opened"
    APPROVAL_VERIFIED = "approval_verified"
    DEPLOYED = "deployed"
    COMPLETED = "completed"


class PromotionErrorCode(StrEnum):
    NO_WINNER = "no_winner"
    WINNER_MISMATCH = "winner_mismatch"
    FIT_RESULT_INVALID = "fit_result_invalid"
    CANDIDATE_DRIFT = "candidate_drift"
    POLICY_INVALID = "policy_invalid"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_INVALID = "approval_invalid"
    CANCELLED = "cancelled"
    ALREADY_COMPLETED = "already_completed"
    GIT_FAILED = "git_failed"
    COMMIT_INVALID = "commit_invalid"
    PUBLISHER_REQUIRED = "publisher_required"
    PUBLISH_FAILED = "publish_failed"
    DEPLOYMENT_REQUIRED = "deployment_required"
    DEPLOYMENT_FAILED = "deployment_failed"
    RESULT_INVALID = "result_invalid"


class PromotionError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: PromotionErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class ApproverId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("approver cannot be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PromotionMarker:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PromotionBranch:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class GitRemote:
    name: str
    base_branch: str
    branch_prefix: str

    def __post_init__(self) -> None:
        for value in (self.name, self.base_branch, self.branch_prefix):
            if not _valid_git_name(value):
                raise PromotionError(PromotionErrorCode.POLICY_INVALID, value)


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    mode: PromotionMode
    remote: GitRemote | None
    require_approval: bool

    def __post_init__(self) -> None:
        if (
            (self.mode is PromotionMode.PULL_REQUEST and self.remote is None)
            or (self.mode is not PromotionMode.PULL_REQUEST and self.remote is not None)
            or (self.mode is PromotionMode.DEPLOY and not self.require_approval)
        ):
            raise PromotionError(PromotionErrorCode.POLICY_INVALID, self.mode.value)

    @property
    def digest(self) -> Sha256Digest:
        remote = self.remote
        payload = "\0".join(
            (
                self.mode.value,
                str(self.require_approval),
                "" if remote is None else remote.name,
                "" if remote is None else remote.base_branch,
                "" if remote is None else remote.branch_prefix,
            )
        )
        return digest_bytes(payload.encode())


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    fit_result_id: str
    candidate_id: CandidateId
    policy_digest: Sha256Digest
    approver: ApproverId
    decision: ApprovalDecision
    decided_at: datetime

    def __post_init__(self) -> None:
        _aware(self.decided_at)

    @property
    def digest(self) -> Sha256Digest:
        return digest_bytes(_APPROVAL_ADAPTER.dump_json(self))


@dataclass(frozen=True, slots=True)
class PullRequestId:
    value: int


@dataclass(frozen=True, slots=True)
class PullRequestDraft:
    marker: PromotionMarker
    title: str
    body: str
    head_branch: PromotionBranch
    base_branch: str
    commit: GitCommit


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    id: PullRequestId
    url: str
    marker: PromotionMarker
    head_branch: PromotionBranch


class PullRequestPublisher(Protocol):
    def find(self, marker: PromotionMarker) -> PullRequestReference | None: ...

    def open(self, draft: PullRequestDraft) -> PullRequestReference: ...


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    marker: PromotionMarker
    fit_result_id: str
    candidate_id: CandidateId
    commit: GitCommit
    approval: ApprovalRecord


@dataclass(frozen=True, slots=True)
class DeploymentReference:
    id: str
    marker: PromotionMarker
    rollback_instruction: str


class DeploymentAdapter(Protocol):
    def find(self, marker: PromotionMarker) -> DeploymentReference | None: ...

    def deploy(self, request: DeploymentRequest) -> DeploymentReference: ...


class PromotionRequestResolver(Protocol):
    def resolve(self, fit_result_id: ResultId) -> PromotionRequest: ...


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    reverse_patch: Path
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromotionEvent:
    kind: PromotionEventKind
    occurred_at: datetime
    reference: str


@dataclass(frozen=True, slots=True)
class PromotionResult:
    id: str
    mode: PromotionMode
    fit_result_id: str
    candidate_id: CandidateId
    policy_digest: Sha256Digest
    approval_digest: Sha256Digest | None
    branch: PromotionBranch | None
    commit: GitCommit | None
    pull_request: PullRequestReference | None
    deployment: DeploymentReference | None
    rollback: RollbackPlan
    report_path: Path
    report_digest: Sha256Digest
    reverse_digest: Sha256Digest
    events: tuple[PromotionEvent, ...]
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "promotions" / self.id / "manifest.json"

    @property
    def digest_path(self) -> Path:
        return self.manifest_path.with_suffix(".sha256")

    def to_json(self) -> str:
        return _RESULT_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    campaign: FitCampaign
    fit_result: FitResult
    candidate: CandidateBuild
    policy: PromotionPolicy
    approval: ApprovalRecord | None

    @property
    def id(self) -> str:
        payload = "\0".join(
            (
                self.fit_result.id,
                str(self.fit_result.input_digest),
                self.candidate.candidate.id.value,
                str(self.policy.digest),
            )
        )
        return f"promotion_{hashlib.sha256(payload.encode()).hexdigest()}"

    @property
    def marker(self) -> PromotionMarker:
        return PromotionMarker(f"ofw-promotion:{self.id}")

    @property
    def root(self) -> Path:
        return self.fit_result.root / ".ofw" / "promotions" / self.id


@dataclass(frozen=True, slots=True)
class CancellationRecord:
    promotion_id: str
    actor: ApproverId
    cancelled_at: datetime

    def to_json(self) -> str:
        return _CANCELLATION_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class _GitHubPullRequest:
    number: int
    url: str
    headRefName: str
    body: str


_APPROVAL_ADAPTER: TypeAdapter[ApprovalRecord] = TypeAdapter(ApprovalRecord)
_RESULT_ADAPTER: TypeAdapter[PromotionResult] = TypeAdapter(PromotionResult)
_CANCELLATION_ADAPTER: TypeAdapter[CancellationRecord] = TypeAdapter(CancellationRecord)
_DIGEST_ADAPTER: TypeAdapter[Sha256Digest] = TypeAdapter(Sha256Digest)
_GITHUB_ADAPTER: TypeAdapter[tuple[_GitHubPullRequest, ...]] = TypeAdapter(
    tuple[_GitHubPullRequest, ...]
)


@dataclass(frozen=True, slots=True)
class GitHubCliPublisher:
    repository: Path

    def find(self, marker: PromotionMarker) -> PullRequestReference | None:
        result = _command(
            self.repository,
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--search",
            marker.value,
            "--json",
            "number,url,headRefName,body",
        )
        try:
            records = _GITHUB_ADAPTER.validate_json(result.stdout)
        except ValidationError as error:
            raise PromotionError(PromotionErrorCode.PUBLISH_FAILED, marker.value) from error
        record = next((item for item in records if marker.value in item.body), None)
        if record is None:
            return None
        return PullRequestReference(
            PullRequestId(record.number),
            record.url,
            marker,
            PromotionBranch(record.headRefName),
        )

    def open(self, draft: PullRequestDraft) -> PullRequestReference:
        _command(
            self.repository,
            "gh",
            "pr",
            "create",
            "--base",
            draft.base_branch,
            "--head",
            draft.head_branch.value,
            "--title",
            draft.title,
            "--body",
            draft.body,
        )
        reference = self.find(draft.marker)
        if reference is None:
            raise PromotionError(PromotionErrorCode.PUBLISH_FAILED, draft.marker.value)
        return reference


@dataclass(frozen=True, slots=True)
class PromotionService:
    pull_requests: PullRequestPublisher | None
    deployments: DeploymentAdapter | None

    def run(self, request: PromotionRequest, now: datetime) -> PromotionResult:
        now = _aware(now)
        existing = self._read_existing(request)
        if existing is not None:
            return existing
        self._check_cancelled(request)
        fit_result = request.campaign.run()
        if fit_result != request.fit_result:
            raise PromotionError(PromotionErrorCode.FIT_RESULT_INVALID, request.fit_result.id)
        if fit_result.winner_id is None:
            raise PromotionError(PromotionErrorCode.NO_WINNER, fit_result.id)
        if fit_result.winner_id != request.candidate.candidate.id:
            raise PromotionError(
                PromotionErrorCode.WINNER_MISMATCH,
                request.candidate.candidate.id.value,
            )
        self._validate_approval(request)
        report_path = request.root / "report.html"
        report_payload = _report(request).encode()
        write_artifact(report_path, report_payload)
        events: tuple[PromotionEvent, ...] = (
            PromotionEvent(PromotionEventKind.REPORT_WRITTEN, now, str(report_path)),
        )
        commit: GitCommit | None = None
        branch: PromotionBranch | None = None
        pull_request: PullRequestReference | None = None
        deployment: DeploymentReference | None = None
        reverse_path = request.root / "reverse.patch"
        reverse = b""
        if request.policy.mode is not PromotionMode.NONE:
            self._check_cancelled(request)
            commit, branch = _ensure_commit(request)
            reverse = _git_bytes(
                request.candidate.workspace.source_root,
                "show",
                "-R",
                "--binary",
                "--format=",
                commit.value,
            )
            events = (*events, PromotionEvent(PromotionEventKind.COMMIT_CREATED, now, commit.value))
        write_artifact(reverse_path, reverse)
        if request.policy.mode is PromotionMode.PULL_REQUEST:
            self._check_cancelled(request)
            if self.pull_requests is None or request.policy.remote is None:
                raise PromotionError(PromotionErrorCode.PUBLISHER_REQUIRED, request.id)
            if commit is None or branch is None:
                raise PromotionError(PromotionErrorCode.COMMIT_INVALID, request.id)
            _push(request.candidate.workspace.source_root, request.policy.remote, branch)
            events = (*events, PromotionEvent(PromotionEventKind.BRANCH_PUSHED, now, branch.value))
            pull_request = self.pull_requests.find(request.marker)
            if pull_request is None:
                pull_request = self.pull_requests.open(
                    PullRequestDraft(
                        request.marker,
                        f"[ofw] promote {request.candidate.candidate.id.value[-12:]}",
                        _pull_request_body(request),
                        branch,
                        request.policy.remote.base_branch,
                        commit,
                    )
                )
            events = (
                *events,
                PromotionEvent(
                    PromotionEventKind.PULL_REQUEST_OPENED,
                    now,
                    pull_request.url,
                ),
            )
        if request.policy.mode is PromotionMode.DEPLOY:
            self._check_cancelled(request)
            approval = request.approval
            if approval is None:
                raise PromotionError(PromotionErrorCode.APPROVAL_REQUIRED, request.id)
            if self.deployments is None or commit is None:
                raise PromotionError(PromotionErrorCode.DEPLOYMENT_REQUIRED, request.id)
            events = (
                *events,
                PromotionEvent(
                    PromotionEventKind.APPROVAL_VERIFIED,
                    now,
                    str(approval.digest),
                ),
            )
            deployment = self.deployments.find(request.marker)
            if deployment is None:
                deployment = self.deployments.deploy(
                    DeploymentRequest(
                        request.marker,
                        fit_result.id,
                        request.candidate.candidate.id,
                        commit,
                        approval,
                    )
                )
            events = (
                *events,
                PromotionEvent(PromotionEventKind.DEPLOYED, now, deployment.id),
            )
        result = PromotionResult(
            request.id,
            request.policy.mode,
            fit_result.id,
            request.candidate.candidate.id,
            request.policy.digest,
            None if request.approval is None else request.approval.digest,
            branch,
            commit,
            pull_request,
            deployment,
            RollbackPlan(
                reverse_path,
                () if commit is None else ("git", "revert", commit.value),
            ),
            report_path,
            digest_bytes(report_payload),
            digest_bytes(reverse),
            (*events, PromotionEvent(PromotionEventKind.COMPLETED, now, request.id)),
            request.fit_result.root,
        )
        payload = f"{result.to_json()}\n".encode()
        write_artifact(result.manifest_path, payload)
        write_artifact(result.digest_path, _DIGEST_ADAPTER.dump_json(digest_bytes(payload)) + b"\n")
        return result

    def cancel(
        self,
        request: PromotionRequest,
        actor: ApproverId,
        now: datetime,
    ) -> CancellationRecord:
        now = _aware(now)
        if (request.root / "manifest.json").exists():
            raise PromotionError(PromotionErrorCode.ALREADY_COMPLETED, request.id)
        record = CancellationRecord(request.id, actor, now)
        write_artifact(request.root / "cancelled.json", f"{record.to_json()}\n".encode())
        return record

    def _validate_approval(self, request: PromotionRequest) -> None:
        approval = request.approval
        if request.policy.require_approval and approval is None:
            raise PromotionError(PromotionErrorCode.APPROVAL_REQUIRED, request.id)
        if approval is None:
            return
        if approval.decision is ApprovalDecision.REJECTED:
            raise PromotionError(PromotionErrorCode.APPROVAL_REJECTED, request.id)
        if (
            approval.fit_result_id != request.fit_result.id
            or approval.candidate_id != request.candidate.candidate.id
            or approval.policy_digest != request.policy.digest
        ):
            raise PromotionError(PromotionErrorCode.APPROVAL_INVALID, request.id)

    def _check_cancelled(self, request: PromotionRequest) -> None:
        path = request.root / "cancelled.json"
        if not path.exists():
            return
        try:
            record = _CANCELLATION_ADAPTER.validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path)) from error
        if record.promotion_id != request.id:
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path))
        raise PromotionError(PromotionErrorCode.CANCELLED, request.id)

    def _read_existing(self, request: PromotionRequest) -> PromotionResult | None:
        path = request.root / "manifest.json"
        if not path.exists():
            return None
        try:
            payload = path.read_bytes()
            expected = _DIGEST_ADAPTER.validate_json(path.with_suffix(".sha256").read_bytes())
            result = _RESULT_ADAPTER.validate_json(payload)
        except (OSError, ValidationError) as error:
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path)) from error
        if (
            digest_bytes(payload) != expected
            or result.id != request.id
            or result.fit_result_id != request.fit_result.id
            or result.candidate_id != request.candidate.candidate.id
            or result.policy_digest != request.policy.digest
        ):
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path))
        try:
            report_digest = digest_bytes(result.report_path.read_bytes())
            reverse_digest = digest_bytes(result.rollback.reverse_patch.read_bytes())
        except OSError as error:
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path)) from error
        if report_digest != result.report_digest or reverse_digest != result.reverse_digest:
            raise PromotionError(PromotionErrorCode.RESULT_INVALID, str(path))
        if result.commit is not None and result.branch is not None:
            _validate_commit(request, result.branch, result.commit)
        return result


@dataclass(frozen=True, slots=True)
class PromotionJobHandler:
    service: PromotionService
    requests: PromotionRequestResolver

    @property
    def kind(self) -> JobKind:
        return JobKind.PROMOTE

    def execute(self, job: JobSpec, context: JobContext) -> JobExecution:
        if job.kind is not JobKind.PROMOTE:
            raise JobExecutionError(
                FailureDisposition.TERMINAL,
                SchedulerErrorCode.RESULT_INVALID,
                Money(0),
            )
        fit_results = tuple(
            predecessor.result
            for predecessor in context.scheduler.predecessors(context.lease.job.id)
            if predecessor.spec.kind is JobKind.FIT and predecessor.result is not None
        )
        if len(fit_results) != 1:
            raise JobExecutionError(
                FailureDisposition.TERMINAL,
                SchedulerErrorCode.RESULT_INVALID,
                Money(0),
            )
        fit_result = fit_results[0]
        try:
            promotion = self.service.run(self.requests.resolve(fit_result.id), context.now)
        except PromotionError as error:
            disposition = (
                FailureDisposition.RETRYABLE
                if error.code
                in (
                    PromotionErrorCode.GIT_FAILED,
                    PromotionErrorCode.PUBLISH_FAILED,
                    PromotionErrorCode.DEPLOYMENT_FAILED,
                )
                else FailureDisposition.TERMINAL
            )
            raise JobExecutionError(
                disposition,
                SchedulerErrorCode.HANDLER_FAILED,
                Money(0),
            ) from error
        return JobExecution(
            JobResult(
                ResultId(promotion.id),
                JobKind.PROMOTE,
                job.revision_id,
                fit_result.id,
                None,
                True,
            ),
            Money(0),
        )


def _ensure_commit(request: PromotionRequest) -> tuple[GitCommit, PromotionBranch]:
    revision = request.campaign.harness.current_revision
    if revision is None:
        raise PromotionError(PromotionErrorCode.CANDIDATE_DRIFT, request.id)
    try:
        validate_candidate_artifacts(request.candidate.candidate, revision)
        validate_candidate_revision(request.candidate.candidate, revision)
    except CandidateError as error:
        raise PromotionError(PromotionErrorCode.CANDIDATE_DRIFT, error.subject) from error
    branch = _promotion_branch(request)
    worktree = request.candidate.workspace.parent / f"promotion-{request.id[-16:]}"
    existing = _branch_commit(request.candidate.workspace.source_root, branch)
    if existing is not None and _commit_matches(request, branch, existing):
        if worktree.exists():
            _remove_promotion_worktree(request, worktree)
        return existing, branch
    if existing is not None and not worktree.exists():
        _git(request.candidate.workspace.source_root, "branch", "-D", branch.value)
    try:
        if not worktree.exists():
            _git(
                request.candidate.workspace.source_root,
                "worktree",
                "add",
                "-b",
                branch.value,
                str(worktree),
                request.candidate.workspace.branch.value,
            )
        patch = request.candidate.candidate.diff_path.read_bytes()
        actual = _git_bytes(worktree, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
        if not actual:
            _git_with_input(worktree, patch, "apply", "--binary", "-")
            actual = _git_bytes(
                worktree,
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
            )
        if actual != patch:
            raise PromotionError(PromotionErrorCode.COMMIT_INVALID, request.id)
        _git(
            worktree,
            "add",
            "--",
            *(path.as_posix() for path in request.candidate.candidate.changed_files),
        )
        staged = _git_bytes(
            worktree,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
        )
        if staged != patch:
            raise PromotionError(PromotionErrorCode.COMMIT_INVALID, request.id)
        _git(
            worktree,
            "-c",
            "user.name=OpenFlyWheel",
            "-c",
            "user.email=openflywheel@localhost",
            "commit",
            "-m",
            _commit_message(request),
        )
        commit = GitCommit(_git_text(worktree, "rev-parse", "HEAD"))
        _validate_commit(request, branch, commit)
        return commit, branch
    except OSError as error:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, str(worktree)) from error
    finally:
        if worktree.exists():
            _remove_promotion_worktree(request, worktree)


def _validate_commit(
    request: PromotionRequest,
    branch: PromotionBranch,
    commit: GitCommit,
) -> None:
    if not _commit_matches(request, branch, commit):
        raise PromotionError(PromotionErrorCode.COMMIT_INVALID, request.id)


def _commit_matches(
    request: PromotionRequest,
    branch: PromotionBranch,
    commit: GitCommit,
) -> bool:
    root = request.candidate.workspace.source_root
    branch_commit = _git_text(root, "rev-parse", f"refs/heads/{branch.value}")
    message = _git_text(root, "show", "-s", "--format=%B", commit.value)
    patch = _git_bytes(
        root,
        "diff",
        "--binary",
        "--no-ext-diff",
        f"{commit.value}^",
        commit.value,
        "--",
    )
    try:
        expected = request.candidate.candidate.diff_path.read_bytes()
    except OSError as error:
        raise PromotionError(PromotionErrorCode.COMMIT_INVALID, request.id) from error
    return branch_commit == commit.value and request.marker.value in message and patch == expected


def _remove_promotion_worktree(request: PromotionRequest, worktree: Path) -> None:
    _git(
        request.candidate.workspace.source_root,
        "worktree",
        "remove",
        "--force",
        str(worktree),
    )
    shutil.rmtree(worktree, ignore_errors=True)


def _branch_commit(root: Path, branch: PromotionBranch) -> GitCommit | None:
    result = subprocess.run(  # nosec B603
        (
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch.value}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, branch.value)
    return GitCommit(result.stdout.strip())


def _push(root: Path, remote: GitRemote, branch: PromotionBranch) -> None:
    _git_text(root, "remote", "get-url", remote.name)
    _git(root, "push", remote.name, f"{branch.value}:refs/heads/{branch.value}")


def _report(request: PromotionRequest) -> str:
    candidate = request.candidate.candidate
    try:
        patch = candidate.diff_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromotionError(PromotionErrorCode.CANDIDATE_DRIFT, candidate.id.value) from error
    outcomes = "".join(
        "<tr>"
        f"<td>{html.escape(outcome.candidate_id.value)}</td>"
        f"<td>{html.escape(outcome.status.value)}</td>"
        f"<td>{html.escape(outcome.reason.value)}</td>"
        f"<td>{outcome.target_delta:.6f}</td>"
        f"<td>{outcome.regression_score:.6f}</td>"
        "</tr>"
        for outcome in request.fit_result.outcomes
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>OFW promotion</title>'
        "</head><body>"
        f"<h1>Promotion {html.escape(request.id)}</h1>"
        f"<p>Fit: {html.escape(request.fit_result.id)}</p>"
        f"<p>Winner: {html.escape(candidate.id.value)}</p>"
        f"<p>Policy: {html.escape(str(request.policy.digest))}</p>"
        "<table><thead><tr><th>Candidate</th><th>Status</th><th>Reason</th>"
        f"<th>Target delta</th><th>Regression</th></tr></thead><tbody>{outcomes}</tbody></table>"
        f"<h2>Patch</h2><pre>{html.escape(patch)}</pre>"
        "</body></html>"
    )


def _pull_request_body(request: PromotionRequest) -> str:
    return (
        f"{request.marker.value}\n\n"
        f"Fit result: `{request.fit_result.id}`\n\n"
        f"Candidate: `{request.candidate.candidate.id.value}`\n\n"
        f"Policy: `{request.policy.digest}`\n\n"
        "This PR is a review artifact. Merging it is not treated as a production deployment."
    )


def _promotion_branch(request: PromotionRequest) -> PromotionBranch:
    remote = request.policy.remote
    prefix = "ofw" if remote is None else remote.branch_prefix
    return PromotionBranch(f"{prefix}/promotion-{request.id[-16:]}")


def _commit_message(request: PromotionRequest) -> str:
    return (
        f"ofw: promote {request.candidate.candidate.id.value[-12:]}\n\n"
        f"{request.marker.value}\n"
        f"OFW-Fit-Result: {request.fit_result.id}\n"
        f"OFW-Policy: {request.policy.digest}"
    )


def _valid_git_name(value: str) -> bool:
    return (
        bool(value)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is not None
        and ".." not in value
        and not value.endswith("/")
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _command(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(  # nosec B603
        arguments,
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.PUBLISH_FAILED, arguments[0])
    return result


def _git(root: Path, *arguments: str) -> None:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, arguments[0])


def _git_text(root: Path, *arguments: str) -> str:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, arguments[0])
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, arguments[0])
    return result.stdout


def _git_with_input(root: Path, payload: bytes, *arguments: str) -> None:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        input=payload,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PromotionError(PromotionErrorCode.GIT_FAILED, arguments[0])
