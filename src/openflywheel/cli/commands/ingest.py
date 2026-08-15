"""CLI"""

from __future__ import annotations

import typer

from openflywheel.application.book_app import BookApplication
from openflywheel.application.ingest_app import IngestApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.emit import emit_result
from openflywheel.cli.paths import resolve_home
from openflywheel.cli.typer_options import default_list, required_str


def register(app: typer.Typer) -> None:
    @app.command("run")
    def ingest_run(
        home: str = required_str(help="Workspace home"),
        fixture_root: str = required_str(help="Fixture GitHub root"),
        exclude: list[str] = default_list(
            [],
            help="Additional excluded path prefixes (locked exclusions always apply)",
        ),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        app_service = IngestApplication(database)
        emit_result(
            app_service.run_fixture_ingest(
                workspace_id=config.workspace_id,
                fixture_root=resolve_home(fixture_root),
                cli_excluded_paths=tuple(exclude),
            )
        )

    @app.command("extract")
    def ingest_extract(
        home: str = typer.Option(..., help="Workspace home"),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(book.extract(workspace_id=config.workspace_id))
