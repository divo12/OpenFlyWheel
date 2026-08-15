"""Workspace contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import DeploymentMode, VisibilityLevel
from openflywheel.contracts.ids import IdentityId, WorkspaceId


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    default_visibility: VisibilityLevel
    retention_days: int = 365


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: WorkspaceId
    name: str
    deployment_mode: DeploymentMode
    policy: WorkspacePolicy
    admin_identity_ids: tuple[IdentityId, ...]
    created_at: datetime


class WorkspaceInitRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    home: str
    deployment_mode: DeploymentMode = DeploymentMode.LOCAL
    force: bool = False


class WorkspaceInitResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    home: str
    database_path: str
