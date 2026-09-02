"""Safe immutable artifacts in a prepared harness workspace."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Never
from uuid import uuid4

_ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DIRECTORY_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_ARTIFACT_LIMIT_BYTES = 64 * 1024
_WORKSPACE_DIRECTORY = ".workspace"
_IGNORE_CONTENT = b"*\n"
_WORKSPACE_MARKERS = ("PROGRAM.md", "experiment_config.yaml")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_READ_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class WorkspaceArtifactErrorCode(StrEnum):
    INVALID_WORKSPACE = "invalid_workspace"
    ARTIFACT_CONFLICT = "artifact_conflict"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    WRITE_FAILED = "write_failed"


class WorkspaceArtifactFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: WorkspaceArtifactErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class WorkspaceArtifactReceipt:
    artifact_id: str
    relative_path: Path


@dataclass(frozen=True, slots=True)
class _DirectoryChainIdentity:
    root: tuple[int, int]
    workspace: tuple[int, int]
    artifacts: tuple[int, int]


@dataclass(frozen=True, slots=True)
class FileWorkspaceArtifactStore:
    directory_name: str

    def __post_init__(self) -> None:
        if _DIRECTORY_NAME_PATTERN.fullmatch(self.directory_name) is None:
            raise ValueError("invalid artifact directory")

    def store(self, root: Path, artifact_id: str, content: bytes) -> WorkspaceArtifactReceipt:
        if _ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
            raise ValueError("invalid artifact id")
        try:
            return self._store(root, artifact_id, content)
        except WorkspaceArtifactFailure:
            raise
        except (OSError, RuntimeError, UnicodeError):
            raise WorkspaceArtifactFailure(
                WorkspaceArtifactErrorCode.WRITE_FAILED,
                artifact_id,
            ) from None

    def _store(
        self,
        root: Path,
        artifact_id: str,
        content: bytes,
    ) -> WorkspaceArtifactReceipt:
        prepared_root = _prepared_root(root)
        workspace, artifacts = _workspace_paths(prepared_root, self.directory_name)
        _validate_artifact_size(content, str(_ARTIFACT_LIMIT_BYTES))
        identity = _prepare_workspace_directories(prepared_root, workspace, artifacts)
        path = artifacts / f"{artifact_id}.json"
        receipt = WorkspaceArtifactReceipt(artifact_id, path.relative_to(prepared_root))
        with _artifact_directory_handle(
            prepared_root,
            self.directory_name,
            identity,
        ) as directory:
            _publish_or_validate(directory, path.name, content, artifact_id)
        return receipt


def _prepared_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        _invalid_workspace("workspace_root")
    if not all((resolved / name).is_file() for name in _WORKSPACE_MARKERS):
        _invalid_workspace("workspace_root")
    return resolved


def _workspace_paths(root: Path, directory_name: str) -> tuple[Path, Path]:
    workspace = root / _WORKSPACE_DIRECTORY
    artifacts = workspace / directory_name
    _require_contained(root, workspace.resolve(strict=False))
    _require_contained(root, artifacts.resolve(strict=False))
    if workspace.exists() and not workspace.is_dir():
        _invalid_workspace(_WORKSPACE_DIRECTORY)
    return workspace, artifacts


def _require_contained(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        _invalid_workspace(_WORKSPACE_DIRECTORY)


def _invalid_workspace(subject: str) -> Never:
    raise WorkspaceArtifactFailure(
        WorkspaceArtifactErrorCode.INVALID_WORKSPACE,
        subject,
    ) from None


def _validate_artifact_size(content: bytes, artifact_id: str) -> None:
    if len(content) > _ARTIFACT_LIMIT_BYTES:
        raise WorkspaceArtifactFailure(
            WorkspaceArtifactErrorCode.ARTIFACT_TOO_LARGE,
            artifact_id,
        )


def _prepare_workspace_directories(
    root: Path,
    workspace: Path,
    artifacts: Path,
) -> _DirectoryChainIdentity:
    artifacts.mkdir(parents=True, exist_ok=True)
    _require_contained(root, workspace.resolve(strict=True))
    _require_contained(root, artifacts.resolve(strict=True))
    with _directory_handle(workspace) as directory:
        _require_directory_identity(directory, workspace)
        _write_ignore_file(directory)
    return _DirectoryChainIdentity(
        root=_path_identity(root),
        workspace=_path_identity(workspace),
        artifacts=_path_identity(artifacts),
    )


def _write_ignore_file(directory: int) -> None:
    try:
        _write_new_file(directory, ".gitignore", _IGNORE_CONTENT)
    except FileExistsError:
        return


def _publish_or_validate(
    directory: int,
    name: str,
    expected: bytes,
    artifact_id: str,
) -> None:
    try:
        _publish_new_file(directory, name, expected)
    except FileExistsError:
        actual = _read_existing(directory, name, artifact_id)
        if actual != expected:
            raise WorkspaceArtifactFailure(
                WorkspaceArtifactErrorCode.ARTIFACT_CONFLICT,
                artifact_id,
            ) from None


def _publish_new_file(directory: int, name: str, content: bytes) -> None:
    temporary_name = f".ofw-{uuid4().hex}.tmp"
    published = False
    try:
        _write_new_file(directory, temporary_name, content)
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        published = True
    finally:
        _unlink_if_present(directory, temporary_name)
        if published:
            os.fsync(directory)


def _write_new_file(directory: int, name: str, content: bytes) -> None:
    descriptor = os.open(name, _CREATE_FILE_FLAGS, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _unlink_if_present(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return


def _read_existing(directory: int, name: str, artifact_id: str) -> bytes:
    descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        content = stream.read(_ARTIFACT_LIMIT_BYTES + 1)
    _validate_artifact_size(content, artifact_id)
    return content


@contextmanager
def _directory_handle(path: Path) -> Iterator[int]:
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _child_directory_handle(parent: int, name: str) -> Iterator[int]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _artifact_directory_handle(
    root: Path,
    directory_name: str,
    expected: _DirectoryChainIdentity,
) -> Iterator[int]:
    with (
        _directory_handle(root) as root_directory,
        _child_directory_handle(root_directory, _WORKSPACE_DIRECTORY) as workspace,
        _child_directory_handle(workspace, directory_name) as artifacts,
    ):
        _require_directory_chain(
            root,
            directory_name,
            root_directory,
            workspace,
            artifacts,
            expected,
        )
        yield artifacts
        _require_directory_chain(
            root,
            directory_name,
            root_directory,
            workspace,
            artifacts,
            expected,
        )


def _require_directory_chain(
    root: Path,
    directory_name: str,
    root_directory: int,
    workspace: int,
    artifacts: int,
    expected: _DirectoryChainIdentity,
) -> None:
    _require_directory_identity(root_directory, root, expected.root)
    _require_child_identity(root_directory, _WORKSPACE_DIRECTORY, workspace, expected.workspace)
    _require_child_identity(workspace, directory_name, artifacts, expected.artifacts)


def _require_directory_identity(
    descriptor: int,
    path: Path,
    expected: tuple[int, int] | None = None,
) -> None:
    opened = _descriptor_identity(descriptor)
    current = _path_identity(path)
    if opened != current or (expected is not None and opened != expected):
        raise OSError("workspace directory changed during artifact recording")


def _require_child_identity(
    parent: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    opened = _descriptor_identity(descriptor)
    current = _stat_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    if opened != current or opened != expected:
        raise OSError("workspace directory changed during artifact recording")


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    return _stat_identity(os.fstat(descriptor))


def _path_identity(path: Path) -> tuple[int, int]:
    return _stat_identity(os.stat(path, follow_symlinks=False))


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino
