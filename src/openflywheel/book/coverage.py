"""Coverage seeding and reporting."""

from __future__ import annotations

import sqlite3

from openflywheel.book.ontology import templates_for_shape
from openflywheel.contracts.book import (
    BoundaryCoverageReport,
    OrgCoverageReport,
    SectionCoverage,
)
from openflywheel.contracts.boundary import SystemBoundaryRecord
from openflywheel.contracts.enums import TruthSection
from openflywheel.contracts.ids import BoundaryId, WorkspaceId
from openflywheel.contracts.retrieval import RetrievalGap
from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
from openflywheel.store.repos.coverage_repo import SqliteCoverageRepository


class CoverageService:
    def __init__(self) -> None:
        self._boundaries = SqliteBoundaryRepository()
        self._coverage = SqliteCoverageRepository()

    def seed_for_boundary(self, conn: sqlite3.Connection, boundary: SystemBoundaryRecord) -> None:
        if boundary.manifest is None:
            return
        shape = boundary.manifest.system_shape
        for template in templates_for_shape(shape):
            self._coverage.insert_if_missing(
                conn,
                workspace_id=boundary.workspace_id,
                boundary_id=boundary.id,
                section=template.section,
                slot_key=template.slot_key,
                description=template.description,
                required_for_shape=shape,
            )

    def seed_workspace(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> None:
        for boundary in self._boundaries.list_boundaries(conn, workspace_id):
            if boundary.manifest is not None:
                self.seed_for_boundary(conn, boundary)

    def boundary_report(
        self, conn: sqlite3.Connection, boundary_id: BoundaryId
    ) -> BoundaryCoverageReport:
        requirements = self._coverage.list_for_boundary(conn, boundary_id)
        by_section: dict[TruthSection, tuple[int, int]] = {}
        for section in TruthSection:
            by_section[section] = (0, 0)
        for req in requirements:
            verified, total = by_section[req.section]
            total += 1
            if req.verified:
                verified += 1
            by_section[req.section] = (verified, total)
        sections = tuple(
            SectionCoverage(
                section=section,
                verified_slots=by_section[section][0],
                required_slots=by_section[section][1],
            )
            for section in TruthSection
            if by_section[section][1] > 0
        )
        ratios = [s.ratio for s in sections if s.required_slots > 0]
        overall = sum(ratios) / len(ratios) if ratios else 0.0
        return BoundaryCoverageReport(
            boundary_id=boundary_id,
            sections=sections,
            overall_ratio=overall,
        )

    def org_report(self, conn: sqlite3.Connection, workspace_id: WorkspaceId) -> OrgCoverageReport:
        boundaries = self._boundaries.list_boundaries(conn, workspace_id)
        reports: list[BoundaryCoverageReport] = []
        for boundary in boundaries:
            if boundary.manifest is None:
                continue
            reports.append(self.boundary_report(conn, boundary.id))
        overall = sum(r.overall_ratio for r in reports) / len(reports) if reports else 0.0
        return OrgCoverageReport(boundaries=tuple(reports), overall_ratio=overall)

    def gaps_for_workspace(
        self, conn: sqlite3.Connection, workspace_id: WorkspaceId
    ) -> tuple[RetrievalGap, ...]:
        unverified = self._coverage.list_unverified(conn, workspace_id)
        return tuple(
            RetrievalGap(
                slot_key=req.slot_key,
                description=req.description,
                section=req.section,
            )
            for req in unverified
        )
