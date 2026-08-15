"""CLI"""

from __future__ import annotations

import typer

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.emit import emit_result
from openflywheel.cli.typer_options import enum_default, flag_bool, required_str
from openflywheel.contracts.enums import DeploymentMode
from openflywheel.contracts.workspace import WorkspaceInitRequest


def register(app: typer.Typer) -> None:
    @app.command("init")
    def workspace_init(
        name: str = required_str(help="Workspace or company name"),
        home: str = required_str(help="Workspace home directory"),
        deployment_mode: DeploymentMode = enum_default(
            DeploymentMode.LOCAL, help="Deployment mode"
        ),
        force: bool = flag_bool(False, help="Reinitialize an existing workspace home"),
    ) -> None:
        service = WorkspaceService()
        request = WorkspaceInitRequest(
            name=name,
            home=home,
            deployment_mode=deployment_mode,
            force=force,
        )
        emit_result(service.init_workspace(request))
