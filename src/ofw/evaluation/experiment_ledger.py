"""Typed immutable experiment-attempt ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ofw.contracts import GitCommit
from ofw.evaluation.local_workspace import (
    FileWorkspaceArtifactStore,
    WorkspaceArtifactFailure,
)
from ofw.observability.langfuse.domain import ScoreId

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_ARTIFACT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_TEXT_LIMIT = 4000
_RECEIPT_LIMIT = 500

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=_IDENTIFIER_PATTERN)]
Revision = Annotated[str, Field(pattern=_REVISION_PATTERN)]
LedgerText = Annotated[str, Field(min_length=1, max_length=_TEXT_LIMIT)]
VerifierReceipts = Annotated[tuple[Identifier, ...], Field(max_length=_RECEIPT_LIMIT)]
WorkspaceRoot = Annotated[Path, Field(strict=False)]


class ExperimentDecision(StrEnum):
    ADMIT = "admit"
    REJECT = "reject"


class ExperimentLedgerErrorCode(StrEnum):
    INVALID_ATTEMPT = "invalid_attempt"
    INVALID_WORKSPACE = "invalid_workspace"
    ARTIFACT_CONFLICT = "artifact_conflict"
    ARTIFACT_TOO_LARGE = "artifact_too_large"
    WRITE_FAILED = "write_failed"


class ExperimentLedgerFailure(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: ExperimentLedgerErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class ExperimentId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) > 256 or re.fullmatch(_IDENTIFIER_PATTERN, self.value) is None:
            raise ExperimentLedgerFailure(
                ExperimentLedgerErrorCode.INVALID_ATTEMPT,
                "experiment_id",
            )


@dataclass(frozen=True, slots=True)
class ExperimentRunId:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) > 256 or re.fullmatch(_IDENTIFIER_PATTERN, self.value) is None:
            raise ExperimentLedgerFailure(
                ExperimentLedgerErrorCode.INVALID_ATTEMPT,
                "run_id",
            )


@dataclass(frozen=True, slots=True)
class ExperimentAttempt:
    experiment_id: ExperimentId
    run_id: ExperimentRunId
    parent_revision: GitCommit
    hypothesis: str
    verifier_receipts: tuple[ScoreId, ...]
    gate_decision: ExperimentDecision
    total_cost_usd: float | None
    latency_seconds: float | None
    rejection_reason: str | None
    decided_at: datetime

    def __post_init__(self) -> None:
        try:
            _ExperimentFields(
                experiment_id=self.experiment_id.value,
                run_id=self.run_id.value,
                parent_revision=self.parent_revision.value,
                hypothesis=self.hypothesis,
                verifier_receipts=tuple(receipt.value for receipt in self.verifier_receipts),
                gate_decision=self.gate_decision,
                total_cost_usd=self.total_cost_usd,
                latency_seconds=self.latency_seconds,
                rejection_reason=self.rejection_reason,
                decided_at=self.decided_at,
            )
        except ValidationError:
            raise ExperimentLedgerFailure(
                ExperimentLedgerErrorCode.INVALID_ATTEMPT,
                "attempt",
            ) from None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class _ExperimentFields(StrictModel):
    experiment_id: Identifier
    run_id: Identifier
    parent_revision: Revision
    hypothesis: LedgerText
    verifier_receipts: VerifierReceipts
    gate_decision: ExperimentDecision
    total_cost_usd: float | None = Field(strict=True, default=None, ge=0.0)
    latency_seconds: float | None = Field(strict=True, default=None, ge=0.0)
    rejection_reason: LedgerText | None = None
    decided_at: datetime

    @field_validator("hypothesis", "rejection_reason")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("verifier_receipts")
    @classmethod
    def validate_receipts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("verifier receipts must be unique")
        return value

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("decided_at must be UTC")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.gate_decision is ExperimentDecision.ADMIT:
            self._validate_admitted()
        else:
            self._validate_rejected()
        return self

    def _validate_admitted(self) -> None:
        if not self.verifier_receipts:
            raise ValueError("admitted attempts require verifier receipts")
        if self.total_cost_usd is None or self.latency_seconds is None:
            raise ValueError("admitted attempts require cost and latency")
        if self.rejection_reason is not None:
            raise ValueError("admitted attempts cannot have a rejection reason")

    def _validate_rejected(self) -> None:
        if self.rejection_reason is None:
            raise ValueError("rejected attempts require a rejection reason")


class RecordExperimentInput(_ExperimentFields):
    workspace_root: WorkspaceRoot

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    def to_attempt(self) -> ExperimentAttempt:
        return ExperimentAttempt(
            experiment_id=ExperimentId(self.experiment_id),
            run_id=ExperimentRunId(self.run_id),
            parent_revision=GitCommit(self.parent_revision),
            hypothesis=self.hypothesis,
            verifier_receipts=tuple(ScoreId(receipt) for receipt in self.verifier_receipts),
            gate_decision=self.gate_decision,
            total_cost_usd=self.total_cost_usd,
            latency_seconds=self.latency_seconds,
            rejection_reason=self.rejection_reason,
            decided_at=self.decided_at,
        )


class ExperimentRecordStatus(StrEnum):
    SUCCESS = "success"


class ExperimentRecordObservation(StrictModel):
    status: ExperimentRecordStatus
    summary: str = Field(min_length=1, max_length=256)
    next_actions: tuple[str, ...] = Field(max_length=2)
    artifacts: tuple[str, ...] = Field(min_length=2, max_length=2)
    experiment_id: Identifier
    run_id: Identifier
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    relative_path: Path
    gate_decision: ExperimentDecision


class ExperimentArtifact(_ExperimentFields):
    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)

    @classmethod
    def from_attempt(cls, artifact_id: str, attempt: ExperimentAttempt) -> ExperimentArtifact:
        return cls(
            artifact_id=artifact_id,
            experiment_id=attempt.experiment_id.value,
            run_id=attempt.run_id.value,
            parent_revision=attempt.parent_revision.value,
            hypothesis=attempt.hypothesis,
            verifier_receipts=tuple(receipt.value for receipt in attempt.verifier_receipts),
            gate_decision=attempt.gate_decision,
            total_cost_usd=attempt.total_cost_usd,
            latency_seconds=attempt.latency_seconds,
            rejection_reason=attempt.rejection_reason,
            decided_at=attempt.decided_at,
        )


@dataclass(frozen=True, slots=True)
class ExperimentArtifactReceipt:
    artifact_id: str
    relative_path: Path


class ExperimentLedger(Protocol):
    def store(self, root: Path, attempt: ExperimentAttempt) -> ExperimentArtifactReceipt: ...


@dataclass(frozen=True, slots=True)
class ExperimentLedgerService:
    ledger: ExperimentLedger

    def record(self, request: RecordExperimentInput) -> ExperimentRecordObservation:
        attempt = request.to_attempt()
        receipt = self.ledger.store(request.workspace_root, attempt)
        return ExperimentRecordObservation(
            status=ExperimentRecordStatus.SUCCESS,
            summary="Stored one experiment attempt in the local ledger.",
            next_actions=("Retain the artifact path with the candidate decision.",),
            artifacts=(str(receipt.relative_path), receipt.artifact_id),
            experiment_id=attempt.experiment_id.value,
            run_id=attempt.run_id.value,
            artifact_id=receipt.artifact_id,
            relative_path=receipt.relative_path,
            gate_decision=attempt.gate_decision,
        )


class FileExperimentLedger:
    def store(self, root: Path, attempt: ExperimentAttempt) -> ExperimentArtifactReceipt:
        artifact_id = _artifact_id(attempt)
        try:
            artifact = ExperimentArtifact.from_attempt(artifact_id, attempt)
            content = (artifact.model_dump_json(indent=2) + "\n").encode("utf-8")
            receipt = FileWorkspaceArtifactStore("experiments").store(root, artifact_id, content)
        except WorkspaceArtifactFailure as error:
            raise ExperimentLedgerFailure(
                ExperimentLedgerErrorCode(error.code.value),
                error.subject,
            ) from None
        except (OSError, RuntimeError, UnicodeError):
            raise ExperimentLedgerFailure(
                ExperimentLedgerErrorCode.WRITE_FAILED,
                artifact_id,
            ) from None
        return ExperimentArtifactReceipt(receipt.artifact_id, receipt.relative_path)


def _artifact_id(attempt: ExperimentAttempt) -> str:
    identity = "\0".join(("ofw.experiment", attempt.experiment_id.value, attempt.run_id.value))
    return str(uuid5(NAMESPACE_URL, identity))
