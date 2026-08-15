"""CLI smoke tests."""

import json
from pathlib import Path

from typer.testing import CliRunner

from openflywheel.cli.main import app
from openflywheel.contracts.enums import VisibilityLevel
from openflywheel.contracts.workspace import WorkspacePolicy

runner = CliRunner()


def test_ofw_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "OpenFlyWheel" in result.stdout


def test_workspace_init_cli(tmp_path: Path) -> None:
    home = tmp_path / "fixture-co"
    result = runner.invoke(
        app,
        [
            "workspace",
            "init",
            "--name",
            "FixtureCo",
            "--home",
            str(home),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert (home / "book.sqlite").is_file()
    assert (home / "workspace.json").is_file()


def test_workspace_init_refuses_existing_home(tmp_path: Path) -> None:
    home = tmp_path / "fixture-co"
    first = runner.invoke(
        app,
        ["workspace", "init", "--name", "FixtureCo", "--home", str(home)],
    )
    assert first.exit_code == 0
    second = runner.invoke(
        app,
        ["workspace", "init", "--name", "FixtureCo", "--home", str(home)],
    )
    assert second.exit_code == 1
    payload = json.loads(second.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "WORKSPACE_EXISTS"


def test_workspace_policy_uses_visibility_enum() -> None:
    policy = WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL)
    assert policy.default_visibility == VisibilityLevel.INTERNAL


def test_book_view_rejects_non_loopback_bind(tmp_path: Path) -> None:
    home = tmp_path / "fixture-co"
    init = runner.invoke(
        app,
        ["workspace", "init", "--name", "FixtureCo", "--home", str(home)],
    )
    assert init.exit_code == 0
    result = runner.invoke(
        app,
        ["book", "view", "--home", str(home), "--host", "0.0.0.0"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "DASHBOARD_BIND_FORBIDDEN"
