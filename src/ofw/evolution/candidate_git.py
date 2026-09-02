"""Git isolation and exact-path sealing for one harness candidate."""

from __future__ import annotations

import stat
import subprocess  # nosec B404
from pathlib import Path, PurePosixPath

from ofw.evolution.candidate import (
    CandidateCommit,
    CandidateErrorCode,
    CandidateFailure,
    CandidateId,
    CandidateTree,
    CandidateWorkspace,
)
from ofw.evolution.hypothesis import HarnessHypothesis
from ofw.preparation.policy import ExperimentPolicySnapshot

_MANAGED_PATHS = frozenset(("PROGRAM.md", "experiment_config.yaml"))
_CREDENTIAL_NAMES = frozenset(
    (
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    )
)


class CandidateGitGateway:
    """Create a detached candidate worktree and commit only hypothesis targets."""

    def control_directory(self, root: Path, hypothesis_id: str) -> Path:
        common = Path(_git(root, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = root / common
        return common.resolve() / "ofw" / "candidates" / hypothesis_id.removeprefix("sha256:")

    def validate_accepted(
        self,
        root: Path,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> None:
        _validate_authority(_directory(root, "workspace_root"), policy, hypothesis)

    def prepare(
        self,
        accepted_root: Path,
        worktree_parent: Path,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> CandidateWorkspace:
        root = _directory(accepted_root, "workspace_root")
        parent = _directory(worktree_parent, "worktree_parent")
        _validate_authority(root, policy, hypothesis)
        worktree = parent / _worktree_name(root, policy, hypothesis)
        if worktree.exists():
            raise CandidateFailure(CandidateErrorCode.WORKTREE_EXISTS, str(worktree))
        _git(root, "worktree", "add", "--detach", str(worktree), policy.initialization_commit)
        return CandidateWorkspace(
            accepted_root=root,
            worktree_path=worktree,
            source_commit=policy.initialization_commit,
        )

    def inspect(
        self,
        workspace: CandidateWorkspace,
        policy: ExperimentPolicySnapshot,
        hypothesis: HarnessHypothesis,
    ) -> CandidateTree:
        _validate_authority(workspace.accepted_root, policy, hypothesis)
        _require_head(workspace.worktree_path, workspace.source_commit)
        changed = _changed_paths(workspace.worktree_path)
        if not changed:
            raise CandidateFailure(CandidateErrorCode.EMPTY_CANDIDATE, hypothesis.id.value)
        _validate_changes(workspace.worktree_path, changed, policy, hypothesis)
        _git(
            workspace.worktree_path,
            "add",
            "--all",
            "--",
            *(path.as_posix() for path in hypothesis.target.relative_paths),
        )
        return CandidateTree(
            tree_id=_git(workspace.worktree_path, "write-tree"),
            changed_paths=changed,
        )

    def commit(
        self,
        workspace: CandidateWorkspace,
        tree: CandidateTree,
        candidate_id: CandidateId,
        experiment_id: str,
    ) -> CandidateCommit:
        _require_head(workspace.worktree_path, workspace.source_commit)
        if (
            _changed_paths(workspace.worktree_path) != tree.changed_paths
            or not _git_succeeds(workspace.worktree_path, "diff", "--quiet")
            or _git(workspace.worktree_path, "write-tree") != tree.tree_id
        ):
            raise CandidateFailure(CandidateErrorCode.STALE_COMMIT, experiment_id)
        message = (
            "feat(ofw): record candidate execution\n\n"
            f"OFW-Experiment: {experiment_id}\n"
            f"OFW-Run: {candidate_id.value}"
        )
        _git(workspace.worktree_path, "commit", "-m", message)
        commit = _git(workspace.worktree_path, "rev-parse", "HEAD")
        if _changed_paths(workspace.worktree_path):
            raise CandidateFailure(CandidateErrorCode.STALE_COMMIT, experiment_id)
        return CandidateCommit(commit=commit)


def _validate_authority(
    root: Path,
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> None:
    _require_experiment(policy, hypothesis)
    _require_source(policy, hypothesis)
    _require_head(root, policy.initialization_commit)
    _require_branch(root, policy)
    _require_targets(policy, hypothesis)


def _require_experiment(
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> None:
    if hypothesis.experiment_id != policy.experiment_id:
        raise CandidateFailure(CandidateErrorCode.STALE_POLICY, hypothesis.id.value)


def _require_source(
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> None:
    if hypothesis.source_commit != policy.initialization_commit:
        raise CandidateFailure(CandidateErrorCode.STALE_COMMIT, hypothesis.id.value)


def _require_branch(root: Path, policy: ExperimentPolicySnapshot) -> None:
    if _git(root, "branch", "--show-current") != policy.branch_name:
        raise CandidateFailure(CandidateErrorCode.STALE_POLICY, policy.experiment_id)


def _require_targets(
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> None:
    if any(path not in policy.editable_paths for path in hypothesis.target.relative_paths):
        raise CandidateFailure(CandidateErrorCode.STALE_POLICY, hypothesis.id.value)


def _require_head(root: Path, expected: str) -> None:
    if _git(root, "rev-parse", "HEAD") != expected:
        raise CandidateFailure(CandidateErrorCode.STALE_COMMIT, expected)


def _changed_paths(root: Path) -> tuple[Path, ...]:
    tracked = _git_paths(root, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z")
    ignored = _git_paths(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    paths = tracked | untracked | ignored
    return tuple(sorted(paths, key=Path.as_posix))


def _git_paths(root: Path, *arguments: str) -> set[Path]:
    output = _git_bytes(root, *arguments)
    return {_path(value) for value in output.split(b"\0") if value}


def _path(value: bytes) -> Path:
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        raise CandidateFailure(CandidateErrorCode.UNSAFE_PATH, "encoding") from None
    pure = PurePosixPath(text)
    if pure.is_absolute() or pure == PurePosixPath(".") or ".." in pure.parts:
        raise CandidateFailure(CandidateErrorCode.UNSAFE_PATH, text)
    return Path(*pure.parts)


def _validate_changes(
    root: Path,
    changed: tuple[Path, ...],
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> None:
    targets = frozenset(hypothesis.target.relative_paths)
    editable = frozenset(policy.editable_paths)
    for path in changed:
        _validate_reserved_path(path)
        if path not in editable or path not in targets:
            raise CandidateFailure(CandidateErrorCode.OUT_OF_SCOPE, path.as_posix())
        _require_regular_path(root, path)


def _validate_reserved_path(path: Path) -> None:
    text = path.as_posix()
    if text in _MANAGED_PATHS or path.parts[0] == ".workspace":
        raise CandidateFailure(CandidateErrorCode.MANAGED_PATH, text)
    if any(_credential_name(part) for part in path.parts):
        raise CandidateFailure(CandidateErrorCode.CREDENTIAL_PATH, text)


def _credential_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == ".env" or lowered.startswith(".env.") or lowered in _CREDENTIAL_NAMES


def _require_regular_path(root: Path, relative: Path) -> None:
    try:
        _require_regular_parents(root, relative)
        path = root / relative
        metadata = path.lstat()
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise CandidateFailure(CandidateErrorCode.UNSAFE_PATH, relative.as_posix()) from None
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CandidateFailure(CandidateErrorCode.UNSAFE_PATH, relative.as_posix())


def _require_regular_parents(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        metadata = current.lstat()
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise OSError


def _directory(path: Path, subject: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CandidateFailure(CandidateErrorCode.INVALID_WORKSPACE, subject) from None
    if not resolved.is_dir():
        raise CandidateFailure(CandidateErrorCode.INVALID_WORKSPACE, subject)
    return resolved


def _worktree_name(
    root: Path,
    policy: ExperimentPolicySnapshot,
    hypothesis: HarnessHypothesis,
) -> str:
    suffix = hypothesis.id.value.removeprefix("sha256:")[:12]
    return f"{root.name}-ofw-{policy.experiment_id}-{suffix}"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CandidateFailure(CandidateErrorCode.GIT_FAILED, arguments[0])
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CandidateFailure(CandidateErrorCode.GIT_FAILED, arguments[0])
    return result.stdout


def _git_succeeds(root: Path, *arguments: str) -> bool:
    return (
        subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
