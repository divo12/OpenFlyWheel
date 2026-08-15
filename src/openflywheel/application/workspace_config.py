"""Workspace config persisted beside the SQLite file."""

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.ids import WorkspaceId


class WorkspaceConfigFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    name: str
    home: str
    database_path: str
