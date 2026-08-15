"""Path confinement for installer and transcript access."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.operation_result import OperationResult

_BLOCKED_PREFIXES: frozenset[str] = frozenset(
    {"/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library", "/private/etc"}
)
_ALLOWED_AGENT_HIDDEN: frozenset[str] = frozenset({".claude", ".cursor"})
_SENSITIVE_HIDDEN: frozenset[str] = frozenset(
    {".env", ".ssh", ".gnupg", ".aws", ".netrc", ".docker", ".kube"}
)


class ResolvedPaths(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_home: Path
    project_root: Path


class DirectoryPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    pointer: str


class TranscriptPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    pointer: str


def resolve_install_paths(*, target_home: str, project_root: str) -> OperationResult[ResolvedPaths]:
    home = _resolve_directory(target_home, label="target_home")
    if home.error is not None:
        return OperationResult.failure(
            code=home.error.code,
            message=home.error.message,
            root_cause_hint=home.error.root_cause_hint,
            safe_retry=home.error.safe_retry,
            stop_condition=home.error.stop_condition,
        )
    project = _resolve_directory(project_root, label="project_root")
    if project.error is not None:
        return OperationResult.failure(
            code=project.error.code,
            message=project.error.message,
            root_cause_hint=project.error.root_cause_hint,
            safe_retry=project.error.safe_retry,
            stop_condition=project.error.stop_condition,
        )
    if home.data is None or project.data is None:
        return OperationResult.failure(
            code="PATH_INTERNAL",
            message="Resolved path data missing after validation",
            root_cause_hint="Report as an internal error",
            safe_retry=False,
            stop_condition="Contact maintainers",
        )
    if _is_dangerous_path(home.data.path) or _is_dangerous_path(project.data.path):
        return OperationResult.failure(
            code="PATH_UNSAFE",
            message="Refusing dangerous install path",
            root_cause_hint="Use explicit non-system directories",
            safe_retry=False,
            stop_condition="Choose a dedicated temp or project directory",
        )
    return OperationResult.success(
        summary="Install paths validated",
        data=ResolvedPaths(target_home=home.data.path, project_root=project.data.path),
    )


def resolve_trusted_transcript_roots(
    *,
    agent_home: str,
    project_root: str,
) -> OperationResult[tuple[Path, ...]]:
    paths = resolve_install_paths(target_home=agent_home, project_root=project_root)
    if paths.error is not None:
        return OperationResult.failure(
            code=paths.error.code,
            message=paths.error.message,
            root_cause_hint=paths.error.root_cause_hint,
            safe_retry=paths.error.safe_retry,
            stop_condition=paths.error.stop_condition,
        )
    if paths.data is None:
        return OperationResult.failure(
            code="PATH_INTERNAL",
            message="Trusted root resolution failed",
            root_cause_hint="Report as internal error",
            safe_retry=False,
            stop_condition="Contact maintainers",
        )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in (paths.data.target_home, paths.data.project_root):
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return OperationResult.success(
        summary="Trusted transcript roots resolved",
        data=tuple(roots),
    )


def validate_session_ref(session_ref: str) -> OperationResult[str]:
    trimmed = session_ref.strip()
    if not trimmed:
        return OperationResult.failure(
            code="SESSION_REF_EMPTY",
            message="Session reference must not be empty",
            root_cause_hint="Provide platform session id",
            safe_retry=False,
            stop_condition="Pass non-empty session_ref",
        )
    if "/" in trimmed or "\\" in trimmed or ".." in trimmed:
        return OperationResult.failure(
            code="SESSION_REF_SEPARATOR",
            message="Session reference must not contain path separators",
            root_cause_hint="Use opaque session id without / or \\",
            safe_retry=False,
            stop_condition="Remove path separators from session_ref",
        )
    return OperationResult.success(summary="Session reference valid", data=trimmed)


def resolve_transcript_path(
    *,
    transcript_path: str,
    allowed_roots: tuple[Path, ...],
) -> OperationResult[TranscriptPath]:
    if ".." in Path(transcript_path).parts:
        return OperationResult.failure(
            code="TRANSCRIPT_TRAVERSAL",
            message="Transcript path must not contain parent segments",
            root_cause_hint="Provide a direct path under an allowed root",
            safe_retry=False,
            stop_condition="Remove .. from transcript path",
        )
    candidate = Path(transcript_path)
    if not candidate.is_absolute():
        return OperationResult.failure(
            code="TRANSCRIPT_RELATIVE",
            message="Transcript path must be absolute",
            root_cause_hint="Resolve transcript to an absolute path under allowed roots",
            safe_retry=False,
            stop_condition="Provide absolute transcript path",
        )
    if _is_sensitive_transcript_path(candidate):
        return OperationResult.failure(
            code="TRANSCRIPT_SENSITIVE",
            message="Transcript path must not reference sensitive hidden directories",
            root_cause_hint="Use agent platform transcript locations only",
            safe_retry=False,
            stop_condition="Choose transcript under .claude or .cursor trees",
        )
    if _is_hidden_path(candidate):
        return OperationResult.failure(
            code="TRANSCRIPT_HIDDEN",
            message="Transcript path must not reference hidden segments",
            root_cause_hint="Use a non-hidden transcript path",
            safe_retry=False,
            stop_condition="Remove hidden path segments",
        )
    resolved = candidate.resolve()
    if not _under_any_root(resolved, allowed_roots):
        return OperationResult.failure(
            code="TRANSCRIPT_OUTSIDE_ROOT",
            message="Transcript path outside allowed roots",
            root_cause_hint="Confine transcripts to workspace or agent home",
            safe_retry=False,
            stop_condition="Use transcript under configured allowed roots",
        )
    if not resolved.is_file():
        return OperationResult.failure(
            code="TRANSCRIPT_NOT_FOUND",
            message="Transcript file not found",
            root_cause_hint="Verify transcript path exists and is a file",
            safe_retry=False,
            stop_condition="Provide readable transcript file",
        )
    return OperationResult.success(
        summary="Transcript path validated",
        data=TranscriptPath(path=resolved, pointer=str(resolved)),
    )


def _resolve_directory(raw: str, *, label: str) -> OperationResult[DirectoryPath]:
    if not raw.strip():
        return OperationResult.failure(
            code="PATH_EMPTY",
            message=f"{label} must not be empty",
            root_cause_hint="Pass explicit --target-home and --project-root",
            safe_retry=False,
            stop_condition=f"Provide {label}",
        )
    if ".." in Path(raw).parts:
        return OperationResult.failure(
            code="PATH_TRAVERSAL",
            message=f"{label} must not contain parent segments",
            root_cause_hint="Use a direct path without ..",
            safe_retry=False,
            stop_condition=f"Remove .. from {label}",
        )
    path = Path(raw).expanduser().resolve()
    if _is_hidden_path(path):
        return OperationResult.failure(
            code="PATH_HIDDEN",
            message=f"{label} must not be a hidden path",
            root_cause_hint="Use a visible directory",
            safe_retry=False,
            stop_condition=f"Choose non-hidden {label}",
        )
    if not path.is_dir():
        return OperationResult.failure(
            code="PATH_NOT_DIRECTORY",
            message=f"{label} is not a directory",
            root_cause_hint="Create the directory or fix the path",
            safe_retry=False,
            stop_condition=f"Provide existing directory for {label}",
        )
    return OperationResult.success(
        summary=f"{label} resolved",
        data=DirectoryPath(path=path, pointer=str(path)),
    )


def _under_any_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        root_resolved = root.resolve()
        try:
            path.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _is_hidden_path(path: Path) -> bool:
    for part in path.parts:
        if not part.startswith(".") or part in {".", ".."}:
            continue
        if part in _ALLOWED_AGENT_HIDDEN:
            continue
        return True
    return False


def _is_sensitive_transcript_path(path: Path) -> bool:
    return any(part in _SENSITIVE_HIDDEN for part in path.parts)


def _is_dangerous_path(path: Path) -> bool:
    resolved = str(path.resolve())
    if resolved in {"/", str(Path.home().resolve())}:
        return True
    return any(resolved.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
