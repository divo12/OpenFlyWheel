"""Platform installer merge and idempotency tests."""

import json
import shutil
from pathlib import Path

from openflywheel.connectors.agents.claude_installer import ClaudeCodeInstaller
from openflywheel.connectors.agents.cursor_installer import CursorInstaller
from openflywheel.connectors.agents.platform import generated_marker
from openflywheel.contracts.enums import PlatformKind


def test_claude_install_preserves_foreign_hooks(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent-transcripts"
        / "claude-settings-with-foreign-hooks.json"
    )
    target_home = tmp_path / "home"
    project_root = tmp_path / "project"
    target_home.mkdir()
    project_root.mkdir()
    settings = target_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    shutil.copy(fixture, settings)

    installer = ClaudeCodeInstaller()
    first = installer.install(target_home=str(target_home), project_root=str(project_root))
    second = installer.install(target_home=str(target_home), project_root=str(project_root))
    assert first.error is None and second.error is None
    assert first.data is not None and second.data is not None

    parsed = json.loads(settings.read_text(encoding="utf-8"))
    pre_tool = parsed["hooks"]["PreToolUse"]
    assert pre_tool[0]["hooks"][0]["command"] == "echo foreign-pre-tool"
    assert parsed["env"]["CUSTOM_FLAG"] == "keep-me"
    stop_hooks = parsed["hooks"]["Stop"]
    assert any(
        generated_marker(PlatformKind.CLAUDE_CODE) in json.dumps(entry) for entry in stop_hooks
    )


def test_claude_install_idempotent(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project_root = tmp_path / "project"
    target_home.mkdir()
    project_root.mkdir()
    installer = ClaudeCodeInstaller()
    first = installer.install(target_home=str(target_home), project_root=str(project_root))
    second = installer.install(target_home=str(target_home), project_root=str(project_root))
    assert first.error is None and second.error is None
    assert first.data is not None and second.data is not None
    assert len(first.data.installed_paths) >= 1
    assert len(second.data.skipped_existing) >= 1


def test_cursor_install_collision_safe_paths(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    target_home.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    foreign_rule = project_root / ".cursor" / "rules" / "team.mdc"
    foreign_rule.parent.mkdir(parents=True)
    foreign_rule.write_text("# team rule\n", encoding="utf-8")

    installer = CursorInstaller()
    result = installer.install(target_home=str(target_home), project_root=str(project_root))
    assert result.error is None
    assert (project_root / ".cursor" / "rules" / "openflywheel.mdc").exists()
    assert foreign_rule.exists()
    assert (project_root / ".cursor" / "openflywheel-hooks.json").exists()


def test_claude_uninstall_preserves_foreign_hooks(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent-transcripts"
        / "claude-settings-with-foreign-hooks.json"
    )
    target_home = tmp_path / "home"
    project_root = tmp_path / "project"
    target_home.mkdir()
    project_root.mkdir()
    settings = target_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    shutil.copy(fixture, settings)

    installer = ClaudeCodeInstaller()
    assert (
        installer.install(target_home=str(target_home), project_root=str(project_root)).error
        is None
    )
    uninstalled = installer.uninstall(target_home=str(target_home), project_root=str(project_root))
    assert uninstalled.error is None

    parsed = json.loads(settings.read_text(encoding="utf-8"))
    pre_tool = parsed["hooks"]["PreToolUse"]
    assert pre_tool[0]["hooks"][0]["command"] == "echo foreign-pre-tool"
    assert parsed["env"]["CUSTOM_FLAG"] == "keep-me"
    settings_blob = json.dumps(parsed)
    assert "ofw agent hook" not in settings_blob
    assert generated_marker(PlatformKind.CLAUDE_CODE) not in settings_blob


def test_claude_diagnostics_reports_installed_hooks(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project_root = tmp_path / "project"
    target_home.mkdir()
    project_root.mkdir()

    installer = ClaudeCodeInstaller()
    assert (
        installer.install(target_home=str(target_home), project_root=str(project_root)).error
        is None
    )
    diag = installer.diagnostics(target_home=str(target_home), project_root=str(project_root))
    assert diag.error is None
    assert diag.data is not None
    assert diag.data.installed is True
    assert len(diag.data.artifacts) >= 1


def test_cursor_install_foreign_rule_collision_fails(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    target_home.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    foreign_rule = project_root / ".cursor" / "rules" / "openflywheel.mdc"
    foreign_rule.parent.mkdir(parents=True)
    foreign_rule.write_text("# foreign team rule\n", encoding="utf-8")

    installer = CursorInstaller()
    result = installer.install(
        target_home=str(target_home),
        project_root=str(project_root),
    )
    assert result.error is not None
    assert result.error.code == "INSTALL_COLLISION"
    assert foreign_rule.read_text(encoding="utf-8") == "# foreign team rule\n"


def test_cursor_uninstall_preserves_foreign_hooks(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    target_home.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    hooks_path = project_root / ".cursor" / "openflywheel-hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        '{"version":1,"hooks":[{"event":"custom","command":"echo foreign-hook"}]}\n',
        encoding="utf-8",
    )

    installer = CursorInstaller()
    assert (
        installer.install(
            target_home=str(target_home),
            project_root=str(project_root),
        ).error
        is None
    )
    uninstalled = installer.uninstall(
        target_home=str(target_home),
        project_root=str(project_root),
    )
    assert uninstalled.error is None

    parsed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert parsed["hooks"][0]["command"] == "echo foreign-hook"
    assert "ofw agent hook" not in json.dumps(parsed)


def test_cursor_diagnostics_reports_installed_artifacts(tmp_path: Path) -> None:
    target_home = tmp_path / "home"
    project_root = tmp_path / "project"
    target_home.mkdir()
    project_root.mkdir()

    installer = CursorInstaller()
    assert (
        installer.install(
            target_home=str(target_home),
            project_root=str(project_root),
        ).error
        is None
    )
    diag = installer.diagnostics(target_home=str(target_home), project_root=str(project_root))
    assert diag.error is None
    assert diag.data is not None
    assert diag.data.installed is True
    assert len(diag.data.artifacts) >= 2
