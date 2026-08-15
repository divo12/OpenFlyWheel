"""CLI"""

from __future__ import annotations

import typer

from openflywheel.application.book_app import BookApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.paths import resolve_home
from openflywheel.mcp.stdio_runner import run_stdio_mcp_sync


def register(app: typer.Typer) -> None:
    @app.command("serve")
    def serve(
        surface: str = typer.Option(..., "--surface", help="MCP surface name"),
        home: str = typer.Option(..., help="Workspace home"),
    ) -> None:
        if surface != "verbs":
            raise typer.BadParameter("Only --surface verbs is supported in v0")
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        book = BookApplication(database)
        run_stdio_mcp_sync(book)
