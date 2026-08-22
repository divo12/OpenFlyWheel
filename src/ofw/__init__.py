"""Public OpenFlyWheel harness API."""

from importlib import import_module
from types import ModuleType

from ofw.contracts import (
    AssetAccess,
    AssetKind,
    FileAssetSource,
    GitCommit,
    HarnessAsset,
    HarnessErrorCode,
    HarnessRevision,
    HarnessRevisionId,
    HarnessValidationError,
    PythonClassAssetSource,
    RepositorySnapshot,
    Sha256Digest,
)
from ofw.harness import (
    EditableFile,
    Harness,
    Lifecycle,
    MineManagedFile,
    editable,
    mine_managed,
)

ofw: ModuleType = import_module(__name__)

__all__ = [
    "AssetAccess",
    "AssetKind",
    "EditableFile",
    "FileAssetSource",
    "GitCommit",
    "Harness",
    "HarnessAsset",
    "HarnessErrorCode",
    "HarnessRevision",
    "HarnessRevisionId",
    "HarnessValidationError",
    "Lifecycle",
    "MineManagedFile",
    "PythonClassAssetSource",
    "RepositorySnapshot",
    "Sha256Digest",
    "editable",
    "mine_managed",
    "ofw",
]
