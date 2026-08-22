"""Harness revision compilation behavior."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ofw import (
    AssetAccess,
    AssetKind,
    FileAssetSource,
    Harness,
    HarnessAsset,
    HarnessErrorCode,
    HarnessRevision,
    HarnessValidationError,
    Lifecycle,
    PythonClassAssetSource,
    ofw,
)


class FixtureLoopShape(Lifecycle):
    """Static test type for dynamically imported fixture loop classes."""


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _load_loop(
    root: Path,
    source: str = "from ofw import Lifecycle\n\nclass FixtureLoop(Lifecycle):\n    pass\n",
) -> type[FixtureLoopShape]:
    module_name = f"fixture_runtime_{uuid4().hex}"
    module_path = root / f"{module_name}.py"
    module_path.write_text(source, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        module: ModuleType = importlib.import_module(module_name)
    finally:
        sys.path.remove(str(root))
    return cast(type[FixtureLoopShape], module.FixtureLoop)


def _repository(tmp_path: Path) -> tuple[Path, type[FixtureLoopShape]]:
    root = tmp_path / "fixtureco-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "policy.md").write_text("Cite sources.\n", encoding="utf-8")
    loop = _load_loop(root)
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root, loop


def _configured_harness(root: Path, loop: type[FixtureLoopShape]) -> Harness:
    harness = Harness("fixtureco-research-agent", root=root)
    harness.connect_context(Path("policy.md"), ofw.editable(Path("prompt.md")))
    harness.connect_lifecycle(loop)
    return harness


def test_process_creates_typed_immutable_revision_and_manifest(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)

    revision = _configured_harness(root, loop).process()

    assert isinstance(revision, HarnessRevision)
    assert all(isinstance(asset, HarnessAsset) for asset in revision.assets)
    assert revision.editable_files == (Path("prompt.md"),)
    assert revision.frozen_files == (Path("policy.md"),)
    assert revision.manifest_path == root / ".ofw" / "revisions" / revision.id / "manifest.json"
    restored = HarnessRevision.model_validate_json(
        revision.manifest_path.read_text(encoding="utf-8")
    )
    assert restored == revision
    with pytest.raises(ValidationError):
        revision.harness_name = "changed"  # type: ignore[misc]


def test_process_records_explicit_asset_objects(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)

    revision = _configured_harness(root, loop).process()

    context_assets = tuple(asset for asset in revision.assets if asset.kind is AssetKind.CONTEXT)
    lifecycle_assets = tuple(
        asset for asset in revision.assets if asset.kind is AssetKind.LIFECYCLE
    )
    assert len(context_assets) == 2
    assert len(lifecycle_assets) == 1
    assert isinstance(context_assets[0].source, FileAssetSource)
    assert isinstance(lifecycle_assets[0].source, PythonClassAssetSource)
    assert context_assets[0].access is AssetAccess.FROZEN
    assert context_assets[1].access is AssetAccess.EDITABLE
    assert lifecycle_assets[0].access is AssetAccess.FROZEN


def test_same_inputs_produce_same_revision(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)

    first = _configured_harness(root, loop).process()
    second = _configured_harness(root, loop).process()

    assert second.id == first.id
    assert second == first


def test_file_change_produces_new_revision(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    first = _configured_harness(root, loop).process()

    (root / "prompt.md").write_text("Be accurate and concise.\n", encoding="utf-8")
    second = _configured_harness(root, loop).process()

    assert second.id != first.id
    assert second.repository.is_dirty
    assert second.repository.dirty_digest is not None


def test_new_git_commit_produces_new_revision(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    first = _configured_harness(root, loop).process()
    (root / "README.md").write_text("Fixture repository.\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-qm", "document fixture")

    second = _configured_harness(root, loop).process()

    assert second.id != first.id
    assert second.repository.commit != first.repository.commit
    assert not second.repository.is_dirty


@pytest.mark.parametrize(
    ("name", "code"),
    (
        ("", HarnessErrorCode.INVALID_NAME),
        ("contains spaces", HarnessErrorCode.INVALID_NAME),
        ("UPPERCASE", HarnessErrorCode.INVALID_NAME),
    ),
)
def test_invalid_harness_name_fails(name: str, code: HarnessErrorCode, tmp_path: Path) -> None:
    with pytest.raises(HarnessValidationError) as raised:
        Harness(name, root=tmp_path)
    assert raised.value.code is code


def test_missing_context_fails(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("missing.md"))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.MISSING_ASSET


def test_path_outside_root_fails(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(outside)
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


def test_symlink_escape_fails(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("linked.md"))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


def test_file_asset_must_be_regular_file(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    (root / "folder").mkdir()
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("folder"))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.NOT_A_FILE


def test_duplicate_asset_with_conflicting_access_fails(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"), ofw.editable(Path("prompt.md")))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.CONFLICTING_ACCESS


def test_duplicate_asset_with_same_access_fails(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"), Path("prompt.md"))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.DUPLICATE_ASSET


def test_process_requires_context_and_lifecycle(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    without_context = Harness("fixtureco-agent", root=root)
    without_context.connect_lifecycle(loop)
    without_lifecycle = Harness("fixtureco-agent", root=root)
    without_lifecycle.connect_context(Path("prompt.md"))

    with pytest.raises(HarnessValidationError) as missing_context:
        without_context.process()
    with pytest.raises(HarnessValidationError) as missing_lifecycle:
        without_lifecycle.process()

    assert missing_context.value.code is HarnessErrorCode.CONTEXT_REQUIRED
    assert missing_lifecycle.value.code is HarnessErrorCode.LIFECYCLE_REQUIRED


def test_only_one_lifecycle_can_be_connected(tmp_path: Path) -> None:
    root, loop = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.connect_lifecycle(loop)

    assert raised.value.code is HarnessErrorCode.LIFECYCLE_ALREADY_CONNECTED


def test_local_and_builtin_lifecycle_classes_fail(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)

    class LocalLoop(Lifecycle):
        pass

    for lifecycle in (LocalLoop, list):
        harness = Harness("fixtureco-agent", root=root)
        with pytest.raises(HarnessValidationError) as raised:
            harness.connect_lifecycle(lifecycle)
        assert raised.value.code is HarnessErrorCode.UNINSPECTABLE_LIFECYCLE


def test_non_class_lifecycle_fails_with_domain_error(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)

    def not_a_class() -> None:
        return None

    harness = Harness("fixtureco-agent", root=root)
    with pytest.raises(HarnessValidationError) as raised:
        harness.connect_lifecycle(cast(type[Lifecycle], not_a_class))

    assert raised.value.code is HarnessErrorCode.UNINSPECTABLE_LIFECYCLE


def test_lifecycle_source_outside_root_fails(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"))
    harness.connect_lifecycle(FixtureLoopShape)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


def test_root_must_be_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()
    (root / "prompt.md").write_text("hello", encoding="utf-8")
    loop = _load_loop(root)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"))
    harness.connect_lifecycle(loop)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.GIT_REPOSITORY_REQUIRED
