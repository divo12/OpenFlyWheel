"""Immutable, language-neutral harness component contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path


class HarnessSchemaVersion(IntEnum):
    V1 = 1


class ComponentKind(StrEnum):
    PROMPT = "prompt"
    TOOL = "tool"
    SKILL = "skill"
    SUBAGENT = "subagent"
    MIDDLEWARE = "middleware"


class AssetAccess(StrEnum):
    FROZEN = "frozen"
    FIT_EDITABLE = "fit_editable"


class HarnessErrorCode(StrEnum):
    INVALID_NAME = "invalid_name"
    INVALID_SOURCE = "invalid_source"
    ROOT_NOT_FOUND = "root_not_found"
    ROOT_NOT_DIRECTORY = "root_not_directory"
    PROMPT_REQUIRED = "prompt_required"
    MISSING_ASSET = "missing_asset"
    PATH_OUTSIDE_ROOT = "path_outside_root"
    NOT_A_FILE = "not_a_file"
    DUPLICATE_ASSET = "duplicate_asset"
    CONFLICTING_ACCESS = "conflicting_access"
    COMPONENT_OVERLAP = "component_overlap"
    INVALID_TOOL_NAME = "invalid_tool_name"
    DUPLICATE_TOOL = "duplicate_tool"
    GIT_REPOSITORY_REQUIRED = "git_repository_required"
    GIT_COMMAND_FAILED = "git_command_failed"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
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
class WorkspaceFile:
    relative_path: Path


@dataclass(frozen=True, slots=True)
class HarnessAsset:
    name: str | None
    access: AssetAccess
    source: WorkspaceFile
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class HarnessComponent:
    kind: ComponentKind
    assets: tuple[HarnessAsset, ...]
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
    components: tuple[HarnessComponent, ...]

    def canonical_json(self) -> str:
        return _render_content(self)


@dataclass(frozen=True, slots=True)
class HarnessRevision:
    schema_version: HarnessSchemaVersion
    id: HarnessRevisionId
    harness_name: str
    root: Path
    repository: RepositorySnapshot
    components: tuple[HarnessComponent, ...]

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "revisions" / str(self.id) / "manifest.json"

    @property
    def assets(self) -> tuple[HarnessAsset, ...]:
        return tuple(asset for component in self.components for asset in component.assets)

    @property
    def editable_files(self) -> tuple[Path, ...]:
        return tuple(
            asset.source.relative_path
            for asset in self.assets
            if asset.access is AssetAccess.FIT_EDITABLE
        )

    @property
    def frozen_files(self) -> tuple[Path, ...]:
        return tuple(
            asset.source.relative_path
            for asset in self.assets
            if asset.access is AssetAccess.FROZEN
        )

    def component(self, kind: ComponentKind) -> HarnessComponent | None:
        for component in self.components:
            if component.kind is kind:
                return component
        return None

    def to_json(self) -> str:
        components = ",".join(_render_component(component) for component in self.components)
        return (
            "{"
            f'"schema_version":{int(self.schema_version)},'
            f'"id":{_quote(str(self.id))},'
            f'"harness_name":{_quote(self.harness_name)},'
            f'"root":{_quote(self.root.as_posix())},'
            f'"repository":{_render_repository(self.repository)},'
            f'"components":[{components}]'
            "}"
        )


def _render_content(content: HarnessRevisionContent) -> str:
    components = ",".join(_render_component(component) for component in content.components)
    return (
        "{"
        f'"schema_version":{int(content.schema_version)},'
        f'"harness_name":{_quote(content.harness_name)},'
        f'"repository":{_render_repository(content.repository)},'
        f'"components":[{components}]'
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


def _render_component(component: HarnessComponent) -> str:
    assets = ",".join(_render_asset(asset) for asset in component.assets)
    return (
        "{"
        f'"kind":{_quote(component.kind.value)},'
        f'"digest":{_quote(str(component.digest))},'
        f'"assets":[{assets}]'
        "}"
    )


def _render_asset(asset: HarnessAsset) -> str:
    name = "null" if asset.name is None else _quote(asset.name)
    return (
        "{"
        f'"name":{name},'
        f'"access":{_quote(asset.access.value)},'
        f'"source":{{"relative_path":{_quote(asset.source.relative_path.as_posix())}}},'
        f'"digest":{_quote(str(asset.digest))}'
        "}"
    )


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
