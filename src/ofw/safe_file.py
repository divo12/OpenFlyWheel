"""Small descriptor-anchored primitives for bounded immutable local files."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_READ_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class SafeFileErrorCode(StrEnum):
    CONFLICT = "conflict"
    DIRECTORY_CHANGED = "directory_changed"
    INVALID_FILE = "invalid_file"
    TOO_LARGE = "too_large"


class SafeFileFailure(Exception):
    """Sanitized failure from a local immutable-file boundary."""

    __slots__ = ("code", "subject")

    def __init__(self, code: SafeFileErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@contextmanager
def open_directory(path: Path) -> Iterator[int]:
    """Open one directory without following links and recheck its identity on exit."""
    with open_directory_chain(path, (), create=False) as descriptor:
        yield descriptor


@contextmanager
def open_child_directory(parent: int, name: str, *, create: bool) -> Iterator[int]:
    """Open one child directory and reject replacement while its descriptor is in use."""
    descriptor = _open_child(parent, name, create=create)
    try:
        _require_child_identity(parent, name, descriptor)
        yield descriptor
        _require_child_identity(parent, name, descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def open_directory_chain(
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> Iterator[int]:
    """Open a contained child chain and recheck every link before and after use."""
    anchor, existing_parts = _absolute_directory_parts(root)
    all_parts = existing_parts + parts
    anchor_descriptor = os.open(anchor, _DIRECTORY_FLAGS)
    descriptors = [anchor_descriptor]
    expected_anchor = _descriptor_identity(anchor_descriptor)
    try:
        for index, part in enumerate(all_parts):
            child_create = create and index >= len(existing_parts)
            descriptors.append(_open_child(descriptors[-1], part, create=child_create))
        _require_chain(anchor, all_parts, descriptors, expected_anchor)
        yield descriptors[-1]
        _require_chain(anchor, all_parts, descriptors, expected_anchor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_idempotent(
    directory: int,
    name: str,
    content: bytes,
    *,
    maximum_bytes: int,
    subject: str,
) -> None:
    """Publish new bytes once, accepting only an identical existing regular file."""
    _require_leaf(name)
    _require_size(content, maximum_bytes, subject)
    try:
        _publish_new_file(directory, name, content)
    except FileExistsError:
        if read_bounded(directory, name, maximum_bytes=maximum_bytes, subject=subject) != content:
            raise SafeFileFailure(SafeFileErrorCode.CONFLICT, subject) from None


def read_bounded(
    directory: int,
    name: str,
    *,
    maximum_bytes: int,
    subject: str,
) -> bytes:
    """Read one no-follow regular file through its containing directory descriptor."""
    _require_leaf(name)
    try:
        descriptor = os.open(name, _READ_FILE_FLAGS, dir_fd=directory)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise SafeFileFailure(SafeFileErrorCode.INVALID_FILE, subject) from None
    try:
        _require_regular_file(descriptor, subject)
        content = _read_descriptor(descriptor, maximum_bytes)
    finally:
        os.close(descriptor)
    _require_size(content, maximum_bytes, subject)
    return content


def _open_child(parent: int, name: str, *, create: bool) -> int:
    _require_leaf(name)
    if create:
        _mkdir_if_missing(parent, name)
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError:
        raise SafeFileFailure(SafeFileErrorCode.INVALID_FILE, name) from None


def _mkdir_if_missing(parent: int, name: str) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        return
    os.fsync(parent)


def _require_chain(
    root: Path,
    parts: tuple[str, ...],
    descriptors: list[int],
    expected_root: tuple[int, int],
) -> None:
    _require_path_identity(root, expected_root)
    for index, name in enumerate(parts):
        _require_child_identity(descriptors[index], name, descriptors[index + 1])


def _require_path_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = _stat_identity(os.stat(path, follow_symlinks=False))
    except OSError:
        raise SafeFileFailure(SafeFileErrorCode.DIRECTORY_CHANGED, path.name) from None
    if current != expected:
        raise SafeFileFailure(SafeFileErrorCode.DIRECTORY_CHANGED, path.name)


def _require_child_identity(parent: int, name: str, descriptor: int) -> None:
    try:
        current = _stat_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except OSError:
        raise SafeFileFailure(SafeFileErrorCode.DIRECTORY_CHANGED, name) from None
    if current != _descriptor_identity(descriptor):
        raise SafeFileFailure(SafeFileErrorCode.DIRECTORY_CHANGED, name)


def _publish_new_file(directory: int, name: str, content: bytes) -> None:
    temporary_name = f".ofw-{uuid4().hex}.tmp"
    published = False
    try:
        write_new_file(directory, temporary_name, content)
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


def write_new_file(directory: int, name: str, content: bytes) -> None:
    """Create and fsync one exclusive no-follow file under a directory descriptor."""
    _require_leaf(name)
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


def _read_descriptor(descriptor: int, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_regular_file(descriptor: int, subject: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise SafeFileFailure(SafeFileErrorCode.INVALID_FILE, subject)


def _require_size(content: bytes, maximum_bytes: int, subject: str) -> None:
    if len(content) > maximum_bytes:
        raise SafeFileFailure(SafeFileErrorCode.TOO_LARGE, subject)


def _require_leaf(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise SafeFileFailure(SafeFileErrorCode.INVALID_FILE, "file_name")


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    return _stat_identity(os.fstat(descriptor))


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _absolute_directory_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    if not path.is_absolute():
        raise SafeFileFailure(SafeFileErrorCode.INVALID_FILE, "directory")
    return Path(path.anchor), path.parts[1:]
