"""CLI"""

from __future__ import annotations

import typer

from openflywheel.cli.emit import emit_result
from openflywheel.cli.typer_options import required_str
from openflywheel.connectors.agents.claude_installer import get_claude_installer
from openflywheel.connectors.agents.cursor_installer import get_cursor_installer
from openflywheel.connectors.agents.platform import PlatformInstaller
from openflywheel.contracts.enums import PlatformKind, parse_platform_kind


def register(app: typer.Typer) -> None:
    install_app = typer.Typer(help="Install agent platform surfaces")
    app.add_typer(install_app, name="install")

    @install_app.callback(invoke_without_command=True)
    def install(
        ctx: typer.Context,
        platform: str = required_str("--platform"),
        target_home: str = required_str("--target-home", help="Agent config home"),
        project_root: str = required_str("--project-root", help="Project to install into"),
    ) -> None:
        if ctx.invoked_subcommand is not None:
            return
        installer = _installer_for(platform)
        emit_result(installer.install(target_home=target_home, project_root=project_root))

    @app.command("diagnostics")
    def diagnostics(
        platform: str = required_str("--platform"),
        target_home: str = required_str("--target-home"),
        project_root: str = required_str("--project-root"),
    ) -> None:
        installer = _installer_for(platform)
        emit_result(installer.diagnostics(target_home=target_home, project_root=project_root))

    @app.command("uninstall")
    def uninstall(
        platform: str = required_str("--platform"),
        target_home: str = required_str("--target-home"),
        project_root: str = required_str("--project-root"),
    ) -> None:
        installer = _installer_for(platform)
        emit_result(installer.uninstall(target_home=target_home, project_root=project_root))


def _installer_for(platform: str) -> PlatformInstaller:
    try:
        kind = parse_platform_kind(platform)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if kind == PlatformKind.CLAUDE_CODE:
        return get_claude_installer()
    if kind == PlatformKind.CURSOR:
        return get_cursor_installer()
    raise typer.BadParameter(f"Unsupported platform: {platform}")
