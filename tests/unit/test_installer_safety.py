"""Installer safety tests."""

import json
from pathlib import Path

import pytest

from openflywheel.cli.commands.agent import _agent_home_for
from openflywheel.connectors.agents.claude_installer import ClaudeCodeInstaller
from openflywheel.connectors.agents.cursor_installer import CursorInstaller
from openflywheel.connectors.agents.path_guard import (
    resolve_install_paths,
    resolve_transcript_path,
    resolve_trusted_transcript_roots,
)
from openflywheel.connectors.agents.transcript import load_canonical_session
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId
from openflywheel.contracts.operation_result import OperationResult

SCHEMA_INVALID_CASES: tuple[tuple[str, dict[str, object], str], ...] = (
    ("claude", {"hooks": []}, "INSTALL_SETTINGS_SCHEMA_INVALID"),
    ("claude", {"hooks": "not-a-dict"}, "INSTALL_SETTINGS_SCHEMA_INVALID"),
    ("claude", {"hooks": {"Stop": "not-a-list"}}, "INSTALL_SETTINGS_SCHEMA_INVALID"),
    ("cursor", {"hooks": "not-a-list"}, "INSTALL_HOOKS_SCHEMA_INVALID"),
    ("cursor", {"hooks": [{"event": 1, "command": "ok"}]}, "INSTALL_HOOKS_SCHEMA_INVALID"),
    ("cursor", {"hooks": [42]}, "INSTALL_HOOKS_SCHEMA_INVALID"),
)


def test_install_rejects_path_traversal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = resolve_install_paths(
        target_home="../outside-home",
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "PATH_TRAVERSAL"


def test_install_collision_on_foreign_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    skill = project / ".claude" / "skills" / "openflywheel" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# foreign skill\n", encoding="utf-8")

    result = ClaudeCodeInstaller().install(target_home=str(home), project_root=str(project))
    assert result.error is not None
    assert result.error.code == "INSTALL_COLLISION"


def test_claude_install_malformed_settings_preserve_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = b"{ not-json"
    settings.write_bytes(original)

    result = ClaudeCodeInstaller().install(target_home=str(home), project_root=str(project))
    assert result.error is not None
    assert result.error.code == "INSTALL_SETTINGS_MALFORMED"
    assert settings.read_bytes() == original


def test_claude_install_malformed_settings_zero_artifacts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"{ not-json")
    skill = project / ".claude" / "skills" / "openflywheel" / "SKILL.md"

    result = ClaudeCodeInstaller().install(target_home=str(home), project_root=str(project))
    assert result.error is not None
    assert not skill.exists()


def test_claude_install_non_object_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps(["array"]).encode("utf-8")
    settings.write_bytes(original)

    result = ClaudeCodeInstaller().install(target_home=str(home), project_root=str(project))
    assert result.error is not None
    assert result.error.code == "INSTALL_SETTINGS_NOT_OBJECT"
    assert settings.read_bytes() == original
    assert not (project / ".claude" / "skills" / "openflywheel" / "SKILL.md").exists()


def test_claude_diagnostics_malformed_settings_typed_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = b"{ broken"
    settings.write_bytes(original)

    result = ClaudeCodeInstaller().diagnostics(
        target_home=str(home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_SETTINGS_MALFORMED"
    assert settings.read_bytes() == original


def test_claude_uninstall_malformed_settings_typed_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = b"{ broken"
    settings.write_bytes(original)

    result = ClaudeCodeInstaller().uninstall(
        target_home=str(home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_SETTINGS_MALFORMED"
    assert settings.read_bytes() == original


def test_cursor_install_malformed_hooks_zero_artifacts(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project = tmp_path / "project"
    target_home.mkdir()
    project.mkdir()
    hooks = project / ".cursor" / "openflywheel-hooks.json"
    hooks.parent.mkdir(parents=True)
    original = b"{ not-json"
    hooks.write_bytes(original)
    skill = project / ".cursor" / "skills" / "openflywheel" / "SKILL.md"
    rule = project / ".cursor" / "rules" / "openflywheel.mdc"

    result = CursorInstaller().install(
        target_home=str(target_home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_HOOKS_MALFORMED"
    assert hooks.read_bytes() == original
    assert not skill.exists()
    assert not rule.exists()


def test_cursor_install_non_object_hooks_zero_artifacts(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project = tmp_path / "project"
    target_home.mkdir()
    project.mkdir()
    hooks = project / ".cursor" / "openflywheel-hooks.json"
    hooks.parent.mkdir(parents=True)
    original = json.dumps([1, 2]).encode("utf-8")
    hooks.write_bytes(original)

    result = CursorInstaller().install(
        target_home=str(target_home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_HOOKS_NOT_OBJECT"
    assert hooks.read_bytes() == original
    assert not (project / ".cursor" / "skills" / "openflywheel" / "SKILL.md").exists()


def test_cursor_diagnostics_malformed_hooks_typed_error(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project = tmp_path / "project"
    target_home.mkdir()
    project.mkdir()
    hooks = project / ".cursor" / "openflywheel-hooks.json"
    hooks.parent.mkdir(parents=True)
    original = b"[1,2,3"
    hooks.write_bytes(original)

    result = CursorInstaller().diagnostics(
        target_home=str(target_home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_HOOKS_MALFORMED"
    assert hooks.read_bytes() == original


def test_cursor_uninstall_malformed_hooks_typed_error(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project = tmp_path / "project"
    target_home.mkdir()
    project.mkdir()
    hooks = project / ".cursor" / "openflywheel-hooks.json"
    hooks.parent.mkdir(parents=True)
    original = json.dumps("string-root").encode("utf-8")
    hooks.write_bytes(original)

    result = CursorInstaller().uninstall(
        target_home=str(target_home),
        project_root=str(project),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_HOOKS_NOT_OBJECT"
    assert hooks.read_bytes() == original


def test_claude_default_agent_home_trusted_root_accepts_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    project_root = tmp_path / "project"
    project_root.mkdir()

    agent_home = _agent_home_for(PlatformKind.CLAUDE_CODE)
    assert agent_home.endswith(".claude")

    transcript_dir = Path(agent_home) / "projects" / "sess-default"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )

    roots = resolve_trusted_transcript_roots(
        agent_home=agent_home,
        project_root=str(project_root),
    )
    assert roots.error is None
    assert roots.data is not None

    validated = resolve_transcript_path(
        transcript_path=str(transcript.resolve()),
        allowed_roots=roots.data,
    )
    assert validated.error is None

    loaded = load_canonical_session(
        platform=PlatformKind.CLAUDE_CODE,
        transcript_path=transcript,
        session_ref="sess-default",
        session_id=AgentSessionId("sess-default-id"),
        allowed_roots=roots.data,
    )
    assert loaded.error is None
    assert loaded.data is not None


def _claude_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    return home, project, settings


def _cursor_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    target_home = tmp_path / "home"
    project = tmp_path / "project"
    target_home.mkdir()
    project.mkdir()
    hooks = project / ".cursor" / "openflywheel-hooks.json"
    hooks.parent.mkdir(parents=True)
    return target_home, project, hooks


def _run_install(platform: str, home: Path, project: Path) -> OperationResult[object]:
    if platform == "claude":
        return ClaudeCodeInstaller().install(target_home=str(home), project_root=str(project))
    return CursorInstaller().install(target_home=str(home), project_root=str(project))


def _run_uninstall(platform: str, home: Path, project: Path) -> OperationResult[object]:
    if platform == "claude":
        return ClaudeCodeInstaller().uninstall(target_home=str(home), project_root=str(project))
    return CursorInstaller().uninstall(target_home=str(home), project_root=str(project))


def _run_diagnostics(platform: str, home: Path, project: Path) -> OperationResult[object]:
    if platform == "claude":
        return ClaudeCodeInstaller().diagnostics(
            target_home=str(home),
            project_root=str(project),
        )
    return CursorInstaller().diagnostics(
        target_home=str(home),
        project_root=str(project),
    )


def _artifact_paths(platform: str, project: Path) -> tuple[Path, ...]:
    if platform == "claude":
        return (project / ".claude" / "skills" / "openflywheel" / "SKILL.md",)
    return (
        project / ".cursor" / "skills" / "openflywheel" / "SKILL.md",
        project / ".cursor" / "rules" / "openflywheel.mdc",
    )


@pytest.mark.parametrize(("platform", "payload", "error_code"), SCHEMA_INVALID_CASES)
@pytest.mark.parametrize("operation", ("install", "uninstall", "diagnostics"))
def test_schema_invalid_json_returns_typed_failure_without_mutation(
    tmp_path: Path,
    platform: str,
    payload: dict[str, object],
    error_code: str,
    operation: str,
) -> None:
    if platform == "claude":
        home, project, config_path = _claude_paths(tmp_path)
    else:
        home, project, config_path = _cursor_paths(tmp_path)

    original = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    if operation == "install":
        config_path.write_bytes(original)
        result = _run_install(platform, home, project)
        for artifact in _artifact_paths(platform, project):
            assert not artifact.exists()
    elif operation == "uninstall":
        assert _run_install(platform, home, project).error is None
        pre_uninstall = {
            path: path.read_bytes() for path in _artifact_paths(platform, project) if path.exists()
        }
        config_path.write_bytes(original)
        result = _run_uninstall(platform, home, project)
        for path, before in pre_uninstall.items():
            assert path.exists()
            assert path.read_bytes() == before
    else:
        config_path.write_bytes(original)
        result = _run_diagnostics(platform, home, project)

    assert result.error is not None
    assert result.error.code == error_code
    assert config_path.read_bytes() == original
