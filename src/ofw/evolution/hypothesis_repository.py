"""Git and safe-file repository for prepared-experiment hypotheses."""

from __future__ import annotations

import stat
import subprocess  # nosec B404
from pathlib import Path

from ofw.evolution.hypothesis import (
    HarnessHypothesis,
    HypothesisArtifact,
    HypothesisErrorCode,
    HypothesisFailure,
)
from ofw.preparation.policy import (
    ExperimentPolicySnapshot,
    FileExperimentPolicyRepository,
)
from ofw.safe_file import (
    SafeFileErrorCode,
    SafeFileFailure,
    open_directory_chain,
    publish_idempotent,
)

_HYPOTHESIS_LIMIT_BYTES = 64 * 1024
_WORKSPACE_MARKERS = ("PROGRAM.md", "experiment_config.yaml")


class FileHypothesisRepository:
    """Validate one prepared Git worktree and store immutable hypothesis JSON."""

    def load_policy(self, root: Path, experiment_id: str) -> ExperimentPolicySnapshot:
        return FileExperimentPolicyRepository().load(root, experiment_id)

    def validate_workspace(
        self,
        root: Path,
        policy: ExperimentPolicySnapshot,
        source_commit: str,
        paths: tuple[Path, ...],
    ) -> None:
        prepared_root = _prepared_root(root)
        if _git(prepared_root, "rev-parse", "HEAD") != source_commit:
            raise HypothesisFailure(HypothesisErrorCode.STALE_COMMIT, policy.experiment_id)
        if _git(prepared_root, "branch", "--show-current") != policy.branch_name:
            raise HypothesisFailure(HypothesisErrorCode.STALE_POLICY, policy.experiment_id)
        if _git(prepared_root, "status", "--porcelain=v1"):
            raise HypothesisFailure(HypothesisErrorCode.DIRTY_WORKSPACE, policy.experiment_id)
        for path in paths:
            _require_regular_target(prepared_root, path)

    def store(self, root: Path, hypothesis: HarnessHypothesis) -> Path:
        prepared_root = _prepared_root(root)
        artifact = HypothesisArtifact.from_hypothesis(hypothesis)
        content = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
        try:
            _ensure_workspace_ignore(prepared_root)
            with open_directory_chain(
                prepared_root,
                (".workspace", "hypotheses"),
                create=True,
            ) as directory:
                publish_idempotent(
                    directory,
                    f"{hypothesis.id.value}.json",
                    content,
                    maximum_bytes=_HYPOTHESIS_LIMIT_BYTES,
                    subject=hypothesis.id.value,
                )
        except SafeFileFailure as error:
            raise _storage_failure(error, hypothesis.id.value) from None
        except OSError:
            raise HypothesisFailure(
                HypothesisErrorCode.WRITE_FAILED,
                hypothesis.id.value,
            ) from None
        return Path(".workspace/hypotheses") / f"{hypothesis.id.value}.json"


def _prepared_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HypothesisFailure(HypothesisErrorCode.STALE_POLICY, "workspace_root") from None
    if not all(_regular_marker(resolved / name) for name in _WORKSPACE_MARKERS):
        raise HypothesisFailure(HypothesisErrorCode.STALE_POLICY, "workspace_root")
    return resolved


def _regular_marker(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _require_regular_target(root: Path, relative: Path) -> None:
    path = root / relative
    try:
        metadata = path.lstat()
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        raise HypothesisFailure(HypothesisErrorCode.INVALID_TARGET, relative.as_posix()) from None
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise HypothesisFailure(HypothesisErrorCode.INVALID_TARGET, relative.as_posix())


def _ensure_workspace_ignore(root: Path) -> None:
    with open_directory_chain(root, (".workspace",), create=True) as workspace:
        publish_idempotent(
            workspace,
            ".gitignore",
            b"*\n",
            maximum_bytes=16,
            subject=".workspace",
        )


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HypothesisFailure(HypothesisErrorCode.STALE_POLICY, arguments[0])
    return result.stdout.strip()


def _storage_failure(error: SafeFileFailure, hypothesis_id: str) -> HypothesisFailure:
    if error.code is SafeFileErrorCode.CONFLICT:
        code = HypothesisErrorCode.HYPOTHESIS_CONFLICT
    elif error.code is SafeFileErrorCode.TOO_LARGE:
        code = HypothesisErrorCode.HYPOTHESIS_TOO_LARGE
    else:
        code = HypothesisErrorCode.WRITE_FAILED
    return HypothesisFailure(code, hypothesis_id)
