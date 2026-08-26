"""Turn a complete git repository into an immutable harness revision."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess  # nosec B404
import tempfile
from pathlib import Path

from ofw.contracts import (
    GitCommit,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionContent,
    HarnessRevisionId,
    HarnessSchemaVersion,
    HarnessValidationError,
    RepositorySnapshot,
    Sha256Digest,
)
from ofw.observability.langfuse.contracts import LangfuseProject

_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def process_repository(
    name: str,
    root: Path,
    *,
    traces: LangfuseProject | None = None,
) -> HarnessRevision:
    """Snapshot a whole agent-harness repository without component mapping."""
    if _NAME_PATTERN.fullmatch(name) is None:
        raise HarnessValidationError(HarnessErrorCode.INVALID_NAME, name)
    selected_root = _resolve_root(root)
    content = HarnessRevisionContent(
        schema_version=HarnessSchemaVersion.V1,
        harness_name=name,
        repository=_snapshot_repository(selected_root),
        observability=None if traces is None else traces.manifest(),
    )
    revision_id = _revision_id(content.repository)
    revision = HarnessRevision(
        schema_version=content.schema_version,
        id=revision_id,
        harness_name=content.harness_name,
        root=selected_root,
        repository=content.repository,
        observability=content.observability,
    )
    _write_manifest(revision)
    return revision


def _revision_id(repository: RepositorySnapshot) -> HarnessRevisionId:
    if repository.dirty_digest is None:
        return HarnessRevisionId(str(repository.commit))
    return HarnessRevisionId(
        f"{repository.commit.value}-dirty-{repository.dirty_digest.value[7:23]}"
    )


def _resolve_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(root))
    try:
        resolved = root.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_FOUND, str(root)) from error
    if not resolved.is_dir():
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_DIRECTORY, str(resolved))
    return resolved


def _snapshot_repository(root: Path) -> RepositorySnapshot:
    top_level = _run_git(root, "rev-parse", "--show-toplevel", repository_probe=True)
    try:
        git_root = Path(top_level.decode().strip()).resolve(strict=True)
    except (UnicodeDecodeError, FileNotFoundError) as error:
        raise HarnessValidationError(
            HarnessErrorCode.GIT_REPOSITORY_REQUIRED,
            str(root),
        ) from error
    if git_root != root:
        raise HarnessValidationError(HarnessErrorCode.GIT_REPOSITORY_REQUIRED, str(root))
    commit_bytes = _run_git(root, "rev-parse", "HEAD")
    try:
        commit = GitCommit(commit_bytes.decode().strip())
    except UnicodeDecodeError as error:
        raise HarnessValidationError(
            HarnessErrorCode.GIT_COMMAND_FAILED,
            "rev-parse HEAD",
        ) from error
    dirty = _dirty_payload(root)
    return RepositorySnapshot(
        commit=commit,
        is_dirty=bool(dirty),
        dirty_digest=None if not dirty else _digest(dirty),
    )


def _dirty_payload(root: Path) -> bytes:
    payload = bytearray(_run_git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--"))
    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for encoded_path in sorted(item for item in untracked.split(b"\0") if item):
        relative = Path(os.fsdecode(encoded_path))
        if _ignored_internal_path(relative):
            continue
        path = root / relative
        content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        payload.extend(b"\0untracked\0")
        payload.extend(encoded_path)
        payload.extend(b"\0")
        payload.extend(content)
    return bytes(payload)


def _ignored_internal_path(path: Path) -> bool:
    return bool(path.parts) and (
        path.parts[0] == ".ofw"
        or any(part == ".env" or part.startswith(".env.") for part in path.parts)
    )


def _run_git(
    root: Path,
    *arguments: str,
    repository_probe: bool = False,
) -> bytes:
    try:
        result: subprocess.CompletedProcess[bytes] = subprocess.run(  # nosec B603
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise HarnessValidationError(
            HarnessErrorCode.GIT_COMMAND_FAILED,
            arguments[0],
        ) from error
    if result.returncode != 0:
        code = (
            HarnessErrorCode.GIT_REPOSITORY_REQUIRED
            if repository_probe
            else HarnessErrorCode.GIT_COMMAND_FAILED
        )
        raise HarnessValidationError(code, arguments[0])
    return result.stdout


def _digest(value: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value).hexdigest()}")


def _write_manifest(revision: HarnessRevision) -> None:
    path = revision.manifest_path
    payload = f"{revision.to_json()}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".json",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    except OSError as error:
        raise HarnessValidationError(
            HarnessErrorCode.MANIFEST_WRITE_FAILED,
            str(path),
        ) from error
