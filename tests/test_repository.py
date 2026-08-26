"""Whole-repository harness revision behavior."""

from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ofw import (
    HarnessErrorCode,
    HarnessRevision,
    HarnessValidationError,
    LangfuseProject,
    process_repository,
)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "fixture-agent"
    root.mkdir()
    (root / "agent.py").write_text("PROMPT = 'be accurate'\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root


def test_process_repository_creates_an_immutable_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    revision = process_repository("fixture-agent", root)

    assert isinstance(revision, HarnessRevision)
    assert str(revision.id) == str(revision.repository.commit)
    assert revision.root == root
    assert revision.manifest_path.is_file()
    assert revision.manifest_path.read_text(encoding="utf-8") == f"{revision.to_json()}\n"
    with pytest.raises(FrozenInstanceError):
        revision.harness_name = "changed"  # type: ignore[misc]


def test_repository_commit_dirty_diff_and_untracked_files_change_revision(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    clean = process_repository("fixture-agent", root)
    (root / "agent.py").write_text("PROMPT = 'be concise'\n", encoding="utf-8")
    dirty = process_repository("fixture-agent", root)
    (root / "new_skill.md").write_text("# New skill\n", encoding="utf-8")
    untracked = process_repository("fixture-agent", root)

    assert clean.id != dirty.id != untracked.id
    assert not clean.repository.is_dirty
    assert dirty.repository.is_dirty
    assert untracked.repository.is_dirty


def test_observability_connection_changes_revision_without_storing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    baseline = process_repository("fixture-agent", root)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-sensitive")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-sensitive")
    project = LangfuseProject.from_env(environment="production")

    connected = process_repository("fixture-agent", root, traces=project)

    assert connected.id == baseline.id
    assert connected.observability == project.manifest()
    assert "pk-sensitive" not in connected.to_json()
    assert "sk-sensitive" not in connected.to_json()


@pytest.mark.parametrize("name", ("", "contains spaces", "UPPERCASE"))
def test_invalid_repository_name_fails(name: str, tmp_path: Path) -> None:
    with pytest.raises(HarnessValidationError) as raised:
        process_repository(name, tmp_path)
    assert raised.value.code is HarnessErrorCode.INVALID_NAME


def test_root_must_be_a_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()

    with pytest.raises(HarnessValidationError) as raised:
        process_repository("fixture-agent", root)

    assert raised.value.code is HarnessErrorCode.GIT_REPOSITORY_REQUIRED
