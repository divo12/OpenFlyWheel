"""Governed AHE candidate manifests and isolated Git worktrees."""

from __future__ import annotations

import hashlib
import math
import shutil
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from ofw.contracts import (
    AssetAccess,
    ComponentKind,
    HarnessAsset,
    HarnessRevision,
    HarnessRevisionId,
    Sha256Digest,
)
from ofw.diagnosis import ClusterId
from ofw.mine import write_artifact


class CandidateSchemaVersion(IntEnum):
    V1 = 1


class CandidateErrorCode(StrEnum):
    REVISION_MISMATCH = "revision_mismatch"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    FILE_NOT_EDITABLE = "file_not_editable"
    BASE_DIGEST_MISMATCH = "base_digest_mismatch"
    SELECTOR_INVALID = "selector_invalid"
    BUDGET_EXCEEDED = "budget_exceeded"
    WORKTREE_FAILED = "worktree_failed"
    FROZEN_ASSET_CHANGED = "frozen_asset_changed"
    NO_CHANGES = "no_changes"


class CandidateError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: CandidateErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class CandidateId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class CandidateBranch:
    value: str


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < self.start:
            raise CandidateError(CandidateErrorCode.SELECTOR_INVALID, "invalid line range")


@dataclass(frozen=True, slots=True)
class FileEdit:
    path: Path
    expected_digest: Sha256Digest
    replacement: str
    selector: LineRange | None = None

    def __post_init__(self) -> None:
        if self.path.is_absolute() or ".." in self.path.parts:
            raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, self.path.as_posix())


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    revision_id: HarnessRevisionId
    cluster_ids: tuple[ClusterId, ...]
    eval_case_ids: tuple[str, ...]
    memory_cluster_ids: tuple[ClusterId, ...]

    @property
    def digest(self) -> Sha256Digest:
        return _digest_text(
            "\0".join(
                (
                    str(self.revision_id),
                    *(cluster.value for cluster in self.cluster_ids),
                    *self.eval_case_ids,
                    *(cluster.value for cluster in self.memory_cluster_ids),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class ChangePrediction:
    hypothesis: str
    target_clusters: tuple[ClusterId, ...]
    at_risk_cases: tuple[str, ...]
    affected_components: tuple[ComponentKind, ...]
    memory_candidates: tuple[ClusterId, ...]
    expected_quality_delta: float
    expected_cost_delta: float
    expected_latency_delta: float

    def __post_init__(self) -> None:
        if (
            not self.hypothesis
            or not self.target_clusters
            or not self.affected_components
            or not all(
                math.isfinite(value)
                for value in (
                    self.expected_quality_delta,
                    self.expected_cost_delta,
                    self.expected_latency_delta,
                )
            )
        ):
            raise CandidateError(CandidateErrorCode.EVIDENCE_MISMATCH, "invalid prediction")


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    maximum_files: int
    maximum_changed_bytes: int
    allowed_components: tuple[ComponentKind, ...]

    def __post_init__(self) -> None:
        if self.maximum_files < 1 or self.maximum_changed_bytes < 1 or not self.allowed_components:
            raise CandidateError(CandidateErrorCode.BUDGET_EXCEEDED, "invalid policy")

    @property
    def digest(self) -> Sha256Digest:
        return _digest_text(
            "\0".join(
                (
                    str(self.maximum_files),
                    str(self.maximum_changed_bytes),
                    *(component.value for component in self.allowed_components),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class EditIntent:
    path: Path
    base_digest: Sha256Digest
    replacement_digest: Sha256Digest
    selector: LineRange | None


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    schema_version: CandidateSchemaVersion
    candidate_id: CandidateId
    base_revision_id: HarnessRevisionId
    evidence_digest: Sha256Digest
    policy_digest: Sha256Digest
    hypothesis: str
    target_clusters: tuple[ClusterId, ...]
    at_risk_cases: tuple[str, ...]
    affected_components: tuple[ComponentKind, ...]
    memory_candidates: tuple[ClusterId, ...]
    expected_quality_delta: float
    expected_cost_delta: float
    expected_latency_delta: float
    edits: tuple[EditIntent, ...]

    def to_json(self) -> str:
        return _MANIFEST_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class CandidateRevision:
    id: CandidateId
    base_revision_id: HarnessRevisionId
    branch: CandidateBranch
    root: Path
    changed_files: tuple[Path, ...]
    changed_components: tuple[ComponentKind, ...]
    manifest_path: Path
    diff_path: Path


@dataclass(slots=True)
class CandidateWorkspace:
    source_root: Path
    parent: Path
    root: Path
    branch: CandidateBranch
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        _git(self.source_root, "worktree", "remove", "--force", str(self.root))
        _git(self.source_root, "branch", "-D", self.branch.value)
        shutil.rmtree(self.parent, ignore_errors=True)
        self.closed = True


@dataclass(frozen=True, slots=True)
class CandidateBuild:
    candidate: CandidateRevision
    workspace: CandidateWorkspace


_MANIFEST_ADAPTER: TypeAdapter[CandidateManifest] = TypeAdapter(CandidateManifest)


@dataclass(frozen=True, slots=True)
class CandidateBuilder:
    revision: HarnessRevision
    evidence: CandidateEvidence
    policy: CandidatePolicy

    def create(
        self,
        edits: tuple[FileEdit, ...],
        prediction: ChangePrediction,
    ) -> CandidateBuild:
        self._validate_request(edits, prediction)
        ordered_edits = tuple(sorted(edits, key=_edit_sort_key))
        intents = tuple(
            EditIntent(
                edit.path,
                edit.expected_digest,
                _digest_text(edit.replacement),
                edit.selector,
            )
            for edit in ordered_edits
        )
        candidate_id = CandidateId(
            "candidate_"
            + hashlib.sha256(
                "\0".join(
                    (
                        str(self.revision.id),
                        str(self.evidence.digest),
                        str(self.policy.digest),
                        str(int(CandidateSchemaVersion.V1)),
                        prediction.hypothesis,
                        *(cluster.value for cluster in prediction.target_clusters),
                        *prediction.at_risk_cases,
                        *(component.value for component in prediction.affected_components),
                        *(cluster.value for cluster in prediction.memory_candidates),
                        str(prediction.expected_quality_delta),
                        str(prediction.expected_cost_delta),
                        str(prediction.expected_latency_delta),
                        *(intent.path.as_posix() for intent in intents),
                        *(str(intent.base_digest) for intent in intents),
                        *(str(intent.replacement_digest) for intent in intents),
                        *(_selector_text(intent.selector) for intent in intents),
                    )
                ).encode()
            ).hexdigest()
        )
        manifest = CandidateManifest(
            CandidateSchemaVersion.V1,
            candidate_id,
            self.revision.id,
            self.evidence.digest,
            self.policy.digest,
            prediction.hypothesis,
            prediction.target_clusters,
            prediction.at_risk_cases,
            prediction.affected_components,
            prediction.memory_candidates,
            prediction.expected_quality_delta,
            prediction.expected_cost_delta,
            prediction.expected_latency_delta,
            intents,
        )
        manifest_path = (
            self.revision.root / ".ofw" / "candidates" / str(candidate_id) / "manifest.json"
        )
        write_artifact(manifest_path, f"{manifest.to_json()}\n".encode())
        workspace = self._worktree(candidate_id)
        try:
            for edit in ordered_edits:
                _apply_edit(workspace.root, edit)
            self._validate_frozen(workspace.root)
            diff = _git_bytes(workspace.root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
            if not diff:
                raise CandidateError(CandidateErrorCode.NO_CHANGES, str(candidate_id))
            if len(diff) > self.policy.maximum_changed_bytes:
                raise CandidateError(CandidateErrorCode.BUDGET_EXCEEDED, str(len(diff)))
            diff_path = manifest_path.with_name("candidate.patch")
            write_artifact(diff_path, diff)
            components = tuple(
                sorted(
                    {self._component(edit.path) for edit in ordered_edits},
                    key=_component_sort_key,
                )
            )
            candidate = CandidateRevision(
                candidate_id,
                self.revision.id,
                workspace.branch,
                workspace.root,
                tuple(edit.path for edit in ordered_edits),
                components,
                manifest_path,
                diff_path,
            )
            return CandidateBuild(candidate, workspace)
        except Exception:
            workspace.close()
            raise

    def _validate_request(
        self,
        edits: tuple[FileEdit, ...],
        prediction: ChangePrediction,
    ) -> None:
        if self.evidence.revision_id != self.revision.id:
            raise CandidateError(CandidateErrorCode.REVISION_MISMATCH, str(self.revision.id))
        if (
            any(cluster not in self.evidence.cluster_ids for cluster in prediction.target_clusters)
            or any(case not in self.evidence.eval_case_ids for case in prediction.at_risk_cases)
            or any(
                cluster not in self.evidence.memory_cluster_ids
                for cluster in prediction.memory_candidates
            )
        ):
            raise CandidateError(CandidateErrorCode.EVIDENCE_MISMATCH, prediction.hypothesis)
        if (
            not edits
            or len(edits) > self.policy.maximum_files
            or len({edit.path for edit in edits}) != len(edits)
            or sum(len(edit.replacement.encode()) for edit in edits)
            > self.policy.maximum_changed_bytes
        ):
            raise CandidateError(CandidateErrorCode.BUDGET_EXCEEDED, str(len(edits)))
        for edit in edits:
            component = self._component(edit.path)
            if component not in self.policy.allowed_components:
                raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, edit.path.as_posix())
            source = self.revision.root / edit.path
            actual = _digest_file(source)
            asset = self._asset(edit.path)
            if actual != edit.expected_digest or actual != asset.digest:
                raise CandidateError(
                    CandidateErrorCode.BASE_DIGEST_MISMATCH,
                    edit.path.as_posix(),
                )
        if any(
            component not in prediction.affected_components
            for component in {self._component(edit.path) for edit in edits}
        ):
            raise CandidateError(CandidateErrorCode.EVIDENCE_MISMATCH, "affected components")

    def _asset(self, path: Path) -> HarnessAsset:
        for component in self.revision.components:
            for asset in component.assets:
                if asset.source.relative_path == path and asset.access is AssetAccess.FIT_EDITABLE:
                    return asset
        raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, path.as_posix())

    def _component(self, path: Path) -> ComponentKind:
        for component in self.revision.components:
            if any(
                asset.source.relative_path == path and asset.access is AssetAccess.FIT_EDITABLE
                for asset in component.assets
            ):
                return component.kind
        raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, path.as_posix())

    def _worktree(self, candidate_id: CandidateId) -> CandidateWorkspace:
        parent = Path(tempfile.mkdtemp(prefix="ofw-candidate-"))
        root = parent / "worktree"
        branch = CandidateBranch(f"ofw-{candidate_id.value[-16:]}")
        try:
            _git(
                self.revision.root,
                "worktree",
                "add",
                "-b",
                branch.value,
                str(root),
                str(self.revision.repository.commit),
            )
            workspace = CandidateWorkspace(self.revision.root, parent, root, branch)
            try:
                dirty = _git_bytes(
                    self.revision.root,
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "HEAD",
                    "--",
                )
                if dirty:
                    _git_with_input(root, dirty, "apply", "--binary", "-")
                _copy_revision_assets(self.revision, root)
            except Exception:
                workspace.close()
                raise
            return workspace
        except Exception:
            shutil.rmtree(parent, ignore_errors=True)
            raise

    def _validate_frozen(self, root: Path) -> None:
        for component in self.revision.components:
            for asset in component.assets:
                if asset.access is AssetAccess.FROZEN:
                    actual = _digest_file(root / asset.source.relative_path)
                    if actual != asset.digest:
                        raise CandidateError(
                            CandidateErrorCode.FROZEN_ASSET_CHANGED,
                            asset.source.relative_path.as_posix(),
                        )


def _apply_edit(root: Path, edit: FileEdit) -> None:
    path = (root / edit.path).resolve(strict=True)
    try:
        path.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, edit.path.as_posix()) from error
    if edit.selector is None:
        path.write_text(edit.replacement, encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if edit.selector.end > len(lines):
        raise CandidateError(CandidateErrorCode.SELECTOR_INVALID, edit.path.as_posix())
    replacement = edit.replacement.splitlines(keepends=True)
    lines[edit.selector.start - 1 : edit.selector.end] = replacement
    path.write_text("".join(lines), encoding="utf-8")


def _git(root: Path, *arguments: str) -> None:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CandidateError(CandidateErrorCode.WORKTREE_FAILED, arguments[0])


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CandidateError(CandidateErrorCode.WORKTREE_FAILED, arguments[0])
    return result.stdout


def _git_with_input(root: Path, payload: bytes, *arguments: str) -> None:
    result = subprocess.run(  # nosec B603
        ("git", "-C", str(root), *arguments),
        input=payload,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CandidateError(CandidateErrorCode.WORKTREE_FAILED, arguments[0])


def _digest_file(path: Path) -> Sha256Digest:
    try:
        return Sha256Digest(f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    except OSError as error:
        raise CandidateError(CandidateErrorCode.FILE_NOT_EDITABLE, str(path)) from error


def _digest_text(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value.encode()).hexdigest()}")


def _component_sort_key(component: ComponentKind) -> str:
    return component.value


def _edit_sort_key(edit: FileEdit) -> str:
    return edit.path.as_posix()


def _selector_text(selector: LineRange | None) -> str:
    return "all" if selector is None else f"{selector.start}:{selector.end}"


def _copy_revision_assets(revision: HarnessRevision, root: Path) -> None:
    for asset in revision.assets:
        source = revision.root / asset.source.relative_path
        destination = root / asset.source.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
