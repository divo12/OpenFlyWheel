"""Immutable contracts for a compiled harness revision."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.ids import GitCommit, HarnessRevisionId, Sha256Digest


class HarnessSchemaVersion(IntEnum):
    V1 = 1


class AssetKind(StrEnum):
    CONTEXT = "context"
    EXECUTION = "execution"
    TOOLING = "tooling"
    OBSERVABILITY = "observability"
    VERIFIER = "verifier"
    LIFECYCLE = "lifecycle"
    GOVERNANCE = "governance"


class AssetAccess(StrEnum):
    FROZEN = "frozen"
    FIT_EDITABLE = "fit_editable"
    MINE_MANAGED = "mine_managed"


class AssetSourceKind(StrEnum):
    FILE = "file"
    PYTHON_CLASS = "python_class"


class HarnessErrorCode(StrEnum):
    INVALID_NAME = "invalid_name"
    INVALID_SOURCE = "invalid_source"
    ROOT_NOT_FOUND = "root_not_found"
    ROOT_NOT_DIRECTORY = "root_not_directory"
    CONTEXT_REQUIRED = "context_required"
    LIFECYCLE_REQUIRED = "lifecycle_required"
    LIFECYCLE_ALREADY_CONNECTED = "lifecycle_already_connected"
    UNINSPECTABLE_LIFECYCLE = "uninspectable_lifecycle"
    MISSING_ASSET = "missing_asset"
    PATH_OUTSIDE_ROOT = "path_outside_root"
    NOT_A_FILE = "not_a_file"
    DUPLICATE_ASSET = "duplicate_asset"
    CONFLICTING_ACCESS = "conflicting_access"
    GIT_REPOSITORY_REQUIRED = "git_repository_required"
    GIT_COMMAND_FAILED = "git_command_failed"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
    ACCESS_NOT_ALLOWED = "access_not_allowed"
    SENSITIVE_ASSET = "sensitive_asset"


class HarnessValidationError(Exception):
    """Typed, actionable failure while declaring or compiling a harness."""

    __slots__ = ("code", "subject")

    def __init__(self, code: HarnessErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class FileAssetSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AssetSourceKind = AssetSourceKind.FILE
    relative_path: Path


class PythonClassAssetSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AssetSourceKind = AssetSourceKind.PYTHON_CLASS
    module: str
    qualified_name: str
    relative_path: Path


class HarnessAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AssetKind
    access: AssetAccess
    source: FileAssetSource | PythonClassAssetSource
    digest: Sha256Digest


class RepositorySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: GitCommit
    is_dirty: bool
    dirty_digest: Sha256Digest | None


class HarnessRevisionContent(BaseModel):
    """Portable content hashed to create a revision identifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: HarnessSchemaVersion
    harness_name: str
    repository: RepositorySnapshot
    assets: tuple[HarnessAsset, ...]


class HarnessRevision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: HarnessSchemaVersion
    id: HarnessRevisionId
    harness_name: str
    root: Path
    repository: RepositorySnapshot
    assets: tuple[HarnessAsset, ...]

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "revisions" / str(self.id) / "manifest.json"

    @property
    def editable_files(self) -> tuple[Path, ...]:
        return tuple(
            asset.source.relative_path
            for asset in self.assets
            if asset.access is AssetAccess.FIT_EDITABLE
            and isinstance(asset.source, FileAssetSource)
        )

    @property
    def frozen_files(self) -> tuple[Path, ...]:
        return tuple(
            asset.source.relative_path
            for asset in self.assets
            if asset.access is AssetAccess.FROZEN and isinstance(asset.source, FileAssetSource)
        )

    @property
    def mine_managed_files(self) -> tuple[Path, ...]:
        return tuple(
            asset.source.relative_path
            for asset in self.assets
            if asset.access is AssetAccess.MINE_MANAGED
            and isinstance(asset.source, FileAssetSource)
        )
