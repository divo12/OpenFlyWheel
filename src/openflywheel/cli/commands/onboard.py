"""CLI"""

from __future__ import annotations

from typing import cast

import typer

from openflywheel.application.workspace_service import WorkspaceHandle, WorkspaceService
from openflywheel.cli.emit import emit_result
from openflywheel.cli.parse import parse_authority, parse_boundary_exclusion, parse_system_shape
from openflywheel.cli.paths import resolve_home
from openflywheel.cli.typer_options import default_list, required_list, required_str
from openflywheel.contracts.boundary import SourceAuthorityRule
from openflywheel.contracts.onboarding import LockBoundaryRequest
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.onboarding.service import OnboardingService


def _require_workspace(home: str) -> WorkspaceHandle:
    opened = WorkspaceService().open_workspace(home)
    if opened.error is not None:
        emit_result(opened)
    if opened.data is None:
        emit_result(
            OperationResult.failure(
                code="WORKSPACE_OPEN_EMPTY",
                message="Workspace open returned no handle",
                root_cause_hint="Internal workspace open inconsistency",
                safe_retry=True,
                stop_condition="Retry after workspace init",
            )
        )
    return cast(WorkspaceHandle, opened.data)


def register(app: typer.Typer) -> None:
    @app.callback(invoke_without_command=True)
    def onboard_root(
        ctx: typer.Context,
        home: str | None = typer.Option(None, help="Workspace home"),
    ) -> None:
        if ctx.invoked_subcommand is not None:
            return
        if home is None:
            raise typer.BadParameter("Missing option '--home'")
        handle = _require_workspace(home)
        service = OnboardingService(handle.database)
        emit_result(service.start_or_resume(handle.config.workspace_id))

    @app.command("connect")
    def onboard_connect(home: str = required_str(help="Workspace home")) -> None:
        handle = _require_workspace(home)
        service = OnboardingService(handle.database)
        emit_result(service.run_connect(handle.config.workspace_id))

    @app.command("locate")
    def onboard_locate(
        home: str = required_str(help="Workspace home"),
        fixture_root: str = required_str(help="Path to fixture company root"),
    ) -> None:
        handle = _require_workspace(home)
        service = OnboardingService(handle.database)
        emit_result(service.run_locate(handle.config.workspace_id, resolve_home(fixture_root)))

    @app.command("lock")
    def onboard_lock(
        home: str = required_str(help="Workspace home"),
        boundary: list[str] = required_list(help="Boundary slug to lock"),
        purpose: list[str] = required_list(help="Purpose per boundary"),
        owner: list[str] = required_list(help="Owner display name per boundary"),
        kpi: list[str] = required_list(help="Primary KPI per boundary"),
        shape: list[str] = required_list(help="System shape per boundary"),
        authority: list[str] = default_list(
            ["github:1"], help="Source authority per boundary as slug:rank"
        ),
        boundary_exclusion: list[str] = default_list(
            [],
            help="Per-boundary exclusion prefix as slug:prefix (repeatable)",
        ),
        exclude: list[str] = default_list(
            [],
            help="Additional exclusion prefix applied to all boundaries in this lock",
        ),
    ) -> None:
        if not (len(boundary) == len(purpose) == len(owner) == len(kpi) == len(shape)):
            raise typer.BadParameter("boundary, purpose, owner, kpi, and shape counts must match")
        if len(authority) not in {1, len(boundary)}:
            raise typer.BadParameter("authority must be one value or match boundary count")

        exclusions_by_index: list[list[str]] = [list(exclude) for _ in boundary]

        def boundary_index(slug: str) -> int:
            for index, name in enumerate(boundary):
                if name == slug:
                    return index
            raise typer.BadParameter(f"boundary-exclusion slug not in lock set: {slug}")

        for item in boundary_exclusion:
            slug, prefix = parse_boundary_exclusion(item)
            exclusions_by_index[boundary_index(slug)].append(prefix)

        authorities: list[tuple[SourceAuthorityRule, ...]] = []
        if len(authority) == 1:
            rule = parse_authority(authority[0])
            authorities = [(rule,)] * len(boundary)
        else:
            authorities = [(parse_authority(item),) for item in authority]

        requests = tuple(
            LockBoundaryRequest(
                candidate_slug=slug,
                purpose=pur,
                system_shape=parse_system_shape(shape_val),
                source_authorities=authorities[index],
                owner_display_names=(own,),
                primary_kpi=kpi_val,
                exclusions=tuple(exclusions_by_index[index]),
            )
            for index, (slug, pur, own, kpi_val, shape_val) in enumerate(
                zip(boundary, purpose, owner, kpi, shape, strict=True)
            )
        )
        handle = _require_workspace(home)
        service = OnboardingService(handle.database)
        emit_result(service.run_lock(handle.config.workspace_id, requests))
