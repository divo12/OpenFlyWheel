"""Read-only dashboard API over application services."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException

from openflywheel.application.book_app import BookApplication
from openflywheel.application.identity_gate import IdentityGate
from openflywheel.book.coverage import CoverageService
from openflywheel.contracts.dashboard import (
    BoundaryDashboardView,
    DashboardDetail,
    DashboardOverview,
)
from openflywheel.contracts.enums import EdgeKind
from openflywheel.contracts.ids import (
    BoundaryId,
    ClaimId,
    IdentityId,
    ManifestVersion,
    PinId,
    WorkspaceId,
)
from openflywheel.contracts.pin import PinRecord
from openflywheel.contracts.retrieval import RetrievalGap
from openflywheel.retrieval.acl import claim_visible_to_identity, filter_claims_by_acl
from openflywheel.store.db import Database
from openflywheel.store.repos.claim_repo import SqliteClaimRepository
from openflywheel.store.repos.edge_repo import SqliteEdgeRepository
from openflywheel.store.row_from_sqlite import pin_row
from openflywheel.store.serde import tuple_from_json
from openflywheel.store.sqlite_access import fetch_all_rows


def create_dashboard_app(database: Database, *, workspace_id: WorkspaceId) -> FastAPI:
    app = FastAPI(
        title="OpenFlyWheel Dashboard",
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
    )
    book = BookApplication(database)
    coverage = CoverageService()
    claims_repo = SqliteClaimRepository()
    edges_repo = SqliteEdgeRepository()
    identity_gate = IdentityGate(database)

    def _resolve_identity(identity_header: str | None) -> IdentityId:
        if not identity_header:
            raise HTTPException(status_code=401, detail="X-OFW-Identity header required")
        resolved = identity_gate.resolve(
            workspace_id=workspace_id,
            identity_id=IdentityId(identity_header),
        )
        if resolved.error is not None or resolved.data is None:
            raise HTTPException(status_code=401, detail="Unknown workspace identity")
        return resolved.data.id

    def _list_pins(conn: sqlite3.Connection) -> tuple[PinRecord, ...]:
        rows = fetch_all_rows(
            conn,
            "SELECT * FROM pins WHERE workspace_id = ? ORDER BY created_at DESC",
            (str(workspace_id),),
        )
        result: list[PinRecord] = []
        for raw in rows:
            row = pin_row(raw)
            result.append(
                PinRecord(
                    id=PinId(row.id),
                    workspace_id=WorkspaceId(row.workspace_id),
                    boundary_id=BoundaryId(row.boundary_id),
                    manifest_version=ManifestVersion(row.manifest_version),
                    claim_ids=tuple(ClaimId(c) for c in tuple_from_json(row.claim_ids_json)),
                    created_at=datetime.fromisoformat(row.created_at),
                )
            )
        return tuple(result)

    def _filter_pins(
        conn: sqlite3.Connection,
        pins: tuple[PinRecord, ...],
        identity_id: IdentityId,
    ) -> tuple[PinRecord, ...]:
        filtered: list[PinRecord] = []
        for pin in pins:
            visible_claim_ids: list[ClaimId] = []
            for claim_id in pin.claim_ids:
                claim = claims_repo.get_claim(conn, claim_id)
                if claim is not None and claim_visible_to_identity(claim, identity_id):
                    visible_claim_ids.append(claim_id)
            if visible_claim_ids:
                filtered.append(
                    PinRecord(
                        id=pin.id,
                        workspace_id=pin.workspace_id,
                        boundary_id=pin.boundary_id,
                        manifest_version=pin.manifest_version,
                        claim_ids=tuple(visible_claim_ids),
                        created_at=pin.created_at,
                    )
                )
        return tuple(filtered)

    def overview_handler(x_ofw_identity: str | None = Header(default=None)) -> DashboardOverview:
        identity_id = _resolve_identity(x_ofw_identity)
        gaps_result = book.coverage_gaps(workspace_id=workspace_id)
        if gaps_result.error is not None or gaps_result.data is None:
            raise HTTPException(status_code=500, detail=gaps_result.summary)
        with database.read() as conn:
            all_claims = claims_repo.list_active_for_workspace(conn, workspace_id)
            visible_claims = filter_claims_by_acl(all_claims, identity_id)
            visible_ids = frozenset(c.id for c in visible_claims)
            edges = edges_repo.list_direct_neighbors(conn, visible_ids)
            tensions = tuple(e for e in edges if e.kind == EdgeKind.IN_TENSION_WITH)
            pins = _filter_pins(conn, _list_pins(conn), identity_id)
        return DashboardOverview(
            workspace_id=workspace_id,
            identity_id=identity_id,
            org_coverage=gaps_result.data.report,
            gaps=gaps_result.data.gaps,
            claim_count=len(visible_claims),
            tension_count=len(tensions),
            pin_count=len(pins),
        )

    def detail_handler(x_ofw_identity: str | None = Header(default=None)) -> DashboardDetail:
        identity_id = _resolve_identity(x_ofw_identity)
        overview_data = overview_handler(x_ofw_identity=str(identity_id))
        gaps_result = book.coverage_gaps(workspace_id=workspace_id)
        if gaps_result.error is not None or gaps_result.data is None:
            raise HTTPException(status_code=500, detail=gaps_result.summary)
        boundaries: list[BoundaryDashboardView] = []
        with database.read() as conn:
            coverage.seed_workspace(conn, workspace_id)
            report = coverage.org_report(conn, workspace_id)
            boundary_gaps: tuple[RetrievalGap, ...] = gaps_result.data.gaps
            for boundary_report in report.boundaries:
                boundary_claims = claims_repo.list_active_for_boundary(
                    conn, boundary_id=boundary_report.boundary_id
                )
                visible = filter_claims_by_acl(boundary_claims, identity_id)
                boundaries.append(
                    BoundaryDashboardView(
                        boundary_id=boundary_report.boundary_id,
                        coverage=boundary_report,
                        claims=visible,
                        gaps=boundary_gaps,
                    )
                )
            visible_ids = frozenset(
                c.id
                for boundary in boundaries
                for c in boundary.claims
                if claim_visible_to_identity(c, identity_id)
            )
            edges = edges_repo.list_direct_neighbors(conn, visible_ids)
            tensions = tuple(e for e in edges if e.kind == EdgeKind.IN_TENSION_WITH)
            pins = _filter_pins(conn, _list_pins(conn), identity_id)
        return DashboardDetail(
            overview=overview_data,
            boundaries=tuple(boundaries),
            tensions=tensions,
            pins=pins,
            unmet_requirements=gaps_result.data.unmet_requirements,
        )

    def health_handler() -> dict[str, str]:
        return {"status": "ok"}

    app.add_api_route("/api/overview", overview_handler, methods=["GET"])
    app.add_api_route("/api/detail", detail_handler, methods=["GET"])
    app.add_api_route("/health", health_handler, methods=["GET"])

    return app
