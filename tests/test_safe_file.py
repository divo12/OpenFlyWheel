"""Shared descriptor-anchored immutable-file safety checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ofw.safe_file import (
    SafeFileErrorCode,
    SafeFileFailure,
    open_child_directory,
    open_directory,
    publish_idempotent,
    read_bounded,
)


def test_child_directory_swap_is_detected_without_redirecting_publication(
    tmp_path: Path,
) -> None:
    parent_path = tmp_path / "parent"
    child_path = parent_path / "child"
    moved_path = parent_path / "moved"
    replacement_path = parent_path / "replacement"
    child_path.mkdir(parents=True)
    replacement_path.mkdir()

    with (
        pytest.raises(SafeFileFailure) as raised,
        open_directory(parent_path) as parent,
        open_child_directory(parent, "child", create=False) as child,
    ):
        child_path.rename(moved_path)
        replacement_path.rename(child_path)
        publish_idempotent(
            child,
            "artifact.json",
            b"{}\n",
            maximum_bytes=16,
            subject="artifact",
        )

    assert raised.value.code is SafeFileErrorCode.DIRECTORY_CHANGED
    assert not (child_path / "artifact.json").exists()
    assert (moved_path / "artifact.json").read_bytes() == b"{}\n"


def test_regular_file_reader_rejects_device() -> None:
    with open_directory(Path("/dev")) as directory, pytest.raises(SafeFileFailure) as raised:
        read_bounded(directory, "null", maximum_bytes=16, subject="device")

    assert raised.value.code is SafeFileErrorCode.INVALID_FILE


def test_regular_file_reader_rejects_oversized_content(tmp_path: Path) -> None:
    directory_path = tmp_path / "control"
    directory_path.mkdir()
    (directory_path / "policy.json").write_bytes(b"oversized")

    with open_directory(directory_path) as directory, pytest.raises(SafeFileFailure) as raised:
        read_bounded(directory, "policy.json", maximum_bytes=4, subject="policy")

    assert raised.value.code is SafeFileErrorCode.TOO_LARGE


def test_directory_open_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    directory = real_parent / "control"
    directory.mkdir(parents=True)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(SafeFileFailure) as raised, open_directory(linked_parent / "control"):
        pytest.fail("symlinked ancestor was followed")

    assert raised.value.code is SafeFileErrorCode.INVALID_FILE


def test_directory_swap_is_detected_without_redirecting_publication(tmp_path: Path) -> None:
    directory_path = tmp_path / "control"
    moved_path = tmp_path / "moved"
    replacement_path = tmp_path / "replacement"
    directory_path.mkdir()
    replacement_path.mkdir()

    with pytest.raises(SafeFileFailure) as raised, open_directory(directory_path) as directory:
        directory_path.rename(moved_path)
        replacement_path.rename(directory_path)
        publish_idempotent(
            directory,
            "policy.json",
            b"{}\n",
            maximum_bytes=16,
            subject="policy",
        )

    assert raised.value.code is SafeFileErrorCode.DIRECTORY_CHANGED
    assert not (directory_path / "policy.json").exists()
    assert (moved_path / "policy.json").read_bytes() == b"{}\n"


def test_failed_atomic_link_leaves_no_publication_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_path = tmp_path / "control"
    directory_path.mkdir()

    def crash(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        del source, destination, src_dir_fd, dst_dir_fd, follow_symlinks
        raise OSError("simulated crash boundary")

    monkeypatch.setattr(os, "link", crash)

    with (
        open_directory(directory_path) as directory,
        pytest.raises(OSError, match="simulated crash boundary"),
    ):
        publish_idempotent(
            directory,
            "policy.json",
            b"{}\n",
            maximum_bytes=16,
            subject="policy",
        )

    assert tuple(directory_path.iterdir()) == ()


def test_new_content_is_rejected_before_any_file_write_when_oversized(tmp_path: Path) -> None:
    directory_path = tmp_path / "control"
    directory_path.mkdir()

    with open_directory(directory_path) as directory, pytest.raises(SafeFileFailure) as raised:
        publish_idempotent(
            directory,
            "policy.json",
            b"too large",
            maximum_bytes=4,
            subject="policy",
        )

    assert raised.value.code is SafeFileErrorCode.TOO_LARGE
    assert tuple(directory_path.iterdir()) == ()
