"""CLI"""

from __future__ import annotations

import os

import typer

from openflywheel.application.agent_worker import BackgroundWorkerService
from openflywheel.application.book_app import BookApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.emit import emit_result
from openflywheel.cli.paths import resolve_home
from openflywheel.cli.typer_options import optional_str_value, required_str, toggle_bool
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.agent_session import EpisodeRecordRequest, SessionEnvelope
from openflywheel.contracts.book import BookContextRequest
from openflywheel.contracts.enums import PlatformKind, VisibilityLevel, parse_platform_kind
from openflywheel.contracts.ids import IdentityId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.store.db import Database
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository


def register(app: typer.Typer) -> None:
    agent_app = typer.Typer(help="Agent lifecycle hooks")
    app.add_typer(agent_app, name="agent")

    @agent_app.command("hook")
    def agent_hook(
        platform: str = required_str("--platform"),
        event: str = required_str("--event"),
        home: str = required_str("--home"),
        project_root: str = required_str("--project-root"),
        identity: str | None = optional_str_value(None, "--identity"),
        session_ref: str | None = optional_str_value(None, "--session-ref"),
        transcript: str | None = optional_str_value(None, "--transcript"),
        query: str | None = optional_str_value(None, "--query"),
        schedule_worker: bool = toggle_bool(True, "--schedule-worker/--no-schedule-worker"),
    ) -> None:
        if os.environ.get("OFW_BACKGROUND") == "1":
            emit_result(_warning("Hook recursion disabled in background worker"))
            return

        ws = WorkspaceService()
        home_path = resolve_home(home)
        database = ws.load_database(home_path)
        config = ws.read_config(home_path)
        book = BookApplication(database)

        if query:
            identity_id = (
                IdentityId(identity)
                if identity
                else _default_identity(database, config.workspace_id)
            )
            ctx = book.book_context(
                BookContextRequest(
                    workspace_id=config.workspace_id,
                    identity_id=identity_id,
                    query=query,
                )
            )
            emit_result(ctx)
            return

        _ = schedule_worker
        try:
            platform_kind = parse_platform_kind(platform)
        except ValueError as exc:
            emit_result(
                _failure(
                    code="HOOK_PLATFORM_INVALID",
                    message=str(exc),
                )
            )
            return

        if event in {"Stop", "sessionEnd"} and session_ref and transcript:
            with database.read() as conn:
                source = SqliteSourceRepository().get_by_slug(
                    conn, config.workspace_id, platform_kind.value
                )
            if source is None:
                emit_result(
                    _failure(
                        code="HOOK_NO_SOURCE",
                        message="Agent source not connected",
                    )
                )
                return
            identity_id = (
                IdentityId(identity)
                if identity
                else _default_identity(database, config.workspace_id)
            )
            agent_home = _agent_home_for(platform_kind)
            envelope = SessionEnvelope(
                workspace_id=config.workspace_id,
                source_id=source.id,
                platform=platform_kind,
                session_ref=session_ref,
                transcript_path=transcript,
                agent_home=agent_home,
                project_root=project_root,
                identity_id=identity_id,
                acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
            )
            result = book.episode_record(EpisodeRecordRequest(envelope=envelope))
            if schedule_worker and result.error is None:
                worker = BackgroundWorkerService(database)
                worker.process_next()
            emit_result(result)
            return

        emit_result(_success(f"Recorded hook event {event} for {platform_kind.value}"))


def _agent_home_for(platform: PlatformKind) -> str:
    from pathlib import Path

    if platform == PlatformKind.CURSOR:
        return os.environ.get("CURSOR_HOME", str(Path.home() / ".cursor"))
    return os.environ.get("CLAUDE_CONFIG_HOME", str(Path.home() / ".claude"))


def _default_identity(database: Database, workspace_id: WorkspaceId) -> IdentityId:
    with database.read() as conn:
        owner = SqliteWorkspaceRepository().find_identity_by_display_name(
            conn, workspace_id, "Owner Alpha"
        )
    if owner is None:
        raise typer.Exit(code=1)
    return owner.id


def _success(summary: str) -> OperationResult[None]:
    return OperationResult.success(summary=summary)


def _warning(summary: str) -> OperationResult[None]:
    return OperationResult.warning(summary=summary)


def _failure(*, code: str, message: str) -> OperationResult[None]:
    return OperationResult.failure(
        code=code,
        message=message,
        root_cause_hint="Complete onboarding connect stage",
        safe_retry=True,
        stop_condition="Configure agent source",
    )
