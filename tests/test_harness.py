"""Component-observable harness revision behavior."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ofw import (
    AssetAccess,
    ComponentKind,
    EditableFile,
    Harness,
    HarnessAsset,
    HarnessComponent,
    HarnessErrorCode,
    HarnessRevision,
    HarnessValidationError,
    WorkspaceFile,
    ofw,
)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "fixtureco-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root


def _configured_harness(root: Path) -> Harness:
    harness = Harness("fixtureco-research-agent", root=root)
    harness.connect_prompt(ofw.editable(Path("prompt.md")))
    return harness


def _required_component(revision: HarnessRevision, kind: ComponentKind) -> HarnessComponent:
    component = revision.component(kind)
    assert component is not None
    return component


def test_process_creates_typed_immutable_revision_and_manifest(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = _configured_harness(root)

    assert not (root / ".ofw").exists()
    revision = harness.process()

    assert isinstance(revision, HarnessRevision)
    assert all(isinstance(component, HarnessComponent) for component in revision.components)
    assert all(isinstance(asset, HarnessAsset) for asset in revision.assets)
    assert all(isinstance(asset.source, WorkspaceFile) for asset in revision.assets)
    assert revision.editable_files == (Path("prompt.md"),)
    assert (
        revision.manifest_path == root / ".ofw" / "revisions" / str(revision.id) / "manifest.json"
    )
    assert revision.manifest_path.read_text(encoding="utf-8") == f"{revision.to_json()}\n"
    subprocess.run(
        (sys.executable, "-m", "json.tool", str(revision.manifest_path)),
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(FrozenInstanceError):
        revision.harness_name = "changed"  # type: ignore[misc]


def test_process_records_only_six_ahe_components_for_polyglot_agent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    files = (
        Path("tool_descriptions/search.yaml"),
        Path("tools/search.ts"),
        Path("tools/worker.go"),
        Path("skills/research/SKILL.md"),
        Path("subagents/reviewer.yaml"),
        Path("middleware/retry.ts"),
    )
    for relative_path in files:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative_path.as_posix()}\n", encoding="utf-8")

    harness = _configured_harness(root)
    harness.connect_tool_descriptions(
        ofw.editable(Path("tool_descriptions/search.yaml")),
    )
    harness.connect_tool_implementations(
        ofw.editable(Path("tools/search.ts")),
        ofw.editable(Path("tools/worker.go")),
    )
    harness.connect_skills(ofw.editable(Path("skills/research/SKILL.md")))
    harness.connect_subagents(ofw.editable(Path("subagents/reviewer.yaml")))
    harness.connect_middleware(ofw.editable(Path("middleware/retry.ts")))

    revision = harness.process()

    assert {component.kind for component in revision.components} == set(ComponentKind)
    assert len(revision.components) == 6
    assert all(component.assets for component in revision.components)
    assert all(str(component.digest).startswith("sha256:") for component in revision.components)
    assert Path("tool_descriptions/search.yaml") in revision.editable_files
    assert Path("tools/search.ts") in revision.editable_files
    assert Path("tools/worker.go") in revision.editable_files


def test_tool_description_and_implementation_are_distinct_components(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    description = root / "search.yaml"
    implementation = root / "search.ts"
    description.write_text("name: search\n", encoding="utf-8")
    implementation.write_text("export const search = () => [];\n", encoding="utf-8")
    harness = _configured_harness(root)
    harness.connect_tool_descriptions(ofw.editable(Path("search.yaml")))
    harness.connect_tool_implementations(ofw.editable(Path("search.ts")))

    revision = harness.process()

    description_component = _required_component(revision, ComponentKind.TOOL_DESCRIPTION)
    implementation_component = _required_component(revision, ComponentKind.TOOL_IMPLEMENTATION)
    assert description_component.assets[0].source.relative_path == Path("search.yaml")
    assert implementation_component.assets[0].source.relative_path == Path("search.ts")
    assert description_component.digest != implementation_component.digest


def test_component_fingerprint_localizes_a_tool_implementation_change(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    description = root / "search.yaml"
    implementation = root / "search.ts"
    description.write_text("name: search\n", encoding="utf-8")
    implementation.write_text("export const search = () => 1;\n", encoding="utf-8")
    first_harness = _configured_harness(root)
    first_harness.connect_tool_descriptions(ofw.editable(Path("search.yaml")))
    first_harness.connect_tool_implementations(ofw.editable(Path("search.ts")))
    first = first_harness.process()

    implementation.write_text("export const search = () => 2;\n", encoding="utf-8")
    second_harness = _configured_harness(root)
    second_harness.connect_tool_descriptions(ofw.editable(Path("search.yaml")))
    second_harness.connect_tool_implementations(ofw.editable(Path("search.ts")))
    second = second_harness.process()

    assert (
        _required_component(first, ComponentKind.TOOL_IMPLEMENTATION).digest
        != _required_component(second, ComponentKind.TOOL_IMPLEMENTATION).digest
    )
    assert (
        _required_component(first, ComponentKind.TOOL_DESCRIPTION).digest
        == _required_component(second, ComponentKind.TOOL_DESCRIPTION).digest
    )
    assert (
        _required_component(first, ComponentKind.PROMPT).digest
        == _required_component(second, ComponentKind.PROMPT).digest
    )


def test_adding_middleware_does_not_change_prompt_component(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    middleware = root / "middleware.ts"
    middleware.write_text("export const beforeCall = () => {};\n", encoding="utf-8")
    first = _configured_harness(root).process()
    second_harness = _configured_harness(root)
    second_harness.connect_middleware(ofw.editable(Path("middleware.ts")))
    second = second_harness.process()

    assert (
        _required_component(first, ComponentKind.PROMPT).digest
        == _required_component(second, ComponentKind.PROMPT).digest
    )
    assert second.component(ComponentKind.MIDDLEWARE) is not None


def test_file_cannot_be_owned_by_two_components(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = _configured_harness(root)
    harness.connect_skills(ofw.editable(Path("prompt.md")))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.COMPONENT_OVERLAP


def test_assets_are_frozen_unless_explicitly_editable(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    skill = root / "SKILL.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_skills(ofw.editable(Path("SKILL.md")))

    revision = harness.process()

    assert (
        _required_component(revision, ComponentKind.PROMPT).assets[0].access is AssetAccess.FROZEN
    )
    assert (
        _required_component(revision, ComponentKind.SKILL).assets[0].access
        is AssetAccess.FIT_EDITABLE
    )


def test_environment_secret_file_is_never_fingerprinted(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    harness = _configured_harness(root)
    harness.connect_prompt(Path(".env"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.SENSITIVE_ASSET


def test_same_inputs_produce_same_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    assert _configured_harness(root).process() == _configured_harness(root).process()


def test_file_change_produces_new_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    first = _configured_harness(root).process()
    (root / "prompt.md").write_text("Be accurate and concise.\n", encoding="utf-8")
    second = _configured_harness(root).process()

    assert second.id != first.id
    assert second.repository.is_dirty
    assert second.repository.dirty_digest is not None


def test_new_git_commit_produces_new_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    first = _configured_harness(root).process()
    (root / "README.md").write_text("Fixture repository.\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-qm", "document fixture")
    second = _configured_harness(root).process()

    assert second.id != first.id
    assert second.repository.commit != first.repository.commit
    assert not second.repository.is_dirty


@pytest.mark.parametrize("name", ("", "contains spaces", "UPPERCASE"))
def test_invalid_harness_name_fails(name: str, tmp_path: Path) -> None:
    with pytest.raises(HarnessValidationError) as raised:
        Harness(name, root=tmp_path)
    assert raised.value.code is HarnessErrorCode.INVALID_NAME


@pytest.mark.parametrize(
    ("source", "code"),
    (
        (Path("missing.md"), HarnessErrorCode.MISSING_ASSET),
        (Path("folder"), HarnessErrorCode.NOT_A_FILE),
    ),
)
def test_invalid_workspace_file_fails(
    source: Path,
    code: HarnessErrorCode,
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "folder").mkdir()
    harness = _configured_harness(root)
    harness.connect_skills(source)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is code


def test_path_and_symlink_escape_fail(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)

    for source in (outside, Path("linked.md")):
        harness = _configured_harness(root)
        harness.connect_skills(source)
        with pytest.raises(HarnessValidationError) as raised:
            harness.process()
        assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


@pytest.mark.parametrize(
    ("sources", "code"),
    (
        ((Path("prompt.md"), Path("prompt.md")), HarnessErrorCode.DUPLICATE_ASSET),
        (
            (Path("prompt.md"), ofw.editable(Path("prompt.md"))),
            HarnessErrorCode.CONFLICTING_ACCESS,
        ),
    ),
)
def test_duplicate_component_asset_fails(
    sources: tuple[Path | EditableFile, ...],
    code: HarnessErrorCode,
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    first, second = sources
    harness.connect_prompt(first, second)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is code


def test_process_requires_prompt(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    skill = root / "SKILL.md"
    skill.write_text("# Skill\n", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_skills(Path("SKILL.md"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is HarnessErrorCode.PROMPT_REQUIRED


def test_root_must_be_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()
    (root / "prompt.md").write_text("hello", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is HarnessErrorCode.GIT_REPOSITORY_REQUIRED
