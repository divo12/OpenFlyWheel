"""CLI"""

from __future__ import annotations

import typer

from openflywheel.cli.commands import agent as agent_commands
from openflywheel.cli.commands import book as book_commands
from openflywheel.cli.commands import ingest as ingest_commands
from openflywheel.cli.commands import install as install_commands
from openflywheel.cli.commands import onboard as onboard_commands
from openflywheel.cli.commands import serve as serve_commands
from openflywheel.cli.commands import workspace as workspace_commands

app = typer.Typer(no_args_is_help=True, help="OpenFlyWheel System Book CLI")
workspace_app = typer.Typer(help="Workspace management")
onboard_app = typer.Typer(help="Staged onboarding workflow")
ingest_app = typer.Typer(help="Source ingest")
book_app = typer.Typer(help="System Book operations")
coverage_app = typer.Typer(help="Coverage reporting")

app.add_typer(workspace_app, name="workspace")
app.add_typer(onboard_app, name="onboard")
app.add_typer(ingest_app, name="ingest")
app.add_typer(book_app, name="book")
app.add_typer(coverage_app, name="coverage")

workspace_commands.register(workspace_app)
onboard_commands.register(onboard_app)
ingest_commands.register(ingest_app)
book_commands.register(book_app, coverage_app)
install_commands.register(app)
serve_commands.register(app)
agent_commands.register(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
