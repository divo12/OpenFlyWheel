"""Git worktree gateway for isolated OpenFlywheel experiment branches."""

from __future__ import annotations

import json
import subprocess  # nosec B404
from pathlib import Path

from ofw.preparation.contracts import (
    BaselineConfiguration,
    PreparationErrorCode,
    PreparationFailure,
    PreparedGitWorkspace,
    PrepareWorkspaceInput,
)

_PROGRAM_NAME = "PROGRAM.md"
_CONFIG_NAME = "experiment_config.yaml"


class GitWorktreeGateway:
    """Create one non-destructive experiment branch in a sibling worktree."""

    def control_directory(self, harness_root: Path, experiment_id: str) -> Path:
        common = _git(harness_root, "rev-parse", "--git-common-dir")
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = harness_root / common_path
        return common_path.resolve() / "ofw" / "preparations" / experiment_id

    def prepare(
        self,
        request: PrepareWorkspaceInput,
        program: str,
        baseline: BaselineConfiguration,
    ) -> PreparedGitWorkspace:
        _ensure_repository(request.harness_root)
        base_commit = _resolve_base_commit(request.harness_root, request.base_ref)
        branch_name = f"ofw/{request.experiment_id}"
        worktree = (
            request.worktree_parent
            / f"{request.harness_root.name}-ofw-{request.experiment_id}"
        )
        _ensure_available(
            request.harness_root,
            branch_name,
            worktree,
            base_commit,
            request.editable_paths,
        )
        _git(
            request.harness_root,
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree),
            base_commit,
        )
        return _initialize_worktree(request, baseline, program, branch_name, worktree, base_commit)


def _initialize_worktree(
    request: PrepareWorkspaceInput,
    baseline: BaselineConfiguration,
    program: str,
    branch_name: str,
    worktree: Path,
    base_commit: str,
) -> PreparedGitWorkspace:
    program_path = worktree / _PROGRAM_NAME
    config_path = worktree / _CONFIG_NAME
    program_path.write_text(program, encoding="utf-8")
    config_path.write_text(
        _render_experiment_config(request, branch_name, base_commit, baseline),
        encoding="utf-8",
    )
    _git(worktree, "add", _PROGRAM_NAME, _CONFIG_NAME)
    _git(worktree, "commit", "-m", f"chore(ofw): initialize {request.experiment_id}")
    initialization_commit = _git(worktree, "rev-parse", "HEAD")
    return PreparedGitWorkspace(
        branch_name,
        worktree,
        base_commit,
        initialization_commit,
        program_path,
    )


def _ensure_repository(root: Path) -> None:
    if _git_optional(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise PreparationFailure(PreparationErrorCode.INVALID_REPOSITORY, str(root))


def _resolve_base_commit(root: Path, base_ref: str) -> str:
    commit = _git_optional(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    if commit is None:
        raise PreparationFailure(PreparationErrorCode.BASE_REF_NOT_FOUND, base_ref)
    return commit


def _ensure_available(
    root: Path,
    branch_name: str,
    worktree: Path,
    base_commit: str,
    editable_paths: tuple[Path, ...],
) -> None:
    _ensure_branch_and_worktree_available(root, branch_name, worktree)
    _ensure_managed_files_absent(root, base_commit)
    _ensure_editable_paths_exist(root, base_commit, editable_paths)


def _ensure_branch_and_worktree_available(
    root: Path,
    branch_name: str,
    worktree: Path,
) -> None:
    if _git_optional(root, "show-ref", "--verify", f"refs/heads/{branch_name}") is not None:
        raise PreparationFailure(PreparationErrorCode.BRANCH_EXISTS, branch_name)
    if worktree.exists():
        raise PreparationFailure(PreparationErrorCode.WORKTREE_EXISTS, str(worktree))


def _ensure_managed_files_absent(root: Path, base_commit: str) -> None:
    for managed in (_PROGRAM_NAME, _CONFIG_NAME):
        if _git_object_exists(root, f"{base_commit}:{managed}"):
            raise PreparationFailure(PreparationErrorCode.MANAGED_FILE_EXISTS, managed)


def _ensure_editable_paths_exist(
    root: Path,
    base_commit: str,
    editable_paths: tuple[Path, ...],
) -> None:
    for editable in editable_paths:
        if not _git_object_exists(root, f"{base_commit}:{editable.as_posix()}"):
            raise PreparationFailure(
                PreparationErrorCode.EDITABLE_PATH_MISSING,
                editable.as_posix(),
            )


def _git_object_exists(root: Path, object_name: str) -> bool:
    return (
        subprocess.run(
            ("git", "-C", str(root), "cat-file", "-e", object_name),
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PreparationFailure(PreparationErrorCode.GIT_FAILED, arguments[0])
    return result.stdout.strip()


def _git_optional(root: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _render_experiment_config(
    request: PrepareWorkspaceInput,
    branch_name: str,
    base_commit: str,
    baseline: BaselineConfiguration,
) -> str:
    editable = "\n".join(f"    - {_yaml(path.as_posix())}" for path in request.editable_paths)
    return (
        "schema_version: 1\n"
        f"experiment_id: {_yaml(request.experiment_id)}\n"
        "benchmark: itsm-bench\n"
        "itsm:\n"
        f"  root: {_yaml(str(request.benchmark_root))}\n"
        f"  harbor_executable: {_yaml(str(request.harbor_executable))}\n"
        f"  harbor_config: {_yaml(request.harbor_config.as_posix())}\n"
        f"  job_name: {_yaml(request.experiment_id)}\n"
        f"  expected_task_count: {baseline.task_count}\n"
        f"  reuse_existing_baseline: {str(request.reuse_existing_baseline).lower()}\n"
        "harness:\n"
        f"  branch: {_yaml(branch_name)}\n"
        f"  base_commit: {_yaml(base_commit)}\n"
        "  editable_paths:\n"
        f"{editable}\n"
        "goal:\n"
        f"  statement: {_yaml(request.goal)}\n"
        f"  quality_target: {request.quality_target}\n"
        f"  max_cost_per_task_usd: {_optional_number(request.max_cost_per_task_usd)}\n"
        f"  max_latency_seconds: {_optional_number(request.max_latency_seconds)}\n"
        f"  max_iterations: {request.max_iterations}\n"
        f"  no_improvement_limit: {request.no_improvement_limit}\n"
        "execution:\n"
        f"  model: {_yaml(baseline.model)}\n"
        "  concurrency: 1\n"
        "  max_retries: 0\n"
        "observability:\n"
        "  provider: langfuse\n"
        "  environment: itsm-bench\n"
        f"  session_id: {_yaml(request.experiment_id)}\n"
        "verifier:\n"
        "  provider: itsm-bench\n"
    )


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _optional_number(value: float | None) -> str:
    return "null" if value is None else str(value)
