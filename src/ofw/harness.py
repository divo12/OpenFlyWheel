"""Compile a file-level harness workspace into an immutable revision."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ofw.contracts import (
    AssetAccess,
    ComponentKind,
    GitCommit,
    HarnessAsset,
    HarnessComponent,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionContent,
    HarnessRevisionId,
    HarnessSchemaVersion,
    HarnessValidationError,
    RepositorySnapshot,
    Sha256Digest,
    WorkspaceFile,
)

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_NAMED_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")


@dataclass(frozen=True, slots=True)
class EditableFile:
    path: Path


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    source: Path | EditableFile

    def __post_init__(self) -> None:
        _validate_named_source(self.name, self.source, HarnessErrorCode.INVALID_TOOL_NAME)


@dataclass(frozen=True, slots=True)
class Subagent:
    name: str
    source: Path | EditableFile

    def __post_init__(self) -> None:
        _validate_named_source(self.name, self.source, HarnessErrorCode.INVALID_SUBAGENT_NAME)


@dataclass(frozen=True, slots=True)
class _FileRegistration:
    component: ComponentKind
    name: str | None
    path: Path
    access: AssetAccess


@dataclass(frozen=True, slots=True)
class _CompiledAsset:
    component: ComponentKind
    asset: HarnessAsset


def editable(path: Path) -> EditableFile:
    """Grant Fit authority to edit one workspace file."""
    if not isinstance(path, Path):
        raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(path))
    return EditableFile(path=path)


@dataclass(slots=True)
class Harness:
    """Mutable component registry; ``process`` returns an immutable revision."""

    name: str
    root: Path
    _files: list[_FileRegistration] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.name) is None:
            raise HarnessValidationError(HarnessErrorCode.INVALID_NAME, self.name)
        if not isinstance(self.root, Path):
            raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(self.root))

    def connect_prompt(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(ComponentKind.PROMPT, sources)
        return self

    def connect_tools(self, *tools: Tool) -> Harness:
        for tool in tools:
            if any(
                registration.component is ComponentKind.TOOL and registration.name == tool.name
                for registration in self._files
            ):
                raise HarnessValidationError(HarnessErrorCode.DUPLICATE_TOOL, tool.name)
            self._files.append(_registration(ComponentKind.TOOL, tool.source, tool.name))
        return self

    def connect_skills(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(ComponentKind.SKILL, sources)
        return self

    def connect_subagents(self, *subagents: Subagent) -> Harness:
        for subagent in subagents:
            if any(
                registration.component is ComponentKind.SUBAGENT
                and registration.name == subagent.name
                for registration in self._files
            ):
                raise HarnessValidationError(
                    HarnessErrorCode.DUPLICATE_SUBAGENT,
                    subagent.name,
                )
            self._files.append(
                _registration(ComponentKind.SUBAGENT, subagent.source, subagent.name)
            )
        return self

    def connect_middleware(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(ComponentKind.MIDDLEWARE, sources)
        return self

    def _register_files(
        self,
        component: ComponentKind,
        sources: tuple[Path | EditableFile, ...],
    ) -> None:
        for source in sources:
            self._files.append(_registration(component, source, None))

    def process(self) -> HarnessRevision:
        logger.debug("Compiling harness revision: %s", self.name)
        root = _resolve_root(self.root)
        if not _has_component(self._files, ComponentKind.PROMPT):
            raise HarnessValidationError(HarnessErrorCode.PROMPT_REQUIRED, self.name)

        components = _compile_components(root, self._files)
        repository = _snapshot_repository(root)
        content = HarnessRevisionContent(
            schema_version=HarnessSchemaVersion.V1,
            harness_name=self.name,
            repository=repository,
            components=components,
        )
        content_digest = _digest_text(content.canonical_json())
        revision = HarnessRevision(
            schema_version=content.schema_version,
            id=HarnessRevisionId(f"ofw_{content_digest.value[7:]}"),
            harness_name=content.harness_name,
            root=root,
            repository=content.repository,
            components=content.components,
        )
        _write_manifest(revision)
        logger.debug("Compiled harness revision %s", revision.id)
        return revision


def _has_component(registrations: list[_FileRegistration], kind: ComponentKind) -> bool:
    return any(registration.component is kind for registration in registrations)


def _resolve_root(root: Path) -> Path:
    try:
        resolved = root.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_FOUND, str(root)) from error
    if not resolved.is_dir():
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_DIRECTORY, str(resolved))
    return resolved


def _compile_components(
    root: Path,
    registrations: list[_FileRegistration],
) -> tuple[HarnessComponent, ...]:
    compiled: list[_CompiledAsset] = []
    for registration in registrations:
        resolved, relative = _resolve_file(root, registration.path)
        compiled.append(
            _CompiledAsset(
                component=registration.component,
                asset=HarnessAsset(
                    name=registration.name,
                    access=registration.access,
                    source=WorkspaceFile(relative_path=relative),
                    digest=_digest_file(resolved),
                ),
            )
        )
    compiled.sort(key=_compiled_asset_sort_key)
    _validate_component_boundaries(compiled)

    components: list[HarnessComponent] = []
    for kind in ComponentKind:
        assets = tuple(item.asset for item in compiled if item.component is kind)
        if not assets:
            continue
        components.append(
            HarnessComponent(
                kind=kind,
                assets=assets,
                digest=_component_digest(kind, assets),
            )
        )
    return tuple(components)


def _validate_component_boundaries(compiled: list[_CompiledAsset]) -> None:
    for index, item in enumerate(compiled):
        for existing in compiled[:index]:
            if existing.asset.source.relative_path != item.asset.source.relative_path:
                continue
            if existing.component is not item.component:
                raise HarnessValidationError(
                    HarnessErrorCode.COMPONENT_OVERLAP,
                    item.asset.source.relative_path.as_posix(),
                )
            if (
                item.component in (ComponentKind.TOOL, ComponentKind.SUBAGENT)
                and existing.asset.name != item.asset.name
            ):
                continue
            code = (
                HarnessErrorCode.DUPLICATE_ASSET
                if existing.asset.access is item.asset.access
                else HarnessErrorCode.CONFLICTING_ACCESS
            )
            raise HarnessValidationError(code, item.asset.source.relative_path.as_posix())


def _component_digest(
    kind: ComponentKind,
    assets: tuple[HarnessAsset, ...],
) -> Sha256Digest:
    fields = [kind.value]
    for asset in assets:
        fields.extend(
            (
                asset.name or "",
                asset.access.value,
                asset.source.relative_path.as_posix(),
                str(asset.digest),
            )
        )
    return _digest_text("\0".join(fields))


def _compiled_asset_sort_key(item: _CompiledAsset) -> tuple[str, str, str]:
    return (
        item.component.value,
        item.asset.source.relative_path.as_posix(),
        item.asset.name or "",
    )


def _registration(
    component: ComponentKind,
    source: Path | EditableFile,
    name: str | None,
) -> _FileRegistration:
    if isinstance(source, EditableFile):
        return _FileRegistration(component, name, source.path, AssetAccess.FIT_EDITABLE)
    if isinstance(source, Path):
        return _FileRegistration(component, name, source, AssetAccess.FROZEN)
    raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(source))


def _validate_named_source(
    name: str,
    source: Path | EditableFile,
    error_code: HarnessErrorCode,
) -> None:
    if _NAMED_SOURCE_PATTERN.fullmatch(name) is None:
        raise HarnessValidationError(error_code, name)
    if not isinstance(source, (Path, EditableFile)):
        raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(source))


def _resolve_file(root: Path, source: Path) -> tuple[Path, Path]:
    if _is_sensitive_path(source):
        raise HarnessValidationError(HarnessErrorCode.SENSITIVE_ASSET, str(source))
    candidate = source if source.is_absolute() else root / source
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise HarnessValidationError(HarnessErrorCode.MISSING_ASSET, str(source)) from error
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise HarnessValidationError(
            HarnessErrorCode.PATH_OUTSIDE_ROOT,
            str(source),
        ) from error
    if not resolved.is_file():
        raise HarnessValidationError(HarnessErrorCode.NOT_A_FILE, str(source))
    return resolved, Path(relative.as_posix())


def _is_sensitive_path(path: Path) -> bool:
    return any(
        part == ".env" or (part.startswith(".env.") and part != ".env.example")
        for part in path.parts
    )


def _snapshot_repository(root: Path) -> RepositorySnapshot:
    top_level = _run_git(root, "rev-parse", "--show-toplevel", repository_probe=True)
    try:
        git_root = Path(top_level.decode().strip()).resolve(strict=True)
    except (UnicodeDecodeError, FileNotFoundError) as error:
        raise HarnessValidationError(
            HarnessErrorCode.GIT_REPOSITORY_REQUIRED,
            str(root),
        ) from error
    if git_root != root:
        raise HarnessValidationError(HarnessErrorCode.GIT_REPOSITORY_REQUIRED, str(root))

    commit_bytes = _run_git(root, "rev-parse", "HEAD")
    try:
        commit = GitCommit(commit_bytes.decode().strip())
    except UnicodeDecodeError as error:
        raise HarnessValidationError(
            HarnessErrorCode.GIT_COMMAND_FAILED, "rev-parse HEAD"
        ) from error
    diff = _run_git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    return RepositorySnapshot(
        commit=commit,
        is_dirty=bool(diff),
        dirty_digest=_digest_bytes(diff) if diff else None,
    )


def _run_git(
    root: Path,
    *arguments: str,
    repository_probe: bool = False,
) -> bytes:
    try:
        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise HarnessValidationError(HarnessErrorCode.GIT_COMMAND_FAILED, arguments[0]) from error
    if result.returncode != 0:
        code = (
            HarnessErrorCode.GIT_REPOSITORY_REQUIRED
            if repository_probe
            else HarnessErrorCode.GIT_COMMAND_FAILED
        )
        raise HarnessValidationError(code, arguments[0])
    return result.stdout


def _digest_file(path: Path) -> Sha256Digest:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError as error:
        raise HarnessValidationError(HarnessErrorCode.MISSING_ASSET, str(path)) from error


def _digest_text(value: str) -> Sha256Digest:
    return _digest_bytes(value.encode())


def _digest_bytes(value: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value).hexdigest()}")


def _write_manifest(revision: HarnessRevision) -> None:
    manifest_path = revision.manifest_path
    payload = f"{revision.to_json()}\n"
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=manifest_path.parent,
            prefix=".manifest-",
            suffix=".json",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(manifest_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    except OSError as error:
        raise HarnessValidationError(
            HarnessErrorCode.MANIFEST_WRITE_FAILED,
            str(manifest_path),
        ) from error
