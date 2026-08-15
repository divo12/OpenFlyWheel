"""Test-only checkpoint commit hooks for ingest UoW."""

from __future__ import annotations

from typing import Protocol

from openflywheel.contracts.ids import SourceId
from openflywheel.store.exceptions import IngestTransactionError


class CheckpointCommitHook(Protocol):
    def before_checkpoint_commit(self, *, source_id: SourceId, cursor_value: str) -> None: ...


class NoOpCheckpointCommitHook:
    def before_checkpoint_commit(self, *, source_id: SourceId, cursor_value: str) -> None:
        return None


class AbortCheckpointCommitHook:
    def before_checkpoint_commit(self, *, source_id: SourceId, cursor_value: str) -> None:
        raise IngestTransactionError(
            code="INGEST_TXN_ABORT",
            message="Injected checkpoint commit abort",
            root_cause_hint="Test hook aborted transaction before checkpoint write",
            safe_retry=True,
            stop_condition="Disable test hook",
        )
