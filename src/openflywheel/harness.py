"""Compile a declared production harness into an immutable revision."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openflywheel.contracts.harness import (
    AssetAccess,
    AssetKind,
    FileAssetSource,
    HarnessAsset,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionContent,
    HarnessSchemaVersion,
    HarnessValidationError,
    PythonClassAssetSource,
    RepositorySnapshot,
)
from openflywheel.contracts.ids import GitCommit, HarnessRevisionId, Sha256Digest

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class Lifecycle:
    """Typed marker base for a user-defined lifecycle class."""


@dataclass(frozen=True, slots=True)
class EditableFile:
    path: Path


@dataclass(frozen=True, slots=True)
class MineManagedFile:
    path: Path


@dataclass(frozen=True, slots=True)
class _FileRegistration:
    kind: AssetKind
    path: Path
    access: AssetAccess


@dataclass(frozen=True, slots=True)
class _LifecycleRegistration:
    module: str
    qualified_name: str
    source_path: Path


def editable(path: Path) -> EditableFile:
    """Explicitly grant a fit campaign authority to edit one file."""
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
    """Mutable declaration builder; ``process`` returns an immutable revision."""

    name: str
    root: Path
    _files: list[_FileRegistration] = field(default_factory=list, init=False, repr=False)
    _lifecycle: _LifecycleRegistration | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.name) is None:
            raise HarnessValidationError(HarnessErrorCode.INVALID_NAME, self.name)
        if not isinstance(self.root, Path):
            raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(self.root))

    def connect_context(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(
            AssetKind.CONTEXT,
            sources,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def connect_execute(self, *sources: Path) -> Harness:
        self._register_files(AssetKind.EXECUTION, sources, (AssetAccess.FROZEN,))
        return self

    def connect_tools(self, *sources: Path | EditableFile) -> Harness:
        self._register_files(
            AssetKind.TOOLING,
            sources,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def connect_observability(self, *sources: Path) -> Harness:
        self._register_files(AssetKind.OBSERVABILITY, sources, (AssetAccess.FROZEN,))
        return self

    def connect_verifiers(self, *sources: Path | MineManagedFile) -> Harness:
        self._register_files(
            AssetKind.VERIFIER,
            sources,
            (AssetAccess.FROZEN, AssetAccess.MINE_MANAGED),
        )
        return self

    def connect_governance(self, *sources: Path) -> Harness:
        self._register_files(AssetKind.GOVERNANCE, sources, (AssetAccess.FROZEN,))
        return self

    def connect_lifecycle(
        self,
        lifecycle: type[Lifecycle],
        *middleware: Path | EditableFile,
    ) -> Harness:
        if self._lifecycle is not None:
            raise HarnessValidationError(
                HarnessErrorCode.LIFECYCLE_ALREADY_CONNECTED,
                self.name,
            )
        try:
            supported_lifecycle = issubclass(lifecycle, Lifecycle)
        except TypeError as error:
            raise HarnessValidationError(
                HarnessErrorCode.UNINSPECTABLE_LIFECYCLE,
                repr(lifecycle),
            ) from error
        if not supported_lifecycle or "<locals>" in lifecycle.__qualname__:
            raise HarnessValidationError(
                HarnessErrorCode.UNINSPECTABLE_LIFECYCLE,
                repr(lifecycle),
            )
        try:
            source_file = inspect.getsourcefile(lifecycle)
        except TypeError as error:
            raise HarnessValidationError(
                HarnessErrorCode.UNINSPECTABLE_LIFECYCLE,
                lifecycle.__qualname__,
            ) from error
        if source_file is None:
            raise HarnessValidationError(
                HarnessErrorCode.UNINSPECTABLE_LIFECYCLE,
                lifecycle.__qualname__,
            )
        self._lifecycle = _LifecycleRegistration(
            module=lifecycle.__module__,
            qualified_name=lifecycle.__qualname__,
            source_path=Path(source_file),
        )
        self._register_files(
            AssetKind.LIFECYCLE,
            middleware,
            (AssetAccess.FROZEN, AssetAccess.FIT_EDITABLE),
        )
        return self

    def _register_files(
        self,
        kind: AssetKind,
        sources: tuple[Path | EditableFile | MineManagedFile, ...],
        allowed_access: tuple[AssetAccess, ...],
    ) -> None:
        for source in sources:
            if isinstance(source, EditableFile):
                registration = _FileRegistration(kind, source.path, AssetAccess.FIT_EDITABLE)
            elif isinstance(source, MineManagedFile):
                registration = _FileRegistration(kind, source.path, AssetAccess.MINE_MANAGED)
            elif isinstance(source, Path):
                registration = _FileRegistration(kind, source, AssetAccess.FROZEN)
            else:
                raise HarnessValidationError(HarnessErrorCode.INVALID_SOURCE, repr(source))
            if registration.access not in allowed_access:
                raise HarnessValidationError(
                    HarnessErrorCode.ACCESS_NOT_ALLOWED,
                    f"{kind.value}:{registration.access.value}:{registration.path}",
                )
            self._files.append(registration)

    def process(self) -> HarnessRevision:
        logger.debug("Compiling harness revision: %s", self.name)
        root = _resolve_root(self.root)
        if not any(registration.kind is AssetKind.CONTEXT for registration in self._files):
            raise HarnessValidationError(HarnessErrorCode.CONTEXT_REQUIRED, self.name)
        if self._lifecycle is None:
            raise HarnessValidationError(HarnessErrorCode.LIFECYCLE_REQUIRED, self.name)

        assets = _compile_file_assets(root, self._files)
        assets.append(_compile_lifecycle_asset(root, self._lifecycle))
        _validate_unique_assets(assets)
        assets.sort(key=_asset_sort_key)
        repository = _snapshot_repository(root)
        content = HarnessRevisionContent(
            schema_version=HarnessSchemaVersion.V1,
            harness_name=self.name,
            repository=repository,
            assets=tuple(assets),
        )
        revision_id = HarnessRevisionId(f"ofw_{_digest_text(content.model_dump_json())[7:]}")
        revision = HarnessRevision(
            schema_version=content.schema_version,
            id=revision_id,
            harness_name=content.harness_name,
            root=root,
            repository=content.repository,
            assets=content.assets,
        )
        _write_manifest(revision)
        logger.debug("Compiled harness revision %s", revision.id)
        return revision


def _resolve_root(root: Path) -> Path:
    try:
        resolved = root.expanduser().resolve(strict=True)
    except FileNotFoundError as error:
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_FOUND, str(root)) from error
    if not resolved.is_dir():
        raise HarnessValidationError(HarnessErrorCode.ROOT_NOT_DIRECTORY, str(resolved))
    return resolved


def _compile_file_assets(
    root: Path,
    registrations: list[_FileRegistration],
) -> list[HarnessAsset]:
    assets: list[HarnessAsset] = []
    for registration in registrations:
        resolved, relative = _resolve_file(root, registration.path)
        assets.append(
            HarnessAsset(
                kind=registration.kind,
                access=registration.access,
                source=FileAssetSource(relative_path=relative),
                digest=_digest_file(resolved),
            )
        )
    return assets


def _validate_unique_assets(assets: list[HarnessAsset]) -> None:
    for index, asset in enumerate(assets):
        for existing in assets[:index]:
            if existing.source.relative_path != asset.source.relative_path:
                continue
            code = (
                HarnessErrorCode.DUPLICATE_ASSET
                if existing.access is asset.access
                else HarnessErrorCode.CONFLICTING_ACCESS
            )
            raise HarnessValidationError(code, asset.source.relative_path.as_posix())


def _compile_lifecycle_asset(
    root: Path,
    registration: _LifecycleRegistration,
) -> HarnessAsset:
    resolved, relative = _resolve_file(root, registration.source_path)
    return HarnessAsset(
        kind=AssetKind.LIFECYCLE,
        access=AssetAccess.FROZEN,
        source=PythonClassAssetSource(
            module=registration.module,
            qualified_name=registration.qualified_name,
            relative_path=relative,
        ),
        digest=_digest_file(resolved),
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


def _asset_sort_key(asset: HarnessAsset) -> tuple[str, str, str]:
    source = asset.source.relative_path.as_posix()
    return asset.kind.value, source, asset.access.value


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
    payload = f"{revision.model_dump_json(indent=2)}\n"
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
