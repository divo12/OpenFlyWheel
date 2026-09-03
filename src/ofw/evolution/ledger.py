"""Append-only, typed evolution events in Git's common control directory."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, TypeAlias
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from ofw.evolution.gate import PromotionStatus
from ofw.preparation.contracts import StrictModel
from ofw.preparation.policy import ExperimentPolicyFailure, experiment_control_directory
from ofw.safe_file import (
    SafeFileErrorCode,
    SafeFileFailure,
    open_directory_chain,
    read_bounded,
)

_DIGEST = r"sha256:[0-9a-f]{64}"
_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:@/-]*"
_EXPERIMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_COMMIT = r"[0-9a-f]{40}"
_LEDGER_LIMIT_BYTES = 4 * 1024 * 1024
_EVENT_LIMIT_BYTES = 256 * 1024

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=_IDENTIFIER)]
Digest = Annotated[str, Field(pattern=_DIGEST)]


class EvolutionEventType(StrEnum):
    EVOLUTION_STARTED = "EvolutionStarted"
    HYPOTHESIS_LINKED = "HypothesisLinked"
    CANDIDATE_PREPARED = "CandidatePrepared"
    CANDIDATE_SUBMITTED = "CandidateSubmitted"
    RUN_STARTED = "RunStarted"
    RUN_COMPLETED = "RunCompleted"
    GATE_DECIDED = "GateDecided"
    CANDIDATE_ACCEPTED = "CandidateAccepted"
    CANDIDATE_REJECTED = "CandidateRejected"
    RELEASE_PUBLISHED = "ReleasePublished"
    RELEASE_ROLLED_BACK = "ReleaseRolledBack"
    EVOLUTION_STOPPED = "EvolutionStopped"
    EXTERNAL_OPERATION_INTENT = "ExternalOperationIntent"
    EXTERNAL_OPERATION_BLOCKED = "ExternalOperationBlocked"


class ExternalOperation(StrEnum):
    CANDIDATE = "candidate"
    HARBOR = "harbor"
    PUBLICATION = "publication"


class EvolutionStopReason(StrEnum):
    QUALITY_TARGET = "quality_target"
    MAX_ITERATIONS = "max_iterations"
    NO_IMPROVEMENT = "no_improvement"
    COST_LIMIT = "cost_limit"
    LATENCY_LIMIT = "latency_limit"
    BASELINE_DEADLINE = "baseline_deadline"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    USER_STOP = "user_stop"
    BLOCKED = "blocked"


class EvolutionStarted(StrictModel):
    policy_digest: Digest
    accepted_commit: str | None = Field(default=None, pattern=_COMMIT)
    accepted_content_id: Digest | None = None
    accepted_release_id: Identifier | None = None


class HypothesisLinked(StrictModel):
    hypothesis_id: Digest
    source_commit: str = Field(pattern=_COMMIT)


class CandidatePrepared(StrictModel):
    iteration: int = Field(strict=True, ge=1, le=100)
    candidate_workspace_id: Identifier
    source_commit: str | None = Field(default=None, pattern=_COMMIT)
    source_content_id: Digest | None = None
    source_release_id: Identifier | None = None


class CandidateSubmitted(StrictModel):
    candidate_id: Digest
    candidate_commit: str = Field(pattern=_COMMIT)


class RunStarted(StrictModel):
    run_id: Identifier
    idempotency_key: Digest


class RunCompleted(StrictModel):
    run_id: Identifier
    receipt_id: Digest


class GateDecided(StrictModel):
    decision_id: Digest
    candidate_run_id: Identifier
    status: PromotionStatus
    reasons: tuple[Identifier, ...] = Field(max_length=20)


class CandidateAccepted(StrictModel):
    candidate_id: Digest
    decision_id: Digest
    candidate_commit: str | None = Field(default=None, pattern=_COMMIT)
    accepted_content_id: Digest | None = None


class CandidateRejected(StrictModel):
    candidate_id: Digest
    decision_id: Digest
    reasons: tuple[Identifier, ...] = Field(max_length=20)


class ReleasePublished(StrictModel):
    release_id: Identifier
    content_commit: str | None = Field(default=None, pattern=_COMMIT)
    content_id: Digest | None = None
    target_reached: bool = False


class ReleaseRolledBack(StrictModel):
    release_id: Identifier
    target_release_id: Identifier
    content_commit: str | None = Field(default=None, pattern=_COMMIT)
    content_id: Digest | None = None


class EvolutionStopped(StrictModel):
    reason: EvolutionStopReason


class ExternalOperationIntent(StrictModel):
    operation: ExternalOperation
    idempotency_key: Digest
    target: Identifier


class ExternalOperationBlocked(StrictModel):
    operation: ExternalOperation
    idempotency_key: Digest
    reason: Identifier


EvolutionEventPayload: TypeAlias = (
    EvolutionStarted
    | HypothesisLinked
    | CandidatePrepared
    | CandidateSubmitted
    | RunStarted
    | RunCompleted
    | GateDecided
    | CandidateAccepted
    | CandidateRejected
    | ReleasePublished
    | ReleaseRolledBack
    | EvolutionStopped
    | ExternalOperationIntent
    | ExternalOperationBlocked
)


class EvolutionLedgerErrorCode(StrEnum):
    INVALID_EVENT = "invalid_event"
    INVALID_WORKSPACE = "invalid_workspace"
    BUSY = "busy"
    EVENT_CONFLICT = "event_conflict"
    CORRUPT_LEDGER = "corrupt_ledger"
    SEQUENCE_GAP = "sequence_gap"
    CURSOR_INVALID = "cursor_invalid"
    LEDGER_TOO_LARGE = "ledger_too_large"
    WRITE_FAILED = "write_failed"


class EvolutionLedgerFailure(Exception):
    __slots__ = ("code", "subject", "last_valid_sequence")

    def __init__(
        self,
        code: EvolutionLedgerErrorCode,
        subject: str,
        last_valid_sequence: int = 0,
    ) -> None:
        self.code = code
        self.subject = subject
        self.last_valid_sequence = last_valid_sequence
        super().__init__(f"{code.value}: {subject}")


class EvolutionEvent(StrictModel):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=_EXPERIMENT, max_length=80)
    sequence: int = Field(strict=True, ge=1)
    event_id: Digest
    event_type: EvolutionEventType
    occurred_at: datetime
    causation_id: Identifier | None = None
    correlation_id: Identifier | None = None
    request_digest: Digest | None = None
    payload_digest: Digest
    payload: EvolutionEventPayload

    @field_validator("occurred_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be UTC")
        return value

    @model_validator(mode="after")
    def validate_payload_type(self) -> EvolutionEvent:
        payload_types: tuple[tuple[EvolutionEventType, type[object]], ...] = (
            (EvolutionEventType.EVOLUTION_STARTED, EvolutionStarted),
            (EvolutionEventType.HYPOTHESIS_LINKED, HypothesisLinked),
            (EvolutionEventType.CANDIDATE_PREPARED, CandidatePrepared),
            (EvolutionEventType.CANDIDATE_SUBMITTED, CandidateSubmitted),
            (EvolutionEventType.RUN_STARTED, RunStarted),
            (EvolutionEventType.RUN_COMPLETED, RunCompleted),
            (EvolutionEventType.GATE_DECIDED, GateDecided),
            (EvolutionEventType.CANDIDATE_ACCEPTED, CandidateAccepted),
            (EvolutionEventType.CANDIDATE_REJECTED, CandidateRejected),
            (EvolutionEventType.RELEASE_PUBLISHED, ReleasePublished),
            (EvolutionEventType.RELEASE_ROLLED_BACK, ReleaseRolledBack),
            (EvolutionEventType.EVOLUTION_STOPPED, EvolutionStopped),
            (EvolutionEventType.EXTERNAL_OPERATION_INTENT, ExternalOperationIntent),
            (EvolutionEventType.EXTERNAL_OPERATION_BLOCKED, ExternalOperationBlocked),
        )
        for event_type, payload_type in payload_types:
            if self.event_type is event_type:
                if not isinstance(self.payload, payload_type):
                    raise ValueError("event payload does not match event_type")
                return self
        raise ValueError("unknown event type")

    @model_validator(mode="after")
    def validate_payload_digest(self) -> EvolutionEvent:
        if self.payload_digest != _digest(self.payload.model_dump_json()):
            raise ValueError("payload_digest does not match payload")
        return self

    def fingerprint(self) -> str:
        content = self.model_dump_json(exclude={"sequence", "event_id"})
        return _digest(content)


@dataclass(frozen=True, slots=True)
class EvolutionEventDraft:
    event_type: EvolutionEventType
    experiment_id: str
    payload: EvolutionEventPayload
    occurred_at: datetime
    causation_id: str | None = None
    correlation_id: str | None = None
    request_digest: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(_EXPERIMENT, self.experiment_id) is None:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.INVALID_EVENT, "experiment_id"
            )
        if self.occurred_at.utcoffset() != timedelta(0):
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.INVALID_EVENT, "occurred_at"
            )

    def build(self, sequence: int) -> EvolutionEvent:
        content = EvolutionEvent(
            experiment_id=self.experiment_id,
            sequence=sequence,
            event_id="sha256:" + "0" * 64,
            event_type=self.event_type,
            occurred_at=self.occurred_at,
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
            request_digest=self.request_digest,
            payload_digest=_digest(self.payload.model_dump_json()),
            payload=self.payload,
        )
        identity = _draft_identity(self, content)
        computed_event_id = _digest(identity)
        if self.event_id is not None and self.event_id != computed_event_id:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.EVENT_CONFLICT,
                self.event_id,
            )
        event_id = computed_event_id
        return EvolutionEvent(
            experiment_id=content.experiment_id,
            sequence=content.sequence,
            event_id=event_id,
            event_type=content.event_type,
            occurred_at=content.occurred_at,
            causation_id=content.causation_id,
            correlation_id=content.correlation_id,
            request_digest=content.request_digest,
            payload_digest=content.payload_digest,
            payload=content.payload,
        )


@dataclass(frozen=True, slots=True)
class EvolutionEventPage:
    events: tuple[EvolutionEvent, ...]
    next_cursor: str | None


class FileEvolutionLedger:
    """One append-only event log per experiment, rooted at the Git common dir."""

    def append(
        self, workspace_root: Path, draft: EvolutionEventDraft
    ) -> EvolutionEvent:
        control = _control(workspace_root, draft.experiment_id)
        try:
            with _writer(control) as directory:
                return _append_to_directory(directory, draft)
        except EvolutionLedgerFailure:
            raise
        except SafeFileFailure as error:
            raise _safe_failure(error, draft.experiment_id) from None
        except OSError:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.WRITE_FAILED,
                draft.experiment_id,
            ) from None

    def page(
        self,
        workspace_root: Path,
        experiment_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> EvolutionEventPage:
        if not 1 <= limit <= 500:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.INVALID_EVENT, "limit"
            )
        control = _control(workspace_root, experiment_id)
        try:
            with open_directory_chain(
                control.parents[2],
                ("ofw", "preparations", control.name),
                create=False,
            ) as directory:
                events = _read_events(directory, experiment_id)
        except FileNotFoundError:
            events = ()
        except SafeFileFailure as error:
            raise _safe_failure(error, experiment_id) from None
        except OSError:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.INVALID_WORKSPACE,
                experiment_id,
            ) from None
        return _select_page(events, experiment_id, cursor, limit)

    def events(
        self, workspace_root: Path, experiment_id: str
    ) -> tuple[EvolutionEvent, ...]:
        events: tuple[EvolutionEvent, ...] = ()
        cursor: str | None = None
        while True:
            page = self.page(
                workspace_root,
                experiment_id,
                cursor=cursor,
                limit=500,
            )
            events += page.events
            if page.next_cursor is None:
                return events
            cursor = page.next_cursor


def _append_to_directory(directory: int, draft: EvolutionEventDraft) -> EvolutionEvent:
    events = _read_events(directory, draft.experiment_id)
    candidate = draft.build(len(events) + 1)
    for existing in events:
        if existing.event_id != candidate.event_id:
            continue
        if existing.fingerprint() == candidate.fingerprint():
            return existing
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.EVENT_CONFLICT,
            candidate.event_id,
            existing.sequence,
        )
    _append_event(directory, candidate)
    return candidate


def _select_page(
    events: tuple[EvolutionEvent, ...],
    experiment_id: str,
    cursor: str | None,
    limit: int,
) -> EvolutionEventPage:
    after = _decode_cursor(cursor, experiment_id) if cursor is not None else 0
    selected = tuple(event for event in events if event.sequence > after)
    page = selected[:limit]
    next_cursor = _next_cursor(experiment_id, selected, page)
    return EvolutionEventPage(page, next_cursor)


def _next_cursor(
    experiment_id: str,
    selected: tuple[EvolutionEvent, ...],
    page: tuple[EvolutionEvent, ...],
) -> str | None:
    if len(selected) <= len(page) or not page:
        return None
    return _encode_cursor(experiment_id, page[-1].sequence)


@contextmanager
def _writer(control: Path) -> Iterator[int]:
    with open_directory_chain(
        control.parents[2],
        ("ofw", "preparations", control.name),
        create=True,
    ) as directory:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            lock = os.open(".evolution.lock", flags, 0o600, dir_fd=directory)
        except OSError:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.BUSY, control.name
            ) from None
        token = uuid4().hex.encode("ascii")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EvolutionLedgerFailure(
                    EvolutionLedgerErrorCode.BUSY, control.name
                ) from None
            os.ftruncate(lock, 0)
            os.write(lock, token)
            os.fsync(lock)
            if os.pread(lock, 64, 0) != token:
                raise EvolutionLedgerFailure(
                    EvolutionLedgerErrorCode.BUSY, control.name
                )
            yield directory
            if os.pread(lock, 64, 0) != token:
                raise EvolutionLedgerFailure(
                    EvolutionLedgerErrorCode.BUSY, control.name
                )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
            os.fsync(directory)


def _control(root: Path, experiment_id: str) -> Path:
    if not root.is_absolute() or re.fullmatch(_EXPERIMENT, experiment_id) is None:
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.INVALID_WORKSPACE, experiment_id
        )
    try:
        return experiment_control_directory(root, experiment_id)
    except ExperimentPolicyFailure:
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.INVALID_WORKSPACE,
            experiment_id,
        ) from None


def _read_events(directory: int, experiment_id: str) -> tuple[EvolutionEvent, ...]:
    try:
        content = read_bounded(
            directory,
            "evolution.jsonl",
            maximum_bytes=_LEDGER_LIMIT_BYTES,
            subject=experiment_id,
        )
    except FileNotFoundError:
        return ()
    except SafeFileFailure as error:
        if error.code is SafeFileErrorCode.TOO_LARGE:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.LEDGER_TOO_LARGE,
                experiment_id,
            ) from None
        raise
    if not content:
        return ()
    return _parse_events(content, experiment_id)


def _parse_events(content: bytes, experiment_id: str) -> tuple[EvolutionEvent, ...]:
    events: list[EvolutionEvent] = []
    for line in content.splitlines(keepends=True):
        event = _parse_event_line(line, experiment_id, events)
        _validate_event_order(event, experiment_id, events)
        events.append(event)
    return tuple(events)


def _parse_event_line(
    line: bytes, experiment_id: str, events: list[EvolutionEvent]
) -> EvolutionEvent:
    last = events[-1].sequence if events else 0
    if not line.endswith(b"\n"):
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CORRUPT_LEDGER, experiment_id, last
        )
    try:
        return EvolutionEvent.model_validate_json(line)
    except (ValueError, UnicodeError):
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CORRUPT_LEDGER, experiment_id, last
        ) from None


def _validate_event_order(
    event: EvolutionEvent, experiment_id: str, events: list[EvolutionEvent]
) -> None:
    last = events[-1].sequence if events else 0
    if event.experiment_id != experiment_id:
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CORRUPT_LEDGER, experiment_id, last
        )
    if event.sequence != last + 1:
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.SEQUENCE_GAP, experiment_id, last
        )
    if _has_duplicate_event(events, event.event_id):
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CORRUPT_LEDGER, experiment_id, last
        )
    _validate_event_identity(event, experiment_id, last)


def _validate_event_identity(
    event: EvolutionEvent, experiment_id: str, last: int
) -> None:
    identity = _event_identity(event)
    if event.event_id != _digest(identity):
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CORRUPT_LEDGER, experiment_id, last
        )


def _has_duplicate_event(events: list[EvolutionEvent], event_id: str) -> bool:
    return any(item.event_id == event_id for item in events)


def _draft_identity(draft: EvolutionEventDraft, event: EvolutionEvent) -> str:
    fingerprint = (
        event.fingerprint()
        if draft.causation_id is None and draft.correlation_id is None
        else ""
    )
    return "\0".join(
        (
            draft.experiment_id,
            draft.event_type.value,
            draft.causation_id or "",
            draft.correlation_id or "",
            fingerprint,
        )
    )


def _event_identity(event: EvolutionEvent) -> str:
    fingerprint = (
        event.fingerprint()
        if event.causation_id is None and event.correlation_id is None
        else ""
    )
    return "\0".join(
        (
            event.experiment_id,
            event.event_type.value,
            event.causation_id or "",
            event.correlation_id or "",
            fingerprint,
        )
    )


def _append_event(directory: int, event: EvolutionEvent) -> None:
    content = (event.model_dump_json() + "\n").encode("utf-8")
    if len(content) > _EVENT_LIMIT_BYTES:
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.LEDGER_TOO_LARGE,
            event.event_id,
            event.sequence - 1,
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open("evolution.jsonl", flags, 0o600, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.INVALID_WORKSPACE,
                event.experiment_id,
                event.sequence - 1,
            )
        if os.fstat(descriptor).st_size + len(content) > _LEDGER_LIMIT_BYTES:
            raise EvolutionLedgerFailure(
                EvolutionLedgerErrorCode.LEDGER_TOO_LARGE,
                event.experiment_id,
                event.sequence - 1,
            )
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory)


def _encode_cursor(experiment_id: str, sequence: int) -> str:
    body = f"1\0{experiment_id}\0{sequence}".encode()
    digest = hashlib.sha256(body).hexdigest()[:32].encode("ascii")
    return base64.urlsafe_b64encode(body + b"\0" + digest).decode("ascii").rstrip("=")


def _decode_cursor(value: str, experiment_id: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        version, actual_experiment, sequence_text, digest = raw.split(b"\0")
        return _cursor_sequence(
            version, actual_experiment, sequence_text, digest, experiment_id
        )
    except (ValueError, UnicodeError, binascii.Error):
        raise EvolutionLedgerFailure(
            EvolutionLedgerErrorCode.CURSOR_INVALID,
            experiment_id,
        ) from None


def _cursor_sequence(
    version: bytes,
    actual_experiment: bytes,
    sequence_text: bytes,
    digest: bytes,
    experiment_id: str,
) -> int:
    body = b"\0".join((version, actual_experiment, sequence_text))
    if version != b"1" or actual_experiment.decode() != experiment_id:
        raise ValueError
    if digest != hashlib.sha256(body).hexdigest()[:32].encode("ascii"):
        raise ValueError
    sequence = int(sequence_text)
    if sequence < 0:
        raise ValueError
    return sequence


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_failure(error: SafeFileFailure, subject: str) -> EvolutionLedgerFailure:
    code = (
        EvolutionLedgerErrorCode.LEDGER_TOO_LARGE
        if error.code is SafeFileErrorCode.TOO_LARGE
        else EvolutionLedgerErrorCode.INVALID_WORKSPACE
    )
    return EvolutionLedgerFailure(code, subject)
