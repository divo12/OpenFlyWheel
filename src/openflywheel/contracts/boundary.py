"""System boundary and manifest contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import SystemShape, TruthSection
from openflywheel.contracts.ids import BoundaryId, IdentityId, ManifestVersion, WorkspaceId


class SourceAuthorityRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_slug: str
    authority_rank: int


class BoundaryManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: ManifestVersion
    purpose: str
    system_shape: SystemShape
    owner_identity_ids: tuple[IdentityId, ...]
    primary_kpi: str
    source_authorities: tuple[SourceAuthorityRule, ...]
    exclusions: tuple[str, ...]
    locked_at: datetime


class SystemBoundaryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: BoundaryId
    workspace_id: WorkspaceId
    name: str
    slug: str
    component_paths: tuple[str, ...]
    manifest: BoundaryManifest | None = None
    created_at: datetime


class BoundaryCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    slug: str
    system_shape: SystemShape
    component_paths: tuple[str, ...]
    rationale: str
    suggested_kpi_section: TruthSection
