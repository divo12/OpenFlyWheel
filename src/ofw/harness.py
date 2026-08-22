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


@dataclass(frozen=True, slots=True)
class EditableFile:
    path: Path


@dataclass(frozen=True, slots=True)
class MineManagedFile:
    path: Path


@dataclass(frozen=True, slots=True)
class _FileRegistration:
    component: ComponentKind
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


def mine_managed(path: Path) -> MineManagedFile:
    """Grant Mine authority to maintain one verifier or eval file."""
    if not isinstance(path, Path):
        raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(path))
    return MineManagedFile(path=path)


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

    def connect_context(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(
            ComponentKind.CONTEXT,
            sources,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def connect_execute(self, *sources: Path) -> Harness:
        self._register_files(ComponentKind.EXECUTION, sources, (AssetAccess.FROZEN,))
        return self

    def connect_tools(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(
            ComponentKind.TOOLING,
            sources,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def connect_observability(self, *sources: Path) -> Harness:
        self._register_files(ComponentKind.OBSERVABILITY, sources, (AssetAccess.FROZEN,))
        return self

    def connect_verifiers(self, *sources: Path | MineManagedFile) -> Harness:
        self._register_files(
            ComponentKind.VERIFIER,
            sources,
            (AssetAccess.FROZEN, AssetAccess.MINE_MANAGED),
        )
        return self

    def connect_lifecycle(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(
            ComponentKind.LIFECYCLE,
            sources,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def connect_governance(self, *sources: Path) -> Harness:
        self._register_files(ComponentKind.GOVERNANCE, sources, (AssetAccess.FROZEN,))
        return self

    def _register_files(
        self,
        component: ComponentKind,
        sources: tuple[Path | EditableFile | MineManagedFile, ...],
        allowed_access: tuple[AssetAccess, ...],
    ) -> None:
        for source in sources:
            if isinstance(source, EditableFile):
                registration = _FileRegistration(
                    component,
                    source.path,
                    AssetAccess.FIT_EDITABLE,
                )
            elif isinstance(source, MineManagedFile):
                registration = _FileRegistration(
                    component,
                    source.path,
                    AssetAccess.MINE_MANAGED,
                )
            elif isinstance(source, Path):
                registration = _FileRegistration(component, source, AssetAccess.FROZEN)
            else:
                raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(source))
            if registration.access not in allowed_access:
                raise HarnessValidationError(
                    HarnessErrorCode.ACCESS_NOT_ALLOWED,
                    f"{component.value}:{registration.access.value}:{registration.path}",
                )
            self._files.append(registration)

    def process(self) -> HarnessRevision:
        logger.debug("Compiling harness revision: %s", self.name)
        root = _resolve_root(self.root)
        if not _has_component(self._files, ComponentKind.CONTEXT):
            raise HarnessValidationError(HarnessErrorCode.CONTEXT_REQUIRED, self.name)
        if not _has_component(self._files, ComponentKind.LIFECYCLE):
            raise HarnessValidationError(HarnessErrorCode.LIFECYCLE_REQUIRED, self.name)

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
        item.asset.access.value,
    )


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
