"""Book CLI commands."""

from __future__ import annotations

import typer

from openflywheel.application.book_app import BookApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.emit import emit_result
from openflywheel.cli.paths import resolve_home
from openflywheel.cli.typer_options import (
    default_int,
    default_list,
    default_str,
    flag_bool,
    required_str,
)
from openflywheel.contracts.book import BookContextRequest, ProposeManualRequest, VerifyRequest
from openflywheel.contracts.enums import TruthSection, VerificationDecision
from openflywheel.contracts.ids import BoundaryId, ClaimId, IdentityId, PinId, ProposalId


def register(app: typer.Typer, coverage_app: typer.Typer) -> None:
    @app.command("propose")
    def book_propose(
        what: str,
        how: str,
        home: str = required_str(help="Workspace home"),
        boundary: str = required_str(help="Boundary id"),
        identity: str = required_str(help="Proposer identity id"),
        section: TruthSection = TruthSection.U3,
        anchor: list[str] = default_list([], help="Evidence anchor ids"),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        from openflywheel.contracts.ids import EvidenceAnchorId

        emit_result(
            book.claim_propose(
                ProposeManualRequest(
                    workspace_id=config.workspace_id,
                    boundary_id=BoundaryId(boundary),
                    what=what,
                    how=how,
                    section=section,
                    proposer_identity_id=IdentityId(identity),
                    anchor_ids=tuple(EvidenceAnchorId(a) for a in anchor),
                )
            )
        )

    @app.command("episode-record")
    def book_episode_record(
        home: str = typer.Option(..., help="Workspace home"),
        platform: str = typer.Option(..., help="Platform kind"),
        session_ref: str = typer.Option(..., help="Session reference"),
        transcript: str = typer.Option(..., help="Transcript path"),
        identity: str = typer.Option(..., help="Identity id"),
        agent_home: str = typer.Option(..., help="Agent config home"),
        project_root: str = typer.Option(..., help="Project root"),
    ) -> None:
        from openflywheel.contracts.acl import AclLabel
        from openflywheel.contracts.agent_session import EpisodeRecordRequest, SessionEnvelope
        from openflywheel.contracts.enums import VisibilityLevel, parse_platform_kind
        from openflywheel.store.repos.source_repo import SqliteSourceRepository

        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        try:
            platform_kind = parse_platform_kind(platform)
        except ValueError as exc:
            from openflywheel.contracts.operation_result import OperationResult

            emit_result(
                OperationResult.failure(
                    code="EPISODE_PLATFORM_INVALID",
                    message=str(exc),
                    root_cause_hint="Use claude-code or cursor",
                    safe_retry=False,
                    stop_condition="Pass valid --platform",
                )
            )
            return
        with database.read() as conn:
            source = SqliteSourceRepository().get_by_slug(
                conn, config.workspace_id, platform_kind.value
            )
        if source is None:
            from openflywheel.contracts.operation_result import OperationResult

            emit_result(
                OperationResult.failure(
                    code="EPISODE_NO_SOURCE",
                    message="Agent source not found",
                    root_cause_hint="Run onboard connect first",
                    safe_retry=True,
                    stop_condition="Connect agent platform source",
                )
            )
            return
        emit_result(
            book.episode_record(
                EpisodeRecordRequest(
                    envelope=SessionEnvelope(
                        workspace_id=config.workspace_id,
                        source_id=source.id,
                        platform=platform_kind,
                        session_ref=session_ref,
                        transcript_path=transcript,
                        agent_home=agent_home,
                        project_root=project_root,
                        identity_id=IdentityId(identity),
                        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
                    )
                )
            )
        )

    @app.command("view")
    def book_view(
        home: str = required_str(help="Workspace home"),
        host: str = default_str("127.0.0.1", help="Bind host"),
        port: int = default_int(8765, help="Bind port"),
        print_only: bool = flag_bool(False, "--print-only", help="Print URL only"),
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            from openflywheel.cli.emit import emit_result
            from openflywheel.contracts.operation_result import OperationResult

            emit_result(
                OperationResult.failure(
                    code="DASHBOARD_BIND_FORBIDDEN",
                    message="Dashboard may bind to loopback only",
                    root_cause_hint="Use 127.0.0.1 or localhost",
                    safe_retry=True,
                    stop_condition="Choose a loopback host",
                )
            )
            raise typer.Exit(code=1)
        url = f"http://{host}:{port}/"
        if print_only:
            typer.echo(url)
            return
        import uvicorn

        from openflywheel.application.workspace_service import WorkspaceService
        from openflywheel.dashboard.api import create_dashboard_app

        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        dashboard = create_dashboard_app(database, workspace_id=config.workspace_id)
        uvicorn.run(dashboard, host=host, port=port, log_level="warning")

    @app.command("context")
    def book_context(
        query: str,
        home: str = typer.Option(..., help="Workspace home"),
        identity: str = typer.Option(..., help="Identity id"),
        boundary: str | None = typer.Option(None, help="Boundary id"),
        pin: str | None = typer.Option(None, help="Pin id"),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(
            book.book_context(
                BookContextRequest(
                    workspace_id=config.workspace_id,
                    identity_id=IdentityId(identity),
                    query=query,
                    boundary_id=BoundaryId(boundary) if boundary else None,
                    pin_id=PinId(pin) if pin else None,
                )
            )
        )

    @app.command("get")
    def book_get(
        claim_id: str,
        home: str = typer.Option(..., help="Workspace home"),
        identity: str = typer.Option(..., help="Identity id"),
        pin: str | None = typer.Option(None, help="Pin id"),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(
            book.book_get(
                workspace_id=config.workspace_id,
                identity_id=IdentityId(identity),
                claim_id=ClaimId(claim_id),
                pin_id=PinId(pin) if pin else None,
            )
        )

    @app.command("verify")
    def book_verify(
        proposal_id: str,
        home: str = typer.Option(..., help="Workspace home"),
        identity: str = typer.Option(..., help="Verifier identity id"),
        decision: VerificationDecision = VerificationDecision.PROMOTE,
        tension_with: str | None = typer.Option(None),
        supersedes: str | None = typer.Option(None),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(
            book.book_verify(
                workspace_id=config.workspace_id,
                request=VerifyRequest(
                    proposal_id=ProposalId(proposal_id),
                    decision=decision,
                    verifier_identity_id=IdentityId(identity),
                    tension_with_claim_id=ClaimId(tension_with) if tension_with else None,
                    supersedes_claim_id=ClaimId(supersedes) if supersedes else None,
                ),
            )
        )

    @app.command("pin")
    def book_pin(
        home: str = typer.Option(..., help="Workspace home"),
        boundary: str = typer.Option(..., help="Boundary id"),
    ) -> None:
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(
            book.book_pin(
                workspace_id=config.workspace_id,
                boundary_id=BoundaryId(boundary),
            )
        )

    @coverage_app.callback(invoke_without_command=True)
    def coverage_report(
        ctx: typer.Context,
        home: str = typer.Option(..., help="Workspace home"),
    ) -> None:
        if ctx.invoked_subcommand is not None:
            return
        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)
        emit_result(book.coverage_gaps(workspace_id=config.workspace_id))
