"""Domain exceptions for transaction abort and error mapping."""

from __future__ import annotations

import sqlite3
from typing import TypeVar

from openflywheel.contracts.operation_result import OperationResult

T = TypeVar("T")


class DomainError(Exception):
    code: str
    message: str
    root_cause_hint: str
    safe_retry: bool
    stop_condition: str

    def __init__(
        self,
        *,
        code: str,
        message: str,
        root_cause_hint: str,
        safe_retry: bool,
        stop_condition: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.root_cause_hint = root_cause_hint
        self.safe_retry = safe_retry
        self.stop_condition = stop_condition

    def to_operation_result(self) -> OperationResult[T]:
        return OperationResult.failure(
            code=self.code,
            message=self.message,
            root_cause_hint=self.root_cause_hint,
            safe_retry=self.safe_retry,
            stop_condition=self.stop_condition,
        )


class OnboardingPreconditionError(DomainError):
    pass


class OnboardingTransactionError(DomainError):
    pass


class IngestPreconditionError(DomainError):
    pass


class IngestTransactionError(DomainError):
    """Raised inside a write transaction to force rollback."""


class WorkspaceInitError(DomainError):
    pass


class StoreNotFoundError(DomainError):
    pass


def map_sqlite_error(exc: sqlite3.Error) -> DomainError:
    _ = exc  # raw text must not reach CLI surfaces
    return DomainError(
        code="SQLITE_ERROR",
        message="Database operation failed",
        root_cause_hint="Internal storage rejected the operation; transaction was rolled back",
        safe_retry=True,
        stop_condition="Fix schema/data preconditions or retry after resolving lock contention",
    )
