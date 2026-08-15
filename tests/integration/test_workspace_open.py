"""Missing workspace home onboarding safety tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.main import app
from openflywheel.contracts.enums import OperationStatus

runner = CliRunner()


def test_open_workspace_missing_home_returns_typed_failure(tmp_path: Path) -> None:
    home = tmp_path / "absent-co"
    result = WorkspaceService().open_workspace(home)
    assert result.status == OperationStatus.ERROR
    assert result.error is not None
    assert result.error.code == "WORKSPACE_NOT_FOUND"
    assert not home.exists()


def test_onboard_connect_missing_home_cli_no_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "absent-co"
    result = runner.invoke(app, ["onboard", "connect", "--home", str(home)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "WORKSPACE_NOT_FOUND"
    assert not (home / "book.sqlite").exists()
    assert not (home / "workspace.json").exists()

    init = runner.invoke(
        app,
        ["workspace", "init", "--name", "FixtureCo", "--home", str(home)],
    )
    assert init.exit_code == 0
    assert (home / "book.sqlite").is_file()
    assert (home / "workspace.json").is_file()


def test_load_database_missing_home_raises_without_creating_files(tmp_path: Path) -> None:
    home = tmp_path / "absent-co"
    service = WorkspaceService()
    with pytest.raises(FileNotFoundError):
        service.load_database(home)
    assert not home.exists()
