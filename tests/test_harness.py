"""Component-observable harness revision behavior."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from ofw import (
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
    (root / "policy.md").write_text("Cite sources.\n", encoding="utf-8")
    (root / "agent.py").write_text("class AgentLoop:\n    pass\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root


def _configured_harness(root: Path) -> Harness:
    harness = Harness("fixtureco-research-agent", root=root)
    harness.connect_context(Path("policy.md"), ofw.editable(Path("prompt.md")))
    harness.connect_lifecycle(Path("agent.py"))
    return harness


def _required_component(revision: HarnessRevision, kind: ComponentKind) -> HarnessComponent:
    component = revision.component(kind)
    assert component is not None
    return component


def test_process_creates_typed_immutable_revision_and_manifest(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _configured_harness(root).process()

    assert isinstance(revision, HarnessRevision)
    assert all(isinstance(component, HarnessComponent) for component in revision.components)
    assert all(isinstance(asset, HarnessAsset) for asset in revision.assets)
    assert all(isinstance(asset.source, WorkspaceFile) for asset in revision.assets)
    assert Path("prompt.md") in revision.editable_files
    assert Path("policy.md") in revision.frozen_files
    assert Path("agent.py") in revision.frozen_files
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


def test_process_records_seven_file_level_components_for_polyglot_agent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    files = (
        Path("instructions.md"),
        Path("memory.py"),
        Path("skills/research/SKILL.md"),
        Path("sandbox/__init__.py"),
        Path("pyproject.toml"),
        Path("tools/search.ts"),
        Path("tools/worker.go"),
        Path("tool_descriptions/search.yaml"),
        Path("channels/chat.ts"),
        Path("connectors/mcp.go"),
        Path("schedules/nightly.ts"),
        Path("connectors/otel.ts"),
        Path("evals/tasks/factual/task.toml"),
        Path("middleware/retry.ts"),
        Path("identity.py"),
    )
    for relative_path in files:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture: {relative_path.as_posix()}\n", encoding="utf-8")

    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(
        ofw.editable(Path("instructions.md")),
        ofw.editable(Path("memory.py")),
        ofw.editable(Path("skills/research/SKILL.md")),
    )
    harness.connect_execute(Path("sandbox/__init__.py"), Path("pyproject.toml"))
    harness.connect_tools(
        ofw.editable(Path("tools/search.ts")),
        ofw.editable(Path("tools/worker.go")),
        ofw.editable(Path("tool_descriptions/search.yaml")),
        ofw.editable(Path("channels/chat.ts")),
        ofw.editable(Path("connectors/mcp.go")),
        ofw.editable(Path("schedules/nightly.ts")),
    )
    harness.connect_observability(Path("connectors/otel.ts"))
    harness.connect_verifiers(ofw.mine_managed(Path("evals/tasks/factual/task.toml")))
    harness.connect_lifecycle(Path("agent.py"), ofw.editable(Path("middleware/retry.ts")))
    harness.connect_governance(Path("identity.py"))

    revision = harness.process()

    assert {component.kind for component in revision.components} == set(ComponentKind)
    assert all(component.assets for component in revision.components)
    assert all(str(component.digest).startswith("sha256:") for component in revision.components)
    assert revision.mine_managed_files == (Path("evals/tasks/factual/task.toml"),)
    assert Path("tools/search.ts") in revision.editable_files
    assert Path("tools/worker.go") in revision.editable_files
    assert Path("connectors/mcp.go") in revision.editable_files
    assert Path("connectors/otel.ts") in revision.frozen_files


def test_component_fingerprint_localizes_a_tool_change(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    tool = root / "tool.ts"
    tool.write_text("export const run = () => 1;\n", encoding="utf-8")
    first_harness = _configured_harness(root)
    first_harness.connect_tools(ofw.editable(Path("tool.ts")))
    first = first_harness.process()

    tool.write_text("export const run = () => 2;\n", encoding="utf-8")
    second_harness = _configured_harness(root)
    second_harness.connect_tools(ofw.editable(Path("tool.ts")))
    second = second_harness.process()

    assert (
        _required_component(first, ComponentKind.TOOLING).digest
        != _required_component(second, ComponentKind.TOOLING).digest
    )
    assert (
        _required_component(first, ComponentKind.CONTEXT).digest
        == _required_component(second, ComponentKind.CONTEXT).digest
    )
    assert (
        _required_component(first, ComponentKind.LIFECYCLE).digest
        == _required_component(second, ComponentKind.LIFECYCLE).digest
    )


def test_adding_middleware_does_not_change_context_component(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    middleware = root / "middleware.ts"
    middleware.write_text("export const beforeCall = () => {};\n", encoding="utf-8")
    first = _configured_harness(root).process()

    second_harness = Harness("fixtureco-research-agent", root=root)
    second_harness.connect_context(Path("policy.md"), ofw.editable(Path("prompt.md")))
    second_harness.connect_lifecycle(Path("agent.py"), ofw.editable(Path("middleware.ts")))
    second = second_harness.process()

    assert (
        _required_component(first, ComponentKind.CONTEXT).digest
        == _required_component(second, ComponentKind.CONTEXT).digest
    )
    assert (
        _required_component(first, ComponentKind.LIFECYCLE).digest
        != _required_component(second, ComponentKind.LIFECYCLE).digest
    )


def test_file_cannot_be_owned_by_two_components(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"))
    harness.connect_tools(ofw.editable(Path("prompt.md")))
    harness.connect_lifecycle(Path("agent.py"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.COMPONENT_OVERLAP


@pytest.mark.parametrize(
    "component",
    (
        ComponentKind.EXECUTION,
        ComponentKind.OBSERVABILITY,
        ComponentKind.VERIFIER,
        ComponentKind.GOVERNANCE,
    ),
)
def test_governed_components_reject_fit_edit_authority(
    component: ComponentKind,
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    editable_source = ofw.editable(Path("prompt.md"))
    harness = Harness("fixtureco-agent", root=root)

    with pytest.raises(HarnessValidationError) as raised:
        match component:
            case ComponentKind.EXECUTION:
                harness.connect_execute(cast(Path, editable_source))
            case ComponentKind.OBSERVABILITY:
                harness.connect_observability(cast(Path, editable_source))
            case ComponentKind.VERIFIER:
                harness.connect_verifiers(cast(Path, editable_source))
            case ComponentKind.GOVERNANCE:
                harness.connect_governance(cast(Path, editable_source))
            case _:
                pytest.fail(f"unexpected governed component: {component}")

    assert raised.value.code is HarnessErrorCode.ACCESS_NOT_ALLOWED


def test_environment_secret_file_is_never_fingerprinted(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    harness = _configured_harness(root)
    harness.connect_context(Path(".env"))

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
    harness.connect_context(source)

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
        harness.connect_context(source)
        with pytest.raises(HarnessValidationError) as raised:
            harness.process()
        assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


@pytest.mark.parametrize(
    ("sources", "code"),
    (
        (
            (Path("prompt.md"), Path("prompt.md")),
            HarnessErrorCode.DUPLICATE_ASSET,
        ),
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
    harness.connect_context(first, second)
    harness.connect_lifecycle(Path("agent.py"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is code


def test_process_requires_context_and_lifecycle(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    without_context = Harness("fixtureco-agent", root=root)
    without_context.connect_lifecycle(Path("agent.py"))
    without_lifecycle = Harness("fixtureco-agent", root=root)
    without_lifecycle.connect_context(Path("prompt.md"))

    with pytest.raises(HarnessValidationError) as missing_context:
        without_context.process()
    with pytest.raises(HarnessValidationError) as missing_lifecycle:
        without_lifecycle.process()
    assert missing_context.value.code is HarnessErrorCode.CONTEXT_REQUIRED
    assert missing_lifecycle.value.code is HarnessErrorCode.LIFECYCLE_REQUIRED


def test_lifecycle_source_outside_root_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    outside = tmp_path / "agent.ts"
    outside.write_text("export class AgentLoop {}\n", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"))
    harness.connect_lifecycle(outside)

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is HarnessErrorCode.PATH_OUTSIDE_ROOT


def test_root_must_be_git_repository(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()
    (root / "prompt.md").write_text("hello", encoding="utf-8")
    (root / "agent.go").write_text("package agent\n", encoding="utf-8")
    harness = Harness("fixtureco-agent", root=root)
    harness.connect_context(Path("prompt.md"))
    harness.connect_lifecycle(Path("agent.go"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()
    assert raised.value.code is HarnessErrorCode.GIT_REPOSITORY_REQUIRED
