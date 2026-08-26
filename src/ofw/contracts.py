"""Immutable, language-neutral repository revision contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from ofw.observability.langfuse.contracts import LangfuseConnectionManifest


class HarnessSchemaVersion(IntEnum):
    V1 = 1


class HarnessErrorCode(StrEnum):
    INVALID_NAME = "invalid_name"
    INVALID_SOURCE = "invalid_source"
    ROOT_NOT_FOUND = "root_not_found"
    ROOT_NOT_DIRECTORY = "root_not_directory"
    GIT_REPOSITORY_REQUIRED = "git_repository_required"
    GIT_COMMAND_FAILED = "git_command_failed"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"


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
class RepositorySnapshot:
    commit: GitCommit
    is_dirty: bool
    dirty_digest: Sha256Digest | None


@dataclass(frozen=True, slots=True)
class HarnessRevisionContent:
    schema_version: HarnessSchemaVersion
    harness_name: str
    repository: RepositorySnapshot
    observability: LangfuseConnectionManifest | None

    def canonical_json(self) -> str:
        return _render_content(self)


@dataclass(frozen=True, slots=True)
class HarnessRevision:
    schema_version: HarnessSchemaVersion
    id: HarnessRevisionId
    harness_name: str
    root: Path
    repository: RepositorySnapshot
    observability: LangfuseConnectionManifest | None

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "revisions" / str(self.id) / "manifest.json"

    def to_json(self) -> str:
        return (
            "{"
            f'"schema_version":{int(self.schema_version)},'
            f'"id":{_quote(str(self.id))},'
            f'"harness_name":{_quote(self.harness_name)},'
            f'"root":{_quote(self.root.as_posix())},'
            f'"repository":{_render_repository(self.repository)},'
            f'"observability":{_render_observability(self.observability)}'
            "}"
        )


def _render_content(content: HarnessRevisionContent) -> str:
    return (
        "{"
        f'"schema_version":{int(content.schema_version)},'
        f'"harness_name":{_quote(content.harness_name)},'
        f'"repository":{_render_repository(content.repository)},'
        f'"observability":{_render_observability(content.observability)}'
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


def _render_observability(connection: LangfuseConnectionManifest | None) -> str:
    return "null" if connection is None else connection.to_json()


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
