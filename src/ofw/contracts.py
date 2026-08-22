"""Immutable harness revision contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path


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
    """Typed failure while declaring or compiling a harness."""

    __slots__ = ("code", "subject")

    def __init__(self, code: HarnessErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class HarnessRevisionId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class GitCommit:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class FileAssetSource:
    relative_path: Path
    kind: AssetSourceKind = field(default=AssetSourceKind.FILE, init=False)


@dataclass(frozen=True, slots=True)
class PythonClassAssetSource:
    module: str
    qualified_name: str
    relative_path: Path
    kind: AssetSourceKind = field(default=AssetSourceKind.PYTHON_CLASS, init=False)


AssetSource = FileAssetSource | PythonClassAssetSource


@dataclass(frozen=True, slots=True)
class HarnessAsset:
    kind: AssetKind
    access: AssetAccess
    source: AssetSource
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    commit: GitCommit
    is_dirty: bool
    dirty_digest: Sha256Digest | None


@dataclass(frozen=True, slots=True)
class HarnessRevisionContent:
    schema_version: HarnessSchemaVersion
    harness_name: str
    repository: RepositorySnapshot
    assets: tuple[HarnessAsset, ...]

    def canonical_json(self) -> str:
        return _render_content(self)


@dataclass(frozen=True, slots=True)
class HarnessRevision:
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

    def to_json(self) -> str:
        assets = ",".join(_render_asset(asset) for asset in self.assets)
        return (
            "{"
            f'"schema_version":{int(self.schema_version)},'
            f'"id":{_quote(str(self.id))},'
            f'"harness_name":{_quote(self.harness_name)},'
            f'"root":{_quote(self.root.as_posix())},'
            f'"repository":{_render_repository(self.repository)},'
            f'"assets":[{assets}]'
            "}"
        )


def _render_content(content: HarnessRevisionContent) -> str:
    assets = ",".join(_render_asset(asset) for asset in content.assets)
    return (
        "{"
        f'"schema_version":{int(content.schema_version)},'
        f'"harness_name":{_quote(content.harness_name)},'
        f'"repository":{_render_repository(content.repository)},'
        f'"assets":[{assets}]'
        "}"
    )


def _render_repository(repository: RepositorySnapshot) -> str:
    dirty_digest = (
        "null" if repository.dirty_digest is None else _quote(str(repository.dirty_digest))
    )
    is_dirty = "true" if repository.is_dirty else "false"
    return (
        "{"
        f'"commit":{_quote(str(repository.commit))},'
        f'"is_dirty":{is_dirty},'
        f'"dirty_digest":{dirty_digest}'
        "}"
    )


def _render_asset(asset: HarnessAsset) -> str:
    return (
        "{"
        f'"kind":{_quote(asset.kind.value)},'
        f'"access":{_quote(asset.access.value)},'
        f'"source":{_render_source(asset.source)},'
        f'"digest":{_quote(str(asset.digest))}'
        "}"
    )


def _render_source(source: AssetSource) -> str:
    if isinstance(source, FileAssetSource):
        return (
            "{"
            f'"kind":{_quote(source.kind.value)},'
            f'"relative_path":{_quote(source.relative_path.as_posix())}'
            "}"
        )
    return (
        "{"
        f'"kind":{_quote(source.kind.value)},'
        f'"module":{_quote(source.module)},'
        f'"qualified_name":{_quote(source.qualified_name)},'
        f'"relative_path":{_quote(source.relative_path.as_posix())}'
        "}"
    )


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
