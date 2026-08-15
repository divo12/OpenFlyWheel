"""Test helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.enums import DeploymentMode
from openflywheel.contracts.workspace import WorkspaceInitRequest


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "tiny-system"


@pytest.fixture
def workspace_home(tmp_path: Path) -> Path:
    home = tmp_path / "fixture-co"
    service = WorkspaceService()
    result = service.init_workspace(
        WorkspaceInitRequest(
            name="FixtureCo",
            home=str(home),
            deployment_mode=DeploymentMode.LOCAL,
        )
    )
    assert result.error is None
    return home
